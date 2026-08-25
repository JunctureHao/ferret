"""详情面板：一层导航、四页、原始状态的 JSON 化。

分两层：

* `_encode_state` / `state_json` 是纯函数 —— 「原始状态」页的完整性兜底全靠它，
  `bytes`、`tuple`、mitmproxy 自己的对象撞上 `json.dumps` 都不能让整页塌掉；
* `FlowDataPanel` 只验结构：导航几页、哪页在什么时候藏起来、头部那一行读了什么。

字段内容归 `test_fields.py`，产出侧归 `tests/core/mitm/test_detail.py`。
翻译器**故意不装**，理由同 `test_fields.py`。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import InfoLevel

from ferret.apps.common.edit import Language
from ferret.apps.common.flow.detail import (
    _STATE_BYTES_PREVIEW,
    FlowDataPanel,
    _body_lang,
    _encode_state,
    state_json,
    status_level,
)
from ferret.apps.common.flow.protocols import READONLY_CAPABILITIES
from ferret.core.mitm import build_flow_detail


class EncodeStateTests(unittest.TestCase):
    """`Flow.get_state()` → 可序列化的等价结构。保留嵌套，绝不打平。"""

    def test_native_scalars_pass_straight_through(self) -> None:
        for value in (None, True, False, 0, 21, 1.5, "http"):
            with self.subTest(value=value):
                self.assertEqual(_encode_state(value), value)

    def test_bytes_become_a_tagged_preview_not_a_transcoded_blob(self) -> None:
        """一个 body 可能几 MB，这一页要回答的是「这个字段大概装了什么」。"""
        encoded = _encode_state(b"GET / HTTP/1.1\r\n")

        self.assertEqual(encoded["__type__"], "bytes")
        self.assertEqual(encoded["size"], 16)
        self.assertEqual(encoded["hex"], b"GET / HTTP/1.1\r\n".hex(" "))
        # 非可打印字节一律 "."，和 hexdump 的 ASCII 列一个规矩。
        self.assertEqual(encoded["text"], "GET / HTTP/1.1..")

    def test_a_long_bytes_value_reports_its_full_size_but_a_clipped_preview(
        self,
    ) -> None:
        encoded = _encode_state(b"x" * 4096)

        self.assertEqual(encoded["size"], 4096)
        self.assertEqual(len(encoded["text"]), _STATE_BYTES_PREVIEW)
        self.assertEqual(encoded["hex"].count(" "), _STATE_BYTES_PREVIEW - 1)

    def test_a_bytearray_is_treated_like_bytes(self) -> None:
        self.assertEqual(_encode_state(bytearray(b"ab"))["__type__"], "bytes")

    def test_tuples_and_sets_become_arrays(self) -> None:
        """地址对是 `tuple`，`json.dumps` 会把它变成数组 —— 这里提前对齐。"""
        self.assertEqual(_encode_state(("127.0.0.1", 8080)), ["127.0.0.1", 8080])
        self.assertEqual(_encode_state(frozenset({1})), [1])

    def test_dict_keys_are_stringified_because_json_has_no_other_kind(self) -> None:
        self.assertEqual(_encode_state({b"k": 1, 2: "v"}), {"k": 1, "2": "v"})

    def test_nesting_is_preserved_all_the_way_down(self) -> None:
        raw = {"conn": {"alpn": b"h2", "peer": ("1.2.3.4", 443), "tls": None}}
        encoded = _encode_state(raw)

        self.assertEqual(encoded["conn"]["alpn"]["__type__"], "bytes")
        self.assertEqual(encoded["conn"]["peer"], ["1.2.3.4", 443])
        self.assertIsNone(encoded["conn"]["tls"])

    def test_an_unknown_object_degrades_instead_of_sinking_the_whole_tree(self) -> None:
        """一个字段拖垮整页就白搭了 —— 这一页的意义正是「面板漏了什么这里都还在」。"""

        class Odd:
            def __repr__(self) -> str:
                return "<odd>"

        encoded = _encode_state({"a": 1, "weird": Odd()})

        self.assertEqual(encoded["a"], 1)
        self.assertEqual(encoded["weird"], {"__type__": "Odd", "repr": "<odd>"})


class StateJsonTests(unittest.TestCase):
    def test_an_empty_state_renders_nothing_rather_than_the_word_null(self) -> None:
        self.assertEqual(state_json(None), "")
        self.assertEqual(state_json({}), "")

    def test_a_real_flow_state_round_trips_through_json(self) -> None:
        flow = tflow.tflow(resp=True)
        text = state_json(flow.get_state())
        parsed = json.loads(text)

        self.assertEqual(parsed["type"], "http")
        for key in ("client_conn", "server_conn", "request", "response"):
            self.assertIn(key, parsed)

    def test_declaration_order_is_kept_instead_of_sorted_alphabetically(self) -> None:
        """`get_state()` 的顺序是 mitmproxy 自己的字段声明顺序，比字母序好读。"""
        text = state_json({"version": 21, "type": "http", "id": "x"})
        self.assertLess(text.index('"version"'), text.index('"type"'))
        self.assertLess(text.index('"type"'), text.index('"id"'))

    def test_chinese_stays_readable(self) -> None:
        """备注就在原始状态里，`ensure_ascii` 一开就成了一串 \\u。"""
        self.assertIn("看一下这条", state_json({"comment": "看一下这条"}))


class BodyLangTests(unittest.TestCase):
    """contentview 的六种取值 → ferret 的三套词法器。"""

    def test_json_output_gets_the_json_lexer(self) -> None:
        self.assertEqual(_body_lang("yaml", '{"a": 1}'), Language.JSON)
        self.assertEqual(_body_lang("yaml", "[1]"), Language.JSON)

    def test_real_yaml_falls_back_to_the_http_lexer(self) -> None:
        """``key: value`` 行 HTTP 词法器有原生分支，不会整片标红。"""
        self.assertEqual(_body_lang("yaml", "key: value"), Language.HTTP)

    def test_xml_and_everything_else(self) -> None:
        self.assertEqual(_body_lang("xml", "<a/>"), Language.XML)
        for syntax in ("css", "javascript", "none", "error", ""):
            with self.subTest(syntax=syntax):
                self.assertEqual(_body_lang(syntax, "x"), Language.HTTP)


class StatusLevelTests(unittest.TestCase):
    """五个硬编码色值换成 Fluent 的语义等级，主题一换才跟得上。"""

    def test_each_status_class_maps_to_its_own_level(self) -> None:
        cases = {
            "200": InfoLevel.SUCCESS,
            "204": InfoLevel.SUCCESS,
            "301": InfoLevel.ATTENTION,
            "404": InfoLevel.WARNING,
            "500": InfoLevel.ERROR,
            "Error": InfoLevel.ERROR,
        }
        for status, level in cases.items():
            with self.subTest(status=status):
                self.assertEqual(status_level(status), level)

    def test_a_pending_or_nonsense_status_stays_neutral_grey(self) -> None:
        """参数是字符串而不是 int：这一格可能是 ``Pending...``，也可能是 ``Error``。"""
        for status in ("Pending...", "", "abc"):
            with self.subTest(status=status):
                self.assertEqual(status_level(status), InfoLevel.INFOAMTION)

    def test_a_status_below_the_lowest_class_does_not_fall_off_the_table(self) -> None:
        self.assertEqual(status_level("100"), InfoLevel.INFOAMTION)


class FlowDataPanelTests(unittest.TestCase):
    """结构这一侧：导航几页、什么时候藏、头部读了什么。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.panel = FlowDataPanel(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_one_nav_item_per_page(self) -> None:
        """两排十个标签压成一排四个 —— 概览也不再挂在「请求」那半边。"""
        self.assertEqual(
            list(self.panel.nav.items), ["Overview", "Request", "Response", "RawState"]
        )
        self.assertEqual(self.panel.pages.count(), 4)
        self.assertEqual(self.panel.nav.currentRouteKey(), "Overview")

    def test_the_empty_page_shows_until_there_is_data(self) -> None:
        self.assertEqual(self.panel.stack.currentIndex(), 0)
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertEqual(self.panel.stack.currentIndex(), 1)

    def test_the_header_reads_the_four_things_worth_a_glance(self) -> None:
        flow = tflow.tflow(resp=True)
        self.panel.set_data(build_flow_detail(flow))

        self.assertEqual(self.panel.context_method.text(), "GET")
        self.assertEqual(self.panel.context_url.text(), flow.request.pretty_url)
        self.assertEqual(self.panel.context_status.text(), "200")
        self.assertEqual(self.panel.context_status.level, InfoLevel.SUCCESS)
        self.assertTrue(self.panel.context_size.text())

    def test_a_four_key_dict_does_not_blow_up_the_header(self) -> None:
        """表格双击传过来的可能只有几个键，缺键一律当「没有」。"""
        self.panel.set_data({"Method": "POST", "URL": "https://x/y"})

        self.assertEqual(self.panel.context_method.text(), "POST")
        self.assertEqual(self.panel.context_status.text(), "Pending")
        self.assertEqual(self.panel.context_size.text(), "")

    def test_a_request_only_flow_hides_the_response_page(self) -> None:
        """没有响应可看就整页藏掉，别让人点进空白。"""
        self.panel.set_data(build_flow_detail(tflow.tflow()))
        self.assertTrue(self.panel.nav.items["Response"].isHidden())

        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.panel.nav.items["Response"].isHidden())

    def test_hiding_the_current_page_falls_back_to_the_first_visible_one(self) -> None:
        self.panel.nav.setCurrentItem("Response")
        self.panel.set_page_visible("Response", False)
        self.assertEqual(self.panel.nav.currentRouteKey(), "Overview")
        self.assertEqual(self.panel.pages.currentWidget().objectName(), "Overview")

    def test_the_raw_state_page_carries_the_whole_native_tree(self) -> None:
        """完整性兜底：概览只显示 SECTIONS 列出来的，这一页什么都还在。"""
        flow = tflow.tflow(resp=True)
        self.panel.set_data(build_flow_detail(flow))
        parsed = json.loads(self.panel.raw_state_panel.text())

        self.assertEqual(parsed, _encode_state(flow.get_state()))

    def test_the_copy_actions_go_dead_when_there_is_nothing_to_copy(self) -> None:
        self.panel.set_data(build_flow_detail(tflow.tflow()))
        self.assertTrue(self.panel.copy_url_action.isEnabled())
        # 只抓到请求的流量还没有 curl 命令。
        self.assertFalse(self.panel.copy_curl_action.isEnabled())

        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertTrue(self.panel.copy_curl_action.isEnabled())

    def test_replay_is_gated_by_capabilities_not_by_hope(self) -> None:
        """会话页是只读的，`SessionViewController` 根本没有 `replay_flow`。"""
        readonly = FlowDataPanel(self.host, None, READONLY_CAPABILITIES)
        self.assertNotIn(readonly.replay_action, readonly.command_bar.actions())
        self.assertIn(self.panel.replay_action, self.panel.command_bar.actions())

    def test_the_inner_close_buttons_stay_wired_but_out_of_sight(self) -> None:
        """外层那一个 X 是唯一的折叠入口，但两个内层按钮仍连着同一个槽。"""
        seen: list[int] = []
        self.panel.collapseRequested.connect(lambda: seen.append(1))

        self.assertTrue(self.panel.req_panel.close_button.isHidden())
        self.assertTrue(self.panel.res_panel.close_button.isHidden())
        for button in (
            self.panel.req_panel.close_button,
            self.panel.res_panel.close_button,
            self.panel.empty_close_button,
            self.panel.context_close_button,
        ):
            button.click()
        self.assertEqual(len(seen), 4)


