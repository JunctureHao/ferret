"""ComposeController 状态机：在途握手与内核停机/重启的兜底解锁。

发送链路的端到端归 `tests/core/mitm/test_compose.py`；这里只用假 facade /
假 runtime 驱信号，验证控制器对 `_inflight_id` 的管理。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.compose.controllers import ComposeController
from ferret.core.mitm import ComposeResult, MitmRuntimeState

app = QApplication.instance() or QApplication([])


class FakeRuntime(QObject):
    """只暴露控制器订阅的两条信号。"""

    compose_result = Signal(object)
    state_changed = Signal(object)


class FakeFacade:
    """最小 facade：`send_custom_request` 返回固定 flow id 并记录调用。"""

    def __init__(self, runtime: FakeRuntime, flow_id: str = "flow-1") -> None:
        self.runtime = runtime
        self._flow_id = flow_id
        self.sent: list[tuple] = []

    def send_custom_request(self, method, url, headers, content, *, record):
        self.sent.append((method, url, tuple(headers), content, record))
        return self._flow_id


def _result(flow_id: str, *, error: str = "") -> ComposeResult:
    return ComposeResult(flow_id=flow_id, error=error, detail={"Flow ID": flow_id})


class ComposeControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.facade = FakeFacade(self.runtime)
        self.controller = ComposeController(mitm=self.facade)  # ty: ignore[invalid-argument-type]
        self.addCleanup(self.controller.deleteLater)
        self.sending: list[bool] = []
        self.results: list[ComposeResult] = []
        self.failures: list[tuple[str, str]] = []
        self.controller.sending_changed.connect(self.sending.append)
        self.controller.result_ready.connect(self.results.append)
        self.controller.send_failed.connect(
            lambda title, content: self.failures.append((title, content))
        )

    def _send(self) -> None:
        self.controller.send("GET", "http://example.com/", [], b"", record=True)

    def test_send_marks_inflight_and_disables(self) -> None:
        self._send()
        self.assertTrue(self.controller.is_sending)
        self.assertEqual(self.sending, [True])
        self.assertEqual(len(self.facade.sent), 1)

    def test_matching_result_clears_inflight(self) -> None:
        self._send()
        self.runtime.compose_result.emit(_result("flow-1"))
        self.assertFalse(self.controller.is_sending)
        self.assertEqual(self.sending, [True, False])
        self.assertEqual(len(self.results), 1)

    def test_mismatched_result_is_ignored(self) -> None:
        """迟到的旧请求结果（flow id 不对）不解锁当前在途。"""
        self._send()
        self.runtime.compose_result.emit(_result("other"))
        self.assertTrue(self.controller.is_sending)
        self.assertEqual(self.sending, [True])
        self.assertEqual(self.results, [])

    def test_kernel_leaving_running_cancels_inflight(self) -> None:
        """核心回归：内核停机/重启时，即使终态结果永远不来，按钮也会解锁。"""
        self._send()
        self.runtime.state_changed.emit(MitmRuntimeState.STOPPING)
        self.assertFalse(self.controller.is_sending)
        self.assertEqual(self.sending, [True, False])
        self.assertEqual(self.failures, [("发送已取消", "内核已停止，本次发送已取消")])

    def test_failed_state_also_cancels_inflight(self) -> None:
        self._send()
        self.runtime.state_changed.emit(MitmRuntimeState.FAILED)
        self.assertFalse(self.controller.is_sending)
        self.assertEqual(len(self.failures), 1)

    def test_running_state_does_not_cancel(self) -> None:
        """「停止」只回落抓包、内核仍 RUNNING 供 Compose 用，不得误触发。"""
        self._send()
        self.runtime.state_changed.emit(MitmRuntimeState.RUNNING)
        self.assertTrue(self.controller.is_sending)
        self.assertEqual(self.failures, [])

    def test_state_change_without_inflight_is_noop(self) -> None:
        self.runtime.state_changed.emit(MitmRuntimeState.STOPPED)
        self.assertEqual(self.sending, [])
        self.assertEqual(self.failures, [])

    def test_late_result_after_cancel_is_discarded(self) -> None:
        """两条解锁幂等：state_changed 先清后，跨线程迟到的结果不重复弹窗。"""
        self._send()
        self.runtime.state_changed.emit(MitmRuntimeState.STOPPING)
        self.runtime.compose_result.emit(_result("flow-1"))
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.results, [])
        self.assertEqual(self.sending, [True, False])


if __name__ == "__main__":
    unittest.main()
