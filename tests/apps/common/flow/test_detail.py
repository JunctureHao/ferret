"""详情面板：左右分栏（请求区 | 响应区）的结构与数据流。

分四层：

* `FlowDataPanel` 验结构：左右两栏各几条标签、哪条在什么时候藏起来、响应栏
  什么时候整个收起、× 挂在哪一栏；
* 左栏/右栏的内容：Raw 的兜底拼装、body 三态、头数徽标；
* `CommentPane`：脏了才亮保存、程序化灌文本不算编辑；
* 标记与备注写回：「没点的时候绝对不写」比「点了有没有写回」更要紧。

翻译器**故意不装**，理由同 `test_fields.py`。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import patch

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import InfoLevel

from ferret.apps.common import dialog
from ferret.apps.common.edit import Language
from ferret.apps.common.flow import detail
from ferret.apps.common.flow.detail import (
    FlowDataPanel,
    ResponsePane,
    _body_lang,
    status_level,
)
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    READONLY_CAPABILITIES,
)
from ferret.core.mitm import MARKER_DEFAULT, WsClose, WsFrame, build_flow_detail


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
    """结构这一侧：两栏各几条标签、什么时候藏、× 挂在哪一栏。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.panel = FlowDataPanel(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_two_columns_of_flat_tabs(self) -> None:
        """左右两栏各一排扁平标签，导航只有一层。"""
        self.assertEqual(
            list(self.panel.req_tabs.pivot.items),
            ["Overview", "Raw", "Headers", "Body", "Query", "Cookies", "Comment"],
        )
        self.assertEqual(
            list(self.panel.res_pane.pivot.items),
            ["Raw", "Headers", "Body", "Messages"],
        )
        self.assertEqual(self.panel.req_tabs.pivot.currentRouteKey(), "Overview")

    def test_a_plain_http_flow_hides_the_messages_tab(self) -> None:
        """九成流量既不是 WS 也不是 SSE，那一条标签不该出现。"""
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertTrue(self.panel.res_pane.isTabVisible("Messages") is False)
        self.assertTrue(self.panel.message_badge.isHidden())

    def test_the_empty_page_shows_until_there_is_data(self) -> None:
        self.assertEqual(self.panel.stack.currentIndex(), 0)
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertEqual(self.panel.stack.currentIndex(), 1)

    def test_a_request_only_flow_collapses_the_response_pane(self) -> None:
        """没有响应可看就整个半栏收起，别让人拖出一栏空白。"""
        self.panel.resize(800, 600)
        self.panel.show()
        self.app.processEvents()  # 首次 show 的下一拍才会平分内层分栏
        self.panel.set_data(build_flow_detail(tflow.tflow()))
        self.assertTrue(self.panel.res_pane.isHidden())

        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.panel.res_pane.isHidden())
        # 右栏重新出现时按 50/50 平分起步（用户之后拖出的比例自然保留）。
        # QSplitter 会把每栏钳到最小尺寸提示上，等分允许几像素的漂移。
        first, second = self.panel.splitter.sizes()
        self.assertGreater(second, 0)
        self.assertAlmostEqual(second / (first + second), 0.5, delta=0.02)

    def test_the_headers_tab_label_carries_the_count(self) -> None:
        data = build_flow_detail(tflow.tflow(resp=True))
        self.panel.set_data(data)
        label = self.panel.req_tabs.pivot.items["Headers"].text()
        self.assertIn(str(len(data["Request Headers"])), label)

    def test_a_request_only_flow_moves_the_close_button_back(self) -> None:
        """横向分栏时 × 归右栏；右栏收起后 × 落回请求区，保证随时可点。"""
        self.panel.splitter.setOrientation(Qt.Orientation.Horizontal)
        self.panel._on_layout_changed()
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.panel.res_pane.close_button.isHidden())
        self.assertTrue(self.panel.req_tabs.close_button.isHidden())

        self.panel.set_data(build_flow_detail(tflow.tflow()))
        self.assertTrue(self.panel.res_pane.close_button.isHidden())
        self.assertFalse(self.panel.req_tabs.close_button.isHidden())

    def test_the_inner_split_is_inverted_against_the_global_layout(self) -> None:
        """全局布局说的是「表格 vs 详情」的排布；内层分栏与它相反才放得下：
        全局横向（详情窄而高）→ 内层上下排，全局纵向（详情宽而矮）→ 内层左右排。"""
        self.assertTrue(self.panel.splitter.inverted)

    def test_the_close_button_follows_the_split_orientation(self) -> None:
        """纵向分栏时上栏是请求区，× 挂请求区标签行。"""
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.panel.splitter.setOrientation(Qt.Orientation.Vertical)
        self.panel._on_layout_changed()
        self.assertFalse(self.panel.req_tabs.close_button.isHidden())
        self.assertTrue(self.panel.res_pane.close_button.isHidden())

        self.panel.splitter.setOrientation(Qt.Orientation.Horizontal)
        self.panel._on_layout_changed()
        self.assertFalse(self.panel.res_pane.close_button.isHidden())

    def test_every_close_button_wires_to_collapse_requested(self) -> None:
        seen: list[int] = []
        self.panel.collapseRequested.connect(lambda: seen.append(1))
        for button in (
            self.panel.empty_close_button,
            self.panel.req_tabs.close_button,
            self.panel.res_pane.close_button,
        ):
            button.click()
        self.assertEqual(len(seen), 3)

    def test_the_more_menu_is_gated_by_capabilities(self) -> None:
        """会话页是只读的：重放/标记/备注弹窗一律不出现，复制两样保留。"""
        readonly = FlowDataPanel(self.host, None, READONLY_CAPABILITIES)
        readonly_names = [a.text() for a in readonly._more_actions()]
        self.assertNotIn("Replay", readonly_names)
        self.assertNotIn("Mark", readonly_names)
        self.assertNotIn("Comment", readonly_names)
        self.assertIn("Copy URL", readonly_names)

        names = [a.text() for a in self.panel._more_actions()]
        for expected in ("Replay", "Mark", "Comment"):
            self.assertIn(expected, names)

    def test_the_copy_action_goes_dead_when_there_is_nothing_to_copy(self) -> None:
        self.panel.set_data(build_flow_detail(tflow.tflow()))
        self.assertTrue(self.panel.copy_url_action.isEnabled())

    def test_the_query_and_cookies_tabs_carry_counts(self) -> None:
        """与 请求头(N) 同一语言：条数直接挂在标签上，不用回概览看。"""
        flow = tflow.tflow(resp=True)
        flow.request.path = "/path?a=1&b=2"
        flow.request.headers["Cookie"] = "a=1; b=2"
        self.panel.set_data(build_flow_detail(flow))

        self.assertIn("2", self.panel.req_tabs.pivot.items["Query"].text())
        self.assertIn("2", self.panel.req_tabs.pivot.items["Cookies"].text())


