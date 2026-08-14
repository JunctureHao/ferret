"""Auto-save coordinator: drives mitmproxy Save + Session lifecycle from a setting."""

from typing import Any

from PySide6.QtCore import QObject, QTimer, Slot

from ferret.apps.capture.controllers import CaptureController
from ferret.apps.session.controllers import SessionController
from ferret.apps.session.models import RecordingState
from ferret.core.log import get_logger
from ferret.core.settings import CONFIG

log = get_logger("auto-save")


class CaptureAutoSaveCoordinator(QObject):
    """协调配置开关、代理就绪与 Session 录制/提交的唯一入口。

    只做业务编排：根据 CONFIG.auto_save_sessions 和代理就绪状态决定是否
    启动一轮录制；录制 handle 准备好后驱动 mitmproxy 原生 Save 写入
    .flow.recording；停止时先关闭 Save 再提交 Session。不写 flow，不生成
    SessionMeta。
    """

    def __init__(
        self,
        capture_controller: CaptureController,
        session_controller: SessionController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._capture = capture_controller
        self._sessions = session_controller
        self._proxy_ready = False
        self._start_requested = False
        self._save_active = False
        self._start_blocked = False
        self._stop_retry_scheduled = False
        self._shutting_down = False

        self._capture.proxy_started.connect(self._on_proxy_started)
        self._capture.captureStateChanged.connect(self._on_capture_state_changed)
        self._sessions.recording_started.connect(self._on_recording_started)
        self._sessions.recording_state_changed.connect(self._on_recording_state_changed)
        CONFIG.auto_save_sessions.valueChanged.connect(self._on_config_changed)

    def _is_desired(self) -> bool:
        return (
            not self._shutting_down
            and bool(CONFIG.get(CONFIG.auto_save_sessions))
            and self._proxy_ready
            and self._capture.is_capturing
        )

    def _reconcile(self) -> None:
        desired = self._is_desired()
        state = self._sessions.recording_state

        if desired:
            if (
                state == RecordingState.IDLE
                and not self._start_requested
                and not self._start_blocked
            ):
                self._start_requested = True
                self._sessions.start_recording()
            return

        self._start_requested = False

        if self._save_active:
            if not self._capture.stop_save_recording():
                log.error("关闭 mitmproxy Save 超时或失败，将延迟重试")
                self._schedule_stop_retry()
                return
            self._save_active = False
            self._stop_retry_scheduled = False

        if state in (RecordingState.STARTING, RecordingState.RECORDING):
            self._sessions.stop_recording()

    def _schedule_stop_retry(self) -> None:
        if self._stop_retry_scheduled or self._shutting_down:
            return
        self._stop_retry_scheduled = True
        QTimer.singleShot(1000, self._retry_stop)

    @Slot()
    def _retry_stop(self) -> None:
        self._stop_retry_scheduled = False
        self._reconcile()

    @Slot()
    def _on_proxy_started(self) -> None:
        self._proxy_ready = True
        self._start_blocked = False
        self._reconcile()

    @Slot(bool)
    def _on_capture_state_changed(self, capturing: bool) -> None:
        if not capturing:
            self._proxy_ready = False
            self._save_active = False
            self._start_blocked = False
            self._stop_retry_scheduled = False
        self._reconcile()

    @Slot()
    def _on_config_changed(self) -> None:
        self._start_blocked = False
        self._reconcile()

    @Slot(object)
    def _on_recording_started(self, handle: Any) -> None:
        self._start_requested = False

        if not self._is_desired():
            self._sessions.stop_recording()
            return

        if not self._capture.start_save_recording(str(handle.flow_path)):
            log.error("启动 mitmproxy Save 失败: %s", handle.flow_path)
            self._start_blocked = True
            self._sessions.stop_recording()
            return

        self._save_active = True

    @Slot(object)
    def _on_recording_state_changed(self, state: object) -> None:
        try:
            recording_state = RecordingState(state)
        except ValueError:
            return

        if recording_state != RecordingState.STARTING:
            self._start_requested = False

        if recording_state == RecordingState.IDLE:
            self._save_active = False
            QTimer.singleShot(0, self._reconcile)

    def shutdown(self) -> None:
        self._shutting_down = True
        self._proxy_ready = False
        self._start_requested = False
        self._stop_retry_scheduled = False

        if self._save_active and self._capture.stop_save_recording():
            self._save_active = False

        if not self._save_active and self._sessions.recording_state in (
            RecordingState.STARTING,
            RecordingState.RECORDING,
        ):
            self._sessions.stop_recording()
