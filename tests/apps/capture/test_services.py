"""界面上的过滤条件翻成 flowfilter 表达式这一步。

两件事值得钉：

* **`~http` 底座不许丢**。View 的基础过滤器（`runtime.py`）和这里拼出来的表达式都
  以它开头，少了它 tcp/udp 流量会直接涌进表格。
* **标志字段不走正则那套**。`~websocket`（原生 `FWebSocket`）压根不带参数，硬塞一个
  `quote_value` 出来的正则进去，整条表达式会解析失败 —— 而失败的表现是筛选静默失效。
"""

import unittest

from mitmproxy.test import tflow

from ferret.apps.capture.services import build_filter_expression, compile_filter


def cond(field: str, logic: str, value: str = "") -> dict:
    return {"field": field, "logic": logic, "value": value}


class FilterExpressionTests(unittest.TestCase):
    def test_no_conditions_leaves_just_the_http_base(self) -> None:
        self.assertEqual(build_filter_expression(None), "~http")
        self.assertEqual(build_filter_expression([]), "~http")

    def test_contains_becomes_an_escaped_url_match(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("URL", "contains", "api.example.com")]),
            r"~http & ~u api\.example\.com",
        )

    def test_excludes_is_the_negation(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("Body", "excludes", "token")]),
            "~http & !~b token",
        )

    def test_equals_anchors_both_ends(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("Method", "equals", "GET")]),
            "~http & ~m ^GET$",
        )

    def test_regex_is_passed_through_untouched(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("Header", "regex", "^X-.*")]),
            "~http & ~h ^X-.*",
        )

    def test_an_empty_value_contributes_nothing(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("URL", "contains", "")]), "~http"
        )

    def test_conditions_are_anded_in_order(self) -> None:
        self.assertEqual(
            build_filter_expression(
                [cond("Method", "equals", "POST"), cond("Body", "contains", "json")]
            ),
            "~http & ~m ^POST$ & ~b json",
        )


class WebsocketFlagTests(unittest.TestCase):
    def test_websocket_is_a_bare_action_filter(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("WebSocket", "is")]),
            "~http & ~websocket",
        )

    def test_is_not_negates_it(self) -> None:
        self.assertEqual(
            build_filter_expression([cond("WebSocket", "is not")]),
            "~http & !~websocket",
        )

    def test_an_empty_value_does_not_drop_the_condition(self) -> None:
        """输入框对它是禁用的，拿「文本为空」判无效等于这个字段永远选不上。"""
        self.assertEqual(
            build_filter_expression([cond("WebSocket", "is", "")]),
            "~http & ~websocket",
        )

    def test_a_stray_value_is_ignored(self) -> None:
        """字段从 URL 换过来时输入框里可能还留着字；带上它表达式直接解析失败。"""
        self.assertEqual(
            build_filter_expression([cond("WebSocket", "is", "leftover text")]),
            "~http & ~websocket",
        )

    def test_it_combines_with_the_value_fields(self) -> None:
        self.assertEqual(
            build_filter_expression(
                [cond("WebSocket", "is"), cond("URL", "contains", "quote")]
            ),
            "~http & ~websocket & ~u quote",
        )


class CompileFilterTests(unittest.TestCase):
    """拼出来的串必须真的能被原生词法器吃下去。"""

    def test_the_websocket_flag_parses(self) -> None:
        self.assertIsNotNone(compile_filter([cond("WebSocket", "is")]))
        self.assertIsNotNone(compile_filter([cond("WebSocket", "is not")]))

    def test_the_flag_actually_separates_ws_from_plain_http(self) -> None:
        ws = tflow.twebsocketflow()
        plain = tflow.tflow(resp=True)

        matcher = compile_filter([cond("WebSocket", "is")])
        assert matcher is not None
        self.assertTrue(matcher(ws))
        self.assertFalse(matcher(plain))

        inverted = compile_filter([cond("WebSocket", "is not")])
        assert inverted is not None
        self.assertFalse(inverted(ws))
        self.assertTrue(inverted(plain))

    def test_the_http_base_keeps_tcp_flows_out(self) -> None:
        matcher = compile_filter(None)
        assert matcher is not None
        self.assertTrue(matcher(tflow.tflow(resp=True)))
        self.assertFalse(matcher(tflow.ttcpflow()))

    def test_every_value_field_parses(self) -> None:
        for field in ("all", "URL", "Method", "Header", "Body"):
            for logic in ("contains", "excludes", "regex", "equals"):
                with self.subTest(field=field, logic=logic):
                    self.assertIsNotNone(compile_filter([cond(field, logic, "abc")]))


if __name__ == "__main__":
    unittest.main()