class MessageBadgeTests(unittest.TestCase):
    """「消息」计数徽标：外挂在标签右侧，不许压住标签文字。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.panel = FlowDataPanel(self.host)
        self.host.resize(900, 700)
        self.host.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def __show_websocket(self) -> None:
        """给面板接一个 stub 控制器，让徽标亮出来。raw 报文按详情字典兜底。"""

        class FramesOnly:
            def websocket_frames(self, _flow_id):
                return [
                    WsFrame(0, True, 1, b"hi", 1700000000.0, False, False),
                    WsFrame(1, False, 1, b"pong", 1700000001.0, False, False),
                ]

            def websocket_close(self, _flow_id):
                return WsClose()

            def get_raw_request(self, _flow_id):
                return ""

            def get_raw_response(self, _flow_id):
                return ""

        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        self.panel.controller = FramesOnly()
        self.panel.set_data(build_flow_detail(flow))
        self.app.processEvents()

    def test_the_badge_sits_outside_the_tab_not_on_top_of_it(self) -> None:
        """qfw 的 RIGHT 锚点公式会把徽标半压在标签上，这里必须完全外挂。"""
        self.__show_websocket()
        badge = self.panel.message_badge
        self.assertFalse(badge.isHidden())

        tab = self.panel.res_pane.pivot.items["Messages"]
        pane = self.panel.res_pane
        # 徽标挂 res_pane、标签挂 pivot，统一折算到 res_pane 坐标再比。
        tab_right = pane.mapFrom(
            self.panel.res_pane.pivot, tab.geometry().topRight()
        ).x()
        self.assertGreaterEqual(badge.geometry().left(), tab_right + 1)

    def test_the_badge_follows_a_wider_number(self) -> None:
        """数字从个位数涨到四位数后仍不遮字 —— 变宽要触发重定位。"""
        self.__show_websocket()
        self.panel.message_badge.setText("1024")
        self.panel.message_badge.adjustSize()
        self.panel.message_badge.manager.reposition()

        tab = self.panel.res_pane.pivot.items["Messages"]
        pane = self.panel.res_pane
        tab_right = pane.mapFrom(
            self.panel.res_pane.pivot, tab.geometry().topRight()
        ).x()
        self.assertGreaterEqual(
            self.panel.message_badge.geometry().left(), tab_right + 1
        )

    def test_the_badge_follows_the_tab_when_it_moves(self) -> None:
        """标签移动（别的标签变宽把它挤走）后徽标要跟过去。"""
        self.__show_websocket()
        badge = self.panel.message_badge
        tab = self.panel.res_pane.pivot.items["Messages"]
        before = badge.x() - tab.geometry().right()

        tab.move(tab.x() + 60, tab.y())
        self.app.processEvents()

        self.assertEqual(badge.x() - tab.geometry().right(), before)


class LeftColumnTests(unittest.TestCase):
    """左栏内容：Raw 的兜底拼装、body 三态、查询参数与 Cookies 的显隐。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.panel = FlowDataPanel(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_an_empty_body_shows_the_placeholder_not_a_blank_editor(self) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.content = b""
        self.panel.set_data(build_flow_detail(flow))
        self.assertIs(
            self.panel.req_body.currentWidget(), self.panel.req_body.empty_label
        )

    def test_a_json_body_lands_on_the_dual_view(self) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.headers["Content-Type"] = "application/json"
        flow.request.content = b'{"a": 1}'
        self.panel.set_data(build_flow_detail(flow))

        self.assertIs(
            self.panel.req_body.currentWidget(), self.panel.req_body.json_panel
        )
        self.assertTrue(self.panel.req_body_badge.isVisibleTo(self.panel))
        self.assertEqual(self.panel.req_body_badge.text(), "JSON")

    def test_a_bodyless_message_shows_no_view_badge(self) -> None:
        flow = tflow.tflow()
        flow.request.content = b""
        self.panel.set_data(build_flow_detail(flow))
        self.assertFalse(self.panel.req_body_badge.isVisibleTo(self.panel))

    def test_a_form_body_is_a_view_of_the_body_not_its_own_tab(self) -> None:
        """urlencoded 表单是请求体的一种格式：键值表格顶进 Body 页。"""
        flow = tflow.tflow(resp=True)
        flow.request.headers["Content-Type"] = "application/x-www-form-urlencoded"
        flow.request.content = b"user=jun&tag=a&tag=b"
        self.panel.set_data(build_flow_detail(flow))

        self.assertIsNotNone(self.panel.req_body.form_panel)
        assert self.panel.req_body.form_panel is not None
        self.assertIs(
            self.panel.req_body.currentWidget(), self.panel.req_body.form_panel
        )
        self.assertNotIn("Form", list(self.panel.req_tabs.pivot.items))

    def test_a_query_string_brings_its_tab_back(self) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.path = "/path?a=1&a=2"
        self.panel.set_data(build_flow_detail(flow))
        self.assertTrue(self.panel.req_tabs.isTabVisible("Query"))

    def test_tabs_with_nothing_in_them_disappear(self) -> None:
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.panel.req_tabs.isTabVisible("Query"))
        # 这条流量没有 cookie，标签整条藏掉。
        self.assertFalse(self.panel.req_tabs.isTabVisible("Cookies"))
        # 备注页常驻：内联编辑是日常入口。
        self.assertTrue(self.panel.req_tabs.isTabVisible("Comment"))

    def test_the_raw_page_is_assembled_from_the_detail_dict_without_a_controller(
        self,
    ) -> None:
        """没有 controller 时按详情字典手工拼「起始行 + 头 + 空行 + body」。"""
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))

        raw = self.panel.req_raw.text()
        self.assertTrue(raw.startswith("GET /path HTTP/1.1"))
        self.assertIn("header: qvalue", raw)
        res_raw = self.panel.res_pane.raw_edit.text()
        self.assertTrue(res_raw.startswith("HTTP/1.1 200 OK"))

    def test_a_broken_controller_does_not_take_the_raw_page_down(self) -> None:
        """用 `assertLogs` 而不是任由 warning 冒到根 logger：整套用例同进程跑，
        前面造过 `Master` 的用例会在根 logger 上留下 mitmproxy 的
        `LegacyLogEvents`，它指着一个已经关掉的 event loop —— 冒上去就成了
        `RuntimeError`。顺手把「出错要留一行日志」也一起钉住。
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
        self.assertTrue(self.panel.req_raw.text().startswith("GET /path"))
        self.assertTrue(self.panel.res_pane.raw_edit.text().startswith("HTTP/1.1 200"))

    def test_a_working_controller_wins_over_the_hand_assembled_fallback(self) -> None:
        class Wire:
            def get_raw_request(self, flow_id):
                return b"GET /wire HTTP/1.1\r\n\r\n"

            def get_raw_response(self, flow_id):
                return "HTTP/1.1 418 I'm a teapot\r\n\r\n"

        self.panel.set_controller(Wire())
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertIn("/wire", self.panel.req_raw.text())
        self.assertIn("418", self.panel.res_pane.raw_edit.text())


class ResponsePaneTests(unittest.TestCase):
    """响应栏单独复用的那一份（compose 页）：标签集合与头数徽标。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_tab_set_without_cookies_or_trailers(self) -> None:
        """响应侧不放 Cookies，Trailers 也不做（数据键还在字典里）。"""
        pane = ResponsePane()
        self.assertEqual(
            list(pane.pivot.items),
            ["Raw", "Headers", "Body"],
        )
        pane.deleteLater()

    def test_the_headers_tab_label_carries_the_count(self) -> None:
        pane = ResponsePane()
        data = build_flow_detail(tflow.tflow(resp=True))
        pane.set_data(data)
        self.assertIn(str(len(data["Response Headers"])), pane.pivot.items["Headers"].text())
        pane.deleteLater()

    def test_raw_falls_back_to_the_detail_dict_without_a_controller(self) -> None:
        pane = ResponsePane()
        pane.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertTrue(pane.raw_edit.text().startswith("HTTP/1.1 200 OK"))
        pane.deleteLater()


