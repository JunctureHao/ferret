"""compose 页测试：结构、URL 合并、性能页与状态流转。

compose 此前没有 UI 测试；这批锁住四件事 —— 页面结构（三标签 + 响应区三条
标签）、参数页合并进 URL 的规则（实时写回 URL 栏 + 发送合并同一套端口规范）、
「性能」页（状态行 + 时间/流量卡片）、状态随 发送 → 结果 → 失败 的流转。
发送链路本身归 `tests/core/mitm/test_compose.py`。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from qfluentwidgets import ComboBox, InfoLevel

from ferret.apps.common.splitter import OrientationSplitter
from ferret.apps.compose.views import ComposeInterface
from ferret.core.mitm import ComposeResult

app = QApplication.instance() or QApplication([])


class FakeController(QObject):
    """与真实 ComposeController 同信号面，send 记录调用。"""

    sending_changed = Signal(bool)
    result_ready = Signal(object)
    send_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    def send(self, method: str, url: str, headers, body: str, *, record: bool):
        self.calls.append(("send", method, url, tuple(headers), body, record))


def success_result(**overrides) -> ComposeResult:
    flow = tflow.tflow(resp=True)
    detail = {
        "Flow ID": flow.id,
        "Status Code": 200,
        "Reason": "OK",
        "Duration": "12 ms",
        "res_total_size": 1200,
        "res_headers_size": 300,
        "res_wire_size": 900,
        "req_total_size": 500,
        "Server Address": "1.2.3.4:443",
        "Response Headers": {"content-type": "application/json"},
    }
    detail.update(overrides)
    return ComposeResult(flow_id=flow.id, error="", detail=detail)


class ComposeInterfaceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FakeController()
        self.page = ComposeInterface(self.controller)  # ty: ignore[invalid-argument-type]
        self.addCleanup(self.page.deleteLater)
        self.addCleanup(app.processEvents)


class ConstructionTests(ComposeInterfaceTestCase):
    def test_the_request_side_has_three_tabs(self) -> None:
        self.assertEqual(
            list(self.page.request_panel.pivot.items),
            ["Params", "Headers", "Body"],
        )

    def test_the_response_side_is_headers_body_performance(self) -> None:
        """响应区三条标签，性能在末位；Raw 不给（compose 的结果不需要原始兜底）。"""
        self.assertEqual(
            list(self.page.response_pane.pivot.items),
            ["Headers", "Body", "Perf"],
        )

    def test_the_response_side_starts_on_the_empty_hint(self) -> None:
        self.assertIs(
            self.page.response_stack.currentWidget(), self.page.empty_hint
        )
        self.assertTrue(self.page.status_badge.isHidden())
        self.assertEqual(self.page.status_label.text(), "")

    def test_the_send_button_is_a_primary_sized_action(self) -> None:
        """这一页唯一的主动作：**横向加宽**（宽度 96、高度不锁），宽出周围一圈
        才镇得住顶栏。"""
        self.assertEqual(self.page.send_btn.minimumSize().width(), 96)
        self.assertEqual(self.page.send_btn.maximumSize().width(), 96)
        # 高度不锁死：只加宽，不变高。
        self.assertEqual(
            self.page.send_btn.maximumSize().height(), 16777215  # Qt 默认上限
        )
        self.assertTrue(self.page.send_btn.isEnabled())

    def test_the_method_combo_is_select_only(self) -> None:
        """方法只可从词表里选，不可自由输入（EditableComboBox 是断点那侧的选择）。"""
        self.assertIs(type(self.page.method_combo), ComboBox)

    def test_the_splitter_follows_the_global_layout(self) -> None:
        """全局水平 → 本页左右排；全局垂直 → 上下排（本页不反转）。"""
        self.assertIsInstance(self.page.splitter, OrientationSplitter)
        self.assertFalse(self.page.splitter.inverted)


class UrlMergeTests(ComposeInterfaceTestCase):
    def test_params_are_the_authoritative_query_on_send(self) -> None:
        """与断点面板同一套规则：参数页合并进 URL，空格转义成 +。"""
        self.page.url_edit.setText("https://api.example.com/v1?keep=1")
        self.page.params_card.set_items([("page", "2"), ("tag", "a b")])
        self.assertEqual(
            self.page._collect_url(), "https://api.example.com/v1?page=2&tag=a+b"
        )

    def test_empty_params_leave_the_url_query_alone(self) -> None:
        self.page.url_edit.setText("https://api.example.com/v1?keep=1")
        self.page.params_card.set_items([])
        self.assertEqual(self.page._collect_url(), "https://api.example.com/v1?keep=1")

    def test_params_write_back_into_the_url_bar_live(self) -> None:
        """参数变化实时写回 URL 栏（用户编辑路径：changed 信号）。"""
        self.page.url_edit.setText("https://api.example.com/v1?keep=1")
        self.page.params_card.set_items([("page", "2")])
        self.page.params_card.changed.emit()
        self.assertEqual(
            self.page.url_edit.text(), "https://api.example.com/v1?page=2"
        )

    def test_clearing_params_does_not_touch_the_url_bar(self) -> None:
        """「删光参数」和「还没填」是同一个空列表信号 —— URL 栏原样不动。"""
        self.page.url_edit.setText("https://api.example.com/v1?keep=1")
        self.page.params_card.set_items([])
        self.page.params_card.changed.emit()
        self.assertEqual(self.page.url_edit.text(), "https://api.example.com/v1?keep=1")

    def test_default_ports_are_normalized_away(self) -> None:
        """跨协议端口是笔误（明文打 HTTPS 端口、反之亦然），还原为默认端口；
        本协议的显式端口（含 https:443 本尊、8080）原样保留。"""
        self.page.url_edit.setText("http://api.example.com:443/v1")
        self.assertEqual(self.page._collect_url(), "http://api.example.com/v1")
        self.page.url_edit.setText("https://api.example.com:80/v1")
        self.assertEqual(self.page._collect_url(), "https://api.example.com/v1")
        self.page.url_edit.setText("https://api.example.com:443/v1")
        self.assertEqual(self.page._collect_url(), "https://api.example.com:443/v1")
        self.page.url_edit.setText("http://api.example.com:8080/v1")
        self.assertEqual(self.page._collect_url(), "http://api.example.com:8080/v1")

    def test_send_uses_the_merged_url_and_strips_blank_headers(self) -> None:
        self.page.url_edit.setText("https://api.example.com/v1?keep=1")
        self.page.params_card.set_items([("page", "2")])
        self.page.headers_card.set_items([("token", "t"), ("", "x")])
        self.page.body_panel.set_text('{"a": 1}')
        self.page._on_send()

        call = self.controller.calls[0]
        self.assertEqual(call[1], "GET")
        self.assertEqual(call[2], "https://api.example.com/v1?page=2")
        self.assertEqual(call[3], (("token", "t"),))
        self.assertEqual(call[4], '{"a": 1}')
        self.assertTrue(call[5])  # record 胶囊默认选中


class StatusFlowTests(ComposeInterfaceTestCase):
    def test_sending_disables_the_button_and_shows_a_hint(self) -> None:
        self.controller.sending_changed.emit(True)
        self.assertFalse(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.status_label.text(), "Sending…")
        self.assertTrue(self.page.status_badge.isHidden())

    def test_a_result_lands_in_the_status_line_and_the_response_pane(self) -> None:
        # 真实控制器的次序：先收「发送结束」，再发结果。
        self.controller.sending_changed.emit(True)
        self.controller.sending_changed.emit(False)
        self.controller.result_ready.emit(success_result())

        self.assertIs(
            self.page.response_stack.currentWidget(), self.page.response_pane
        )
        self.assertTrue(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.status_badge.text(), "200")
        self.assertEqual(self.page.status_badge.level, InfoLevel.SUCCESS)
        self.assertEqual(
            self.page.status_label.text(), "200 OK · 12 ms · 1.2k · 1.2.3.4:443"
        )

    def test_the_performance_page_carries_the_timing_and_traffic_keys(self) -> None:
        """性能页的数据卡直接吃详情字典：时间/流量两组只显示字典里真有的键。"""
        result = success_result()
        self.controller.result_ready.emit(result)
        self.page.response_pane.setCurrentTab("Perf")

        cards = self.page.perf_overview.visible_cards()
        titles = [card.section.title for card in cards]
        self.assertIn("Time", titles)
        self.assertIn("Traffic", titles)
        timing = next(card for card in cards if card.section.title == "Time")
        rows = {(row.label, row.value) for row in timing.rows()}
        self.assertIn(("Flow ID", result.detail["Flow ID"]), rows)
        self.assertIn(("Total duration", "12 ms"), rows)

    def test_an_error_result_paints_the_badge_red(self) -> None:
        self.controller.result_ready.emit(
            ComposeResult(
                flow_id="flow-1", error="boom", detail={"Status Code": "Error"}
            )
        )
        self.assertEqual(self.page.status_badge.text(), "Error")
        self.assertEqual(self.page.status_badge.level, InfoLevel.ERROR)

    def test_a_send_failure_shows_the_message_in_the_performance_page(self) -> None:
        """还没进队列就失败（URL 非法等）：没有 result 可等，性能页状态行直接背锅。"""
        self.controller.send_failed.emit("Send failed", "The URL is empty")
        self.assertEqual(self.page.status_badge.text(), "Error")
        self.assertEqual(self.page.status_badge.level, InfoLevel.ERROR)
        self.assertEqual(self.page.status_label.text(), "The URL is empty")

    def test_an_empty_url_never_reaches_the_controller(self) -> None:
        self.page.url_edit.setText("   ")
        self.page._on_send()
        self.assertEqual(self.controller.calls, [])


if __name__ == "__main__":
    unittest.main()
