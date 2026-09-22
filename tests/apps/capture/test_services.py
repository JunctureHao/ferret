"""过滤面板的整条 flowfilter 表达式套上 `~http` 底座这一步。

模型只有一条原生表达式（`.plans/0-filter-redesign.expression-first.md`）：字段/逻辑/值
那套弱结构化翻译已退役。这里钉两件事：

* **`~http` 底座不许丢**。View 的基础过滤器（`runtime.py`）也以它开头，少了它 tcp/udp
  流量会直接涌进表格。
* **raw 整体加括号**。flowfilter 里并列会攥住 `|`，不加括号 `~http & a | b` 的优先级会把
  整串段拧错（AGENTS.md §5）。
"""

import unittest

from mitmproxy.test import tflow

from ferret.apps.capture.services import build_filter_expression, compile_filter


class FilterExpressionTests(unittest.TestCase):
    def test_empty_expression_is_just_the_http_base(self) -> None:
        self.assertEqual(build_filter_expression(), "~http")
        self.assertEqual(build_filter_expression(""), "~http")
        self.assertEqual(build_filter_expression("   "), "~http")

    def test_expression_is_wrapped_as_a_parenthesized_atom(self) -> None:
        self.assertEqual(
            build_filter_expression('~u "api/.*" & !~m GET'),
            '~http & ( ~u "api/.*" & !~m GET )',
        )

    def test_whitespace_is_trimmed_before_wrapping(self) -> None:
        self.assertEqual(build_filter_expression("  ~m GET  "), "~http & ( ~m GET )")

    def test_a_bare_no_arg_flag_survives_the_wrap(self) -> None:
        """`(~websocket)` 解析失败，`( ~websocket )` 才过——括号内两侧的空格不能省。"""
        self.assertEqual(build_filter_expression("~websocket"), "~http & ( ~websocket )")
        self.assertIsNotNone(compile_filter("~websocket"))


class CompileFilterTests(unittest.TestCase):
    """拼出来的串必须真的能被原生词法器吃下去。"""

    def test_empty_compiles_to_the_http_base(self) -> None:
        matcher = compile_filter()
        assert matcher is not None
        self.assertTrue(matcher(tflow.tflow(resp=True)))
        self.assertFalse(matcher(tflow.ttcpflow()))

    def test_a_valid_expression_compiles(self) -> None:
        self.assertIsNotNone(compile_filter('~u "api/.*" & !~m GET'))

    def test_an_invalid_expression_raises(self) -> None:
        with self.assertRaises(ValueError):
            compile_filter("~~~ not a filter")

    def test_the_websocket_flag_still_works_through_raw(self) -> None:
        ws = tflow.twebsocketflow()
        plain = tflow.tflow(resp=True)
        matcher = compile_filter("~websocket")
        assert matcher is not None
        self.assertTrue(matcher(ws))
        self.assertFalse(matcher(plain))

    def test_the_marked_flag_still_works_through_raw(self) -> None:
        marked = tflow.tflow(resp=True)
        marked.marked = ":bug:"
        plain = tflow.tflow(resp=True)
        matcher = compile_filter("~marked")
        assert matcher is not None
        self.assertTrue(matcher(marked))
        self.assertFalse(matcher(plain))

    def test_or_is_held_by_the_wrapping_parentheses(self) -> None:
        """flowfilter 里并列会攥住 `|`；`~http & ~m GET | ~m POST` 不加括号会被拧成
        别的形状（AGENTS.md §5 钉过的教训）。"""
        matcher = compile_filter("~m GET | ~m POST")
        assert matcher is not None
        get_flow = tflow.tflow(resp=True)
        get_flow.request.method = "GET"
        post_flow = tflow.tflow(resp=True)
        post_flow.request.method = "POST"
        put_flow = tflow.tflow(resp=True)
        put_flow.request.method = "PUT"
        self.assertTrue(matcher(get_flow))
        self.assertTrue(matcher(post_flow))
        self.assertFalse(matcher(put_flow))


if __name__ == "__main__":
    unittest.main()