class CommentPaneTests(unittest.TestCase):
    """备注内联编辑页：脏了才亮保存，程序化灌文本不算编辑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.pane = detail.CommentPane()
        self.pane.setParent(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_loading_a_note_does_not_light_the_save_button(self) -> None:
        self.pane.set_data({"comment": "旧备注"})
        self.assertFalse(self.pane.save_button.isEnabled())

    def test_editing_lights_it_and_mark_saved_extinguishes_it(self) -> None:
        self.pane.set_data({"comment": ""})
        self.pane.edit.code_widget.setPlainText("新备注")
        self.assertTrue(self.pane.save_button.isEnabled())

        self.pane.mark_saved("新备注")
        self.assertFalse(self.pane.save_button.isEnabled())

    def test_saving_emits_the_text(self) -> None:
        seen: list[str] = []
        self.pane.commentSaved.connect(seen.append)
        self.pane.set_data({"comment": ""})
        self.pane.edit.code_widget.setPlainText("看这条")
        self.pane.save_button.click()
        self.assertEqual(seen, ["看这条"])

    def test_read_only_mode_never_lights_the_save_button(self) -> None:
        self.pane.set_read_only(True)
        self.pane.set_data({"comment": ""})
        self.pane.edit.code_widget.setPlainText("改不动")
        self.assertFalse(self.pane.save_button.isEnabled())


class _MarkStub:
    """只认标记和备注两件事的控制器替身。会话页那侧刻意也是这么缺的。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail = fail

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        if self.fail:
            raise RuntimeError("kernel is not running")
        self.calls.append(("mark", flow_id, marked))

    def set_flow_comment(self, flow_id: str, comment: str) -> None:
        if self.fail:
            raise RuntimeError("kernel is not running")
        self.calls.append(("comment", flow_id, comment))

    def get_raw_request(self, flow_id: str) -> bytes:
        return b""

    def get_raw_response(self, flow_id: str) -> bytes:
        return b""


