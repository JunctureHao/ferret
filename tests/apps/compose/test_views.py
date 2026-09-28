"""compose 页测试：结构、URL 合并、性能页与状态流转。

覆盖页面结构、参数页与 URL 的同步规则、性能页的时间/流量卡片，以及
发送 → 结果 → 失败 时按钮、等待提示和响应内容的可见性。
发送链路本身归 `tests/core/mitm/test_compose.py`。
"""

from __future__ import annotations

import os
import unittest
from collections.abc import Sequence
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from qfluentwidgets import CheckBox, EditableComboBox, PrimaryPushButton

from ferret.apps.common.splitter import OrientationSplitter
from ferret.apps.compose.views import ComposeInterface
from ferret.core.mitm import ComposeResult, RequestEdit

app = QApplication.instance() or QApplication([])


class FakeController(QObject):
    """与真实 ComposeController 同信号面，send 记录调用。"""

    sending_changed = Signal(bool)
    result_ready = Signal(object)
    send_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple] = []

    def send(self, method: str, url: str, headers, body: bytes | str, *, record: bool):
        self.calls.append(("send", method, url, tuple(headers), body, record))


def success_result(**overrides) -> ComposeResult:
    flow = tflow.tflow(resp=True)
    detail = {
        "Flow ID": flow.id,
        "Status Code": 200,
        "Reason": "OK",
        "duration_ms": 12.0,
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
        self.assertIs(self.page.response_stack.currentWidget(), self.page.empty_hint)
        self.assertEqual(self.page.empty_title.text(), "暂无响应")
        self.assertTrue(self.page.empty_title.isVisibleTo(self.page))
        self.assertTrue(self.page.empty_subtitle.isVisibleTo(self.page))

    def test_the_send_button_is_a_primary_sized_action(self) -> None:
        """主按钮带文字，宽度兼顾发送中状态和翻译长度，高度随样式计算。"""
        self.assertIsInstance(self.page.send_btn, PrimaryPushButton)
        self.assertEqual(self.page.send_btn.text(), "发送")
        self.assertGreaterEqual(self.page.send_btn.minimumSize().width(), 96)
        self.assertEqual(
            self.page.send_btn.minimumSize().width(),
            self.page.send_btn.maximumSize().width(),
        )
        # 高度不锁死：只加宽，不变高。
        self.assertEqual(
            self.page.send_btn.maximumSize().height(),
            16777215,  # Qt 默认上限
        )
        self.assertTrue(self.page.send_btn.isEnabled())

    def test_the_method_combo_accepts_methods_outside_the_vocabulary(self) -> None:
        """prefill 可能带来词表外的方法（PROPFIND 等），必须显示得出来 —— 可编辑下拉。"""
        self.assertIs(type(self.page.method_combo), EditableComboBox)

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
        self.assertEqual(self.page.url_edit.text(), "https://api.example.com/v1?page=2")

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
        self.assertTrue(call[5])  # 默认记录流量

    def test_disabling_recording_is_passed_to_the_controller(self) -> None:
        self.assertIsInstance(self.page.record_btn, CheckBox)
        self.assertEqual(self.page.record_btn.text(), "记录流量")
        self.page.url_edit.setText("https://api.example.com/v1")
        self.page.record_btn.setChecked(False)
        self.page._on_send()

        self.assertFalse(self.controller.calls[0][5])


class StatusFlowTests(ComposeInterfaceTestCase):
    def test_sending_disables_the_button_and_shows_a_hint(self) -> None:
        initial_width = self.page.send_btn.width()
        self.controller.sending_changed.emit(True)
        self.assertFalse(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.send_btn.text(), "发送中…")
        self.assertEqual(self.page.send_btn.width(), initial_width)
        self.assertIs(self.page.response_stack.currentWidget(), self.page.empty_hint)
        self.assertEqual(self.page.empty_title.text(), "正在等待响应")
        self.assertTrue(self.page.empty_title.isVisibleTo(self.page))
        self.assertTrue(self.page.empty_icon_stack.isVisibleTo(self.page))
        self.assertTrue(self.page.loading_ring.isVisibleTo(self.page))

    def test_a_result_lands_in_the_response_pane_and_restores_the_button(self) -> None:
        # 真实控制器的次序：先收「发送结束」，再发结果。
        self.controller.sending_changed.emit(True)
        self.controller.sending_changed.emit(False)
        result = success_result()
        self.controller.result_ready.emit(result)

        self.assertIs(self.page.response_stack.currentWidget(), self.page.response_pane)
        self.assertEqual(self.page.response_pane.datas, result.detail)
        self.assertTrue(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.send_btn.text(), "发送")
        self.assertEqual(self.page.empty_title.text(), "暂无响应")
        self.assertFalse(self.page.loading_ring.isVisibleTo(self.page))

    def test_a_new_send_hides_the_previous_response(self) -> None:
        self.controller.result_ready.emit(success_result())
        self.page.response_pane.setCurrentTab("Body")
        self.assertTrue(self.page.response_pane.isVisibleTo(self.page))

        self.controller.sending_changed.emit(True)

        self.assertFalse(self.page.response_pane.isVisibleTo(self.page))
        self.assertTrue(self.page.empty_title.isVisibleTo(self.page))
        self.assertEqual(self.page.empty_title.text(), "正在等待响应")

    def test_the_performance_page_carries_the_timing_and_traffic_keys(self) -> None:
        """性能页的数据卡直接吃详情字典：时间/流量两组只显示字典里真有的键。"""
        result = success_result()
        self.controller.result_ready.emit(result)
        self.page.response_pane.setCurrentTab("Perf")

        cards = self.page.perf_overview.visible_cards()
        titles = [card.section.title for card in cards]
        self.assertIn("时间", titles)
        self.assertIn("流量", titles)
        timing = next(card for card in cards if card.section.title == "时间")
        rows = {(row.label, row.value) for row in timing.rows()}
        self.assertIn(("Flow ID", result.detail["Flow ID"]), rows)
        self.assertIn(("总耗时", "12 ms"), rows)

    def test_an_error_result_restores_the_button_and_reports_the_error(self) -> None:
        self.controller.sending_changed.emit(True)
        self.controller.sending_changed.emit(False)
        with patch("ferret.apps.compose.views.show_error") as show_error:
            self.controller.result_ready.emit(
                ComposeResult(
                    flow_id="flow-1", error="boom", detail={"Status Code": "Error"}
                )
            )
            show_error.assert_called_once_with("请求失败", "boom", self.page)

        self.assertTrue(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.send_btn.text(), "发送")
        self.assertFalse(self.page.loading_ring.isVisibleTo(self.page))

    def test_a_send_failure_hides_the_previous_response_and_reports_the_error(
        self,
    ) -> None:
        """入队失败没有 sending_changed 信号，仍要隐藏旧结果并展示当前错误。"""
        self.controller.result_ready.emit(success_result())
        self.page.response_pane.setCurrentTab("Body")
        with patch("ferret.apps.compose.views.show_error") as show_error:
            self.controller.send_failed.emit("Send failed", "The URL is empty")
            show_error.assert_called_once_with(
                "Send failed", "The URL is empty", self.page
            )

        self.assertFalse(self.page.response_pane.isVisibleTo(self.page))
        self.assertTrue(self.page.empty_title.isVisibleTo(self.page))
        self.assertTrue(self.page.send_btn.isEnabled())
        self.assertEqual(self.page.send_btn.text(), "发送")

    def test_an_empty_url_never_reaches_the_controller(self) -> None:
        self.page.url_edit.setText("   ")
        self.page._on_send()
        self.assertEqual(self.controller.calls, [])


class PrefillTests(ComposeInterfaceTestCase):
    """prefill 把一份 RequestEdit 灌进表单（右键「Edit in Compose」/ cURL 导入）。"""

    # 显式关键字默认值而不是 dict 合并 splat：`**{**fields, **overrides}` 的值
    # 类型混在一起，ty 没法对上构造器的各参数签名。
    @staticmethod
    def edit(
        *,
        method: str = "PROPFIND",
        url: str = "https://api.example.com/v1?page=2",
        headers: Sequence[tuple[str, str]] | None = None,
        content: bytes = b'{"a": 1}',
    ) -> RequestEdit:
        return RequestEdit(
            method=method,
            url=url,
            headers=(
                [
                    ("accept", "application/json"),
                    ("cookie", "a=1"),
                    ("cookie", "b=2"),
                ]
                if headers is None
                else list(headers)
            ),
            content=content,
        )

    def test_every_control_lands(self) -> None:
        self.page.prefill(self.edit())
        self.assertEqual(self.page.method_combo.currentText(), "PROPFIND")
        self.assertEqual(self.page.url_edit.text(), "https://api.example.com/v1?page=2")
        # query 拆进参数页（发送时参数页是权威源，再合并回 URL）。
        self.assertEqual(self.page.params_card.items(), [("page", "2")])
        self.assertEqual(
            self.page.headers_card.items(),
            [("accept", "application/json"), ("cookie", "a=1"), ("cookie", "b=2")],
        )
        self.assertEqual(self.page.body_panel.plain_text(), '{"a": 1}')
        self.assertFalse(self.page._binary)

    def test_a_binary_body_locks_the_editor_and_goes_out_as_bytes(self) -> None:
        """非 UTF-8 体：锁只读 + 提示条，发送时原始字节直通。"""
        payload = b"\x89PNG\r\n\x1a\n\xff"
        self.page.prefill(self.edit(content=payload))
        self.assertTrue(self.page._binary)
        self.assertTrue(self.page.body_panel.text.is_read_only())
        self.assertFalse(self.page.body_hint.isHidden())
        self.assertEqual(self.page.body_panel.plain_text(), "")

        self.page._on_send()
        self.assertEqual(self.controller.calls[0][4], payload)

    def test_a_text_prefill_unlocks_a_previously_binary_body(self) -> None:
        """上次灌了二进制、这次灌文本：锁必须解开，不然表单永远卡在只读。"""
        self.page.prefill(self.edit(content=b"\xff\xfe"))
        self.page.prefill(self.edit(content=b"hello"))
        self.assertFalse(self.page._binary)
        self.assertFalse(self.page.body_panel.text.is_read_only())
        self.assertTrue(self.page.body_hint.isHidden())
        self.assertEqual(self.page.body_panel.plain_text(), "hello")

    def test_the_response_area_is_reset(self) -> None:
        """旧结果不属于新表单：prefill 后回到空态。"""
        self.controller.result_ready.emit(success_result())
        self.assertIs(self.page.response_stack.currentWidget(), self.page.response_pane)
        self.page.prefill(self.edit())
        self.assertIs(self.page.response_stack.currentWidget(), self.page.empty_hint)
        self.assertEqual(self.page.empty_title.text(), "暂无响应")


if __name__ == "__main__":
    unittest.main()