class MessagePaneTests(unittest.TestCase):
    """请求/响应两页：标签集合、空标签隐藏、Raw 的兜底拼装。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.panel = FlowDataPanel(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_the_request_page_splits_query_from_form(self) -> None:
        """两个都被叫「参数」，一个在 URL 上一个在 body 里 —— 原来混在一个标签里。"""
        self.assertEqual(
            list(self.panel.req_panel.pivot.items),
            ["Headers", "Query", "Form", "Cookies", "Body", "Trailers", "Raw"],
        )
        self.assertEqual(
            list(self.panel.res_panel.pivot.items),
            ["Headers", "Cookies", "Body", "Trailers", "Raw"],
        )

    def test_tabs_with_nothing_in_them_disappear(self) -> None:
        flow = tflow.tflow(resp=True)
        self.panel.set_data(build_flow_detail(flow))
        req = self.panel.req_panel

        self.assertFalse(req.isTabVisible("Query"))
        self.assertFalse(req.isTabVisible("Form"))
        self.assertFalse(req.isTabVisible("Trailers"))
        # 这三页缺内容本身就是要看的信息，永远在。
        for route_key in ("Headers", "Body", "Raw"):
            with self.subTest(route_key=route_key):
                self.assertTrue(req.isTabVisible(route_key))

    def test_a_form_body_brings_its_tab_back(self) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.headers["Content-Type"] = "application/x-www-form-urlencoded"
        flow.request.content = b"user=jun&tag=a&tag=b"
        self.panel.set_data(build_flow_detail(flow))

        self.assertTrue(self.panel.req_panel.isTabVisible("Form"))
        self.assertFalse(self.panel.req_panel.isTabVisible("Query"))

    def test_a_query_string_brings_its_tab_back(self) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.path = "/path?a=1&a=2"
        self.panel.set_data(build_flow_detail(flow))

        self.assertTrue(self.panel.req_panel.isTabVisible("Query"))

    def test_the_body_view_name_moved_out_of_the_tab_label(self) -> None:
        """原来是拼进 Body 标签的文字里再 adjustSize，标签宽度跟着每条流量跳。"""
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Type"] = "application/json"
        flow.response.content = b'{"a": 1}'
        self.panel.set_data(build_flow_detail(flow))

        badge = self.panel.res_panel.body_view_badge
        self.assertEqual(badge.text(), "JSON")
        item = self.panel.res_panel.pivot.items["Body"]
        self.assertEqual(item.text(), "Body")

    def test_a_bodyless_message_shows_no_view_badge(self) -> None:
        flow = tflow.tflow()
        flow.request.content = b""
        self.panel.set_data(build_flow_detail(flow))
        self.assertFalse(self.panel.req_panel.body_view_badge.isVisibleTo(self.panel))

    def test_the_raw_page_is_assembled_from_the_detail_dict_without_a_controller(
        self,
    ) -> None:
        """没有 controller 时按详情字典手工拼「起始行 + 头 + 空行 + body」。"""
        flow = tflow.tflow(resp=True)
        self.panel.set_data(build_flow_detail(flow))

        raw = self.panel.req_panel.raw_edit.text()
        self.assertTrue(raw.startswith("GET /path HTTP/1.1"))
        self.assertIn("header: qvalue", raw)
        res_raw = self.panel.res_panel.raw_edit.text()
        self.assertTrue(res_raw.startswith("HTTP/1.1 200 OK"))

    def test_a_broken_controller_does_not_take_the_raw_page_down(self) -> None:
        """改造前只有响应那一路包了 try/except，请求那一路裸调。

        用 `assertLogs` 而不是任由 warning 冒到根 logger：整套用例同进程跑，前面
        造过 `Master` 的用例会在根 logger 上留下 mitmproxy 的 `LegacyLogEvents`，
        它指着一个已经关掉的 event loop —— 冒上去就成了 `RuntimeError`。
        顺手把「出错要留一行日志」也一起钉住。
        """

        class Broken:
            def get_raw_request(self, flow_id):
                raise RuntimeError("boom")

            def get_raw_response(self, flow_id):
                raise RuntimeError("boom")

        self.panel.set_controller(Broken())
        with self.assertLogs("ferret.flow.detail", "WARNING") as caught:
            self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertEqual(len(caught.output), 2)
        self.assertTrue(self.panel.req_panel.raw_edit.text().startswith("GET /path"))
        self.assertTrue(self.panel.res_panel.raw_edit.text().startswith("HTTP/1.1 200"))

    def test_a_working_controller_wins_over_the_hand_assembled_fallback(self) -> None:
        class Wire:
            def get_raw_request(self, flow_id):
                return b"GET /wire HTTP/1.1\r\n\r\n"

            def get_raw_response(self, flow_id):
                return "HTTP/1.1 418 I'm a teapot\r\n\r\n"

        self.panel.set_controller(Wire())
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertIn("/wire", self.panel.req_panel.raw_edit.text())
        self.assertIn("418", self.panel.res_panel.raw_edit.text())


if __name__ == "__main__":
    unittest.main()
