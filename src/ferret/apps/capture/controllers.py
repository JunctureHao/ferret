"""Traffic page controller over the application-scoped mitmproxy runtime."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ferret.apps.capture.services import compile_filter
from ferret.core.log import get_logger
from ferret.core.mitm import (
    HTTPFlow,
    MitmFacade,
    MitmRuntime,
    MitmRuntimeState,
    View,
)
from ferret.core.system_proxy import SystemProxyService

log = get_logger("mitmproxy")


class CaptureState(StrEnum):
    """System traffic attachment state exposed to the traffic page."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class CaptureController(QObject):
    """Coordinate system proxy attachment; the mitmproxy runtime stays alive."""

    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    master_ready = Signal(object)

    captureStateChanged = Signal(bool)
    proxy_started = Signal()
    proxy_failed = Signal(str)
    capture_state_changed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        mitm: MitmFacade | None = None,
        system_proxy: SystemProxyService | None = None,
    ) -> None:
        super().__init__(parent)
        if mitm is None:
            runtime = MitmRuntime(self)
            mitm = MitmFacade(runtime)
            self._owned_runtime: MitmRuntime | None = runtime
        else:
            runtime = mitm.runtime
            self._owned_runtime = None

        self._mitm = mitm
        self._runtime = runtime
        self._system_proxy = system_proxy or SystemProxyService()
        self._capture_state = CaptureState.STOPPED
        self._last_error = ""
        self._pending_attach = False

        runtime.flow_added.connect(self.flow_added)
        runtime.flow_updated.connect(self.flow_updated)
        runtime.flow_removed.connect(self.flow_removed)
        runtime.view_refreshed.connect(self.view_refreshed)
        runtime.ready.connect(self._on_runtime_ready)
        runtime.failed.connect(self._on_runtime_failed)
        runtime.stopped.connect(self._on_runtime_stopped)

        QTimer.singleShot(0, lambda: self.master_ready.emit(self._mitm.view))

    @property
    def is_capturing(self) -> bool:
        return self._capture_state in (
            CaptureState.STARTING,
            CaptureState.RUNNING,
            CaptureState.STOPPING,
        )

    @property
    def capture_state(self) -> CaptureState:
        return self._capture_state

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def current_port(self) -> int:
        return self._mitm.listen_port

    @property
    def view(self) -> View:
        return self._mitm.view

    def start_capture(self, port: int | None = None) -> None:
        if self._capture_state in (CaptureState.STARTING, CaptureState.RUNNING):
            return
        if port is not None and port != self.current_port:
            if self._runtime.is_running:
                self._runtime.restart(listen_port=port)
            else:
                self._runtime.listen_port = port

        self._last_error = ""
        self._pending_attach = True
        self._set_capture_state(CaptureState.STARTING)

        if self._runtime.is_running:
            self._attach_system_proxy()
            return
        if self._runtime.state in (MitmRuntimeState.STOPPED, MitmRuntimeState.FAILED):
            self._runtime.start()

    def stop_capture(self) -> None:
        self._pending_attach = False
        if self._capture_state == CaptureState.STOPPED:
            return
        self._set_capture_state(CaptureState.STOPPING)
        detach_ok = self._system_proxy.detach()
        if not detach_ok:
            self._last_error = "恢复原系统代理失败"
            self._set_capture_state(CaptureState.FAILED)
            self.captureStateChanged.emit(False)
            return
        try:
            self._mitm.stop_capture_recording()
        except Exception:
            log.exception("failed to stop capture recording")
        self._set_capture_state(CaptureState.STOPPED)
        self.captureStateChanged.emit(False)

    def shutdown(self) -> None:
        self.stop_capture()
        if self._owned_runtime is not None:
            self._owned_runtime.stop()

    def update_port(self, new_port: int) -> None:
        if new_port == self.current_port:
            return
        was_capturing = self._capture_state == CaptureState.RUNNING
        if was_capturing:
            self.stop_capture()
        self._runtime.restart(listen_port=new_port)
        if was_capturing:
            self._pending_attach = True
            self._set_capture_state(CaptureState.STARTING)

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        return self._mitm.get_flow(flow_id)

    def total_count(self) -> int:
        return self._mitm.total_count()

    def all_http_flows(self) -> list[HTTPFlow]:
        return self._mitm.all_http_flows()

    def apply_filter(self, conditions: list[dict] | None = None) -> None:
        self._mitm.set_filter(compile_filter(conditions))

    def save_flows(self, flows: list[HTTPFlow], path: str) -> int:
        return self._mitm.save_flows(flows, path)

    def get_httpie_command(self, flow_id: str) -> str:
        return self._mitm.get_httpie_command(flow_id)

    def get_raw_request(self, flow_id: str) -> bytes:
        return self._mitm.get_raw_request(flow_id)

    def get_raw_response(self, flow_id: str) -> bytes:
        return self._mitm.get_raw_response(flow_id)

    def get_raw_flow(self, flow_id: str) -> bytes:
        return self._mitm.get_raw_flow(flow_id)

    def export_har(self, flows: list[HTTPFlow], path: str) -> None:
        self._mitm.export_har(flows, path)

    def replay_flow(self, flow_id: str) -> None:
        self._mitm.replay_flow(flow_id)

    def replay_flows(self, flows: list[HTTPFlow]) -> None:
        self._mitm.replay_flows(flows)

    def load_replay_file(self, path: Path | str) -> None:
        self._mitm.replay_file(path)

    def load_flow_file(self, path: Path | str) -> int:
        return self._mitm.load_flow_file(path)

    def clear_flows(self) -> None:
        self._mitm.clear_flows()

    def remove_flows(self, flows: list[HTTPFlow]) -> None:
        self._mitm.remove_flows(flows)

    def toggle_capture(self) -> bool:
        if self._capture_state == CaptureState.RUNNING:
            self.stop_capture()
            return False
        if self._capture_state in (CaptureState.STOPPED, CaptureState.FAILED):
            self.start_capture()
        return self.is_capturing

    def _attach_system_proxy(self) -> None:
        if not self._pending_attach or not self._runtime.is_running:
            return
        try:
            self._mitm.start_capture_recording()
            self._system_proxy.attach(self._mitm.listen_host, self._mitm.listen_port)
        except Exception as exc:  # noqa: BLE001
            with_recording = self._mitm.runtime.is_running
            if with_recording:
                try:
                    self._mitm.stop_capture_recording()
                except Exception:
                    log.exception("failed to roll back capture recording")
            self._pending_attach = False
            self._last_error = str(exc)
            self._set_capture_state(CaptureState.FAILED)
            self.proxy_failed.emit(str(exc))
            self.captureStateChanged.emit(False)
            return

        self._pending_attach = False
        self._set_capture_state(CaptureState.RUNNING)
        self.proxy_started.emit()
        self.captureStateChanged.emit(True)

    def _set_capture_state(self, state: CaptureState) -> None:
        if state == self._capture_state:
            return
        self._capture_state = state
        self.capture_state_changed.emit(state)

    def _on_runtime_ready(self, _view: View) -> None:
        self._attach_system_proxy()

    def _on_runtime_failed(self, message: str) -> None:
        self._pending_attach = False
        self._system_proxy.detach()
        self._last_error = message
        self._set_capture_state(CaptureState.FAILED)
        self.proxy_failed.emit(message)
        self.captureStateChanged.emit(False)

    def _on_runtime_stopped(self) -> None:
        if self._runtime.state == MitmRuntimeState.STOPPED and self._capture_state in (
            CaptureState.STARTING,
            CaptureState.RUNNING,
        ):
            self._on_runtime_failed("mitmproxy 内核已停止")