class MarkAndCommentTests(unittest.TestCase):
    """「…」菜单上的标记与备注弹窗，加内联编辑页的写回。

    这些动作都要改**活** flow，所以除了「点了有没有写回」，更要紧的是「没点的时候
    绝对不写」—— 切换选中行会把上一条的状态同步到动作上，一个没拦住的 `toggled`
    就等于把上一条的标记盖到刚选中的那条流量上。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.controller = _MarkStub()
        self.panel = FlowDataPanel(
            self.host,
            self.controller,
            CAPTURE_CAPABILITIES,
        )
        self.flow = tflow.tflow(resp=True)
        self.panel.set_data(build_flow_detail(self.flow))

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_the_mark_action_renders_as_a_toggle(self) -> None:
        self.assertTrue(self.panel.mark_action.isCheckable())

    def test_toggling_writes_the_marker_mitmproxy_itself_writes(self) -> None:
        self.panel.mark_action.setChecked(True)
        self.assertEqual(
            self.controller.calls, [("mark", self.flow.id, MARKER_DEFAULT)]
        )

    def test_untoggling_clears_it(self) -> None:
        self.panel.mark_action.setChecked(True)
        self.panel.mark_action.setChecked(False)
        self.assertEqual(self.controller.calls[-1], ("mark", self.flow.id, ""))

    def test_selecting_a_marked_flow_ticks_the_toggle(self) -> None:
        marked = tflow.tflow(resp=True)
        marked.marked = MARKER_DEFAULT
        self.panel.set_data(build_flow_detail(marked))
        self.assertTrue(self.panel.mark_action.isChecked())

        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.panel.mark_action.isChecked())

    def test_selecting_a_flow_never_writes_anything(self) -> None:
        """这一条是整个功能里最容易错的地方：同步勾选态也会发 `toggled`。"""
        marked = tflow.tflow(resp=True)
        marked.marked = MARKER_DEFAULT
        self.panel.set_data(build_flow_detail(marked))
        self.panel.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertEqual(self.controller.calls, [])

    def test_a_failed_write_rolls_the_toggle_back(self) -> None:
        """否则界面说「标了」而 flow 上没有 —— 这条流量以后也不会再被重画。"""
        panel = FlowDataPanel(
            self.host,
            _MarkStub(fail=True),
            CAPTURE_CAPABILITIES,
        )
        panel.set_data(build_flow_detail(self.flow))
        panel.mark_action.setChecked(True)
        self.assertFalse(panel.mark_action.isChecked())

    def test_both_actions_go_dead_without_a_flow_to_write_to(self) -> None:
        panel = FlowDataPanel(self.host, None, CAPTURE_CAPABILITIES)
        panel.set_data(build_flow_detail(self.flow))
        self.assertFalse(panel.mark_action.isEnabled())
        self.assertFalse(panel.comment_action.isEnabled())

        self.assertTrue(self.panel.mark_action.isEnabled())
        self.assertTrue(self.panel.comment_action.isEnabled())

    def test_the_comment_dialog_is_the_one_the_context_menu_uses(self) -> None:
        """同一件事在两处长成两个样子本身就是毛病。"""
        with patch.object(detail, "CommentDialog") as factory:
            factory.return_value.exec.return_value = True
            factory.return_value.comment.return_value = "登录接口"
            self.panel.comment_action.trigger()

        self.assertIs(detail.CommentDialog, dialog.CommentDialog)
        self.assertEqual(factory.call_args.args[0], "")
        self.assertEqual(self.controller.calls, [("comment", self.flow.id, "登录接口")])

    def test_the_dialog_opens_on_the_note_that_is_already_there(self) -> None:
        self.flow.comment = "旧备注"
        self.panel.set_data(build_flow_detail(self.flow))
        with patch.object(detail, "CommentDialog") as factory:
            factory.return_value.exec.return_value = False
            self.panel.comment_action.trigger()

        self.assertEqual(factory.call_args.args[0], "旧备注")

    def test_cancelling_writes_nothing(self) -> None:
        with patch.object(detail, "CommentDialog") as factory:
            factory.return_value.exec.return_value = False
            self.panel.comment_action.trigger()

        self.assertEqual(self.controller.calls, [])

    def test_clearing_the_box_and_saving_deletes_the_note(self) -> None:
        """空串是一个有意的取值，不是「没填」。"""
        self.flow.comment = "旧备注"
        self.panel.set_data(build_flow_detail(self.flow))
        with patch.object(detail, "CommentDialog") as factory:
            factory.return_value.exec.return_value = True
            factory.return_value.comment.return_value = ""
            self.panel.comment_action.trigger()

        self.assertEqual(self.controller.calls, [("comment", self.flow.id, "")])

    def test_the_inline_editor_writes_back_through_the_same_channel(self) -> None:
        seen: list[list] = []

        with patch.object(self.panel.overview, "set_data") as overview:
            self.panel.comment_pane.set_data({"comment": ""})
            self.panel.comment_pane.edit.code_widget.setPlainText("内联写回")
            self.panel.comment_pane.save_button.click()
            self.app.processEvents()
            seen.append(self.controller.calls)

        overview.assert_called_once()
        self.assertEqual(seen, [[("comment", self.flow.id, "内联写回")]])

    def test_a_write_back_refreshes_the_overview_card_only(self) -> None:
        """整个 `set_data` 会连消息页一起重建，把 WS 帧表的选中和滚动位置清掉 ——
        而帧还在一秒几十条地进来。"""
        with (
            patch.object(self.panel.overview, "set_data") as overview,
            patch.object(self.panel.messages, "set_data") as messages,
            patch.object(detail, "CommentDialog") as factory,
        ):
            factory.return_value.exec.return_value = True
            factory.return_value.comment.return_value = "看这条"
            self.panel.comment_action.trigger()

        overview.assert_called_once()
        messages.assert_not_called()
        self.assertEqual(overview.call_args.args[0]["comment"], "看这条")

    def test_a_failed_comment_write_leaves_the_cached_detail_alone(self) -> None:
        panel = FlowDataPanel(
            self.host,
            _MarkStub(fail=True),
            CAPTURE_CAPABILITIES,
        )
        panel.set_data(build_flow_detail(self.flow))
        with patch.object(detail, "CommentDialog") as factory:
            factory.return_value.exec.return_value = True
            factory.return_value.comment.return_value = "写不进去"
            panel.comment_action.trigger()

        self.assertEqual(panel.datas.get("comment"), "")


if __name__ == "__main__":
    unittest.main()
