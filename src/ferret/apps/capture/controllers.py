"""Traffic page controller over the application-scoped mitmproxy runtime."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from sysproxy import (
    ERR_INVALID_ADDRESS,
    ERR_RESTORE_FAILED,
    ERR_SET_FAILED,
    SystemProxyService,
)

from ferret.apps.capture.services import compile_filter
from ferret.core.log import get_logger
from ferret.core.mitm import (
    HTTPFlow,
    MitmFacade,
    MitmRuntime,
    MitmRuntimeState,
    View,
    WsClose,
    WsFrame,
)
from ferret.core.settings import CONFIG, get_config_dir
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

log = get_logger("mitmproxy")

# sysproxy 包按「库不管展示」的约定只抛英文常量；捕获页在展示边界把它们译成
# 当前语言。键是包的对账常量，值是 QT_TRANSLATE_NOOP 标记 —— 模块级不许直接
# translate（AGENTS.md §8），用点经 `resolve_marker` 求值。
_SYSTEM_PROXY_ERRORS = {
    ERR_INVALID_ADDRESS: QT_TRANSLATE_NOOP(
        "CaptureController", "Invalid system proxy address"
    ),
    ERR_RESTORE_FAILED: QT_TRANSLATE_NOOP(
        "CaptureController", "Restoring the previous system proxy failed"
    ),
    ERR_SET_FAILED: QT_TRANSLATE_NOOP(
        "CaptureController", "Setting the system proxy failed"
    ),
}


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

    # WebSocket 三件事，载荷是 `(flow_id, 值对象)`。原样从 runtime 转过来，理由见
    # `UiBridgeAddon.websocket_message`：钩子在 mitm 线程上跑，过界的只能是值对象。
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)

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
        # 兜底构造仅用于无人注入的场景（测试等）；journal 同样落应用配置目录。
        self._system_proxy = system_proxy or SystemProxyService(
            journal_path=get_config_dir() / "system-proxy-state.json"
        )
        self._capture_state = CaptureState.STOPPED
        self._last_error = ""
        self._pending_attach = False

        runtime.flow_added.connect(self.flow_added)
        runtime.flow_updated.connect(self.flow_updated)
        # 挂起/放行也当成一次更新：网关挂起发生在 `request`，而 `View` 没有这个钩子，
        # 不借道 flow_updated 那一行的「挂起中」永远不上屏。
        runtime.flow_suspended.connect(self.flow_updated)
        # 断点同理：原生 `Intercept` 在 `request` / `response` 钩子里拦人，也不经过
        # `View` 的任何信号，不转一手的话流量表看不出这条正被钉着。
        runtime.flow_intercepted.connect(self.flow_updated)
        runtime.flow_removed.connect(self.flow_removed)
        runtime.view_refreshed.connect(self.view_refreshed)
        # 帧不借道 flow_updated：一条行情连接每秒几十帧，整行重绘纯属浪费，而消息页
        # 要的是「新到的这一帧」而不是「这条流量变了」。
        runtime.websocket_started.connect(self.websocket_started)
        runtime.websocket_frame.connect(self.websocket_frame)
        runtime.websocket_closed.connect(self.websocket_closed)
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
    def current_host(self) -> str:
        """绑定地址（可能是 `0.0.0.0`）。只用于回显设置，别拿它去连。"""
        return self._mitm.listen_host

    @property
    def local_endpoint(self) -> str:
        """本机客户端 / 系统代理该填的端点，恒为环回。"""
        return f"{self._mitm.local_client_host}:{self._mitm.listen_port}"

    @property
    def is_lan_exposed(self) -> bool:
        """当前是否允许局域网设备连进来。"""
        return self._mitm.is_lan_exposed

    def lan_address(self) -> str | None:
        """本机的局域网 IPv4 地址，**仅供显示 / 复制**；拿不到返回 None。"""
        return self._mitm.lan_address()

    @property
    def block_global(self) -> bool:
        return self._mitm.block_global

    @property
    def block_private(self) -> bool:
        return self._mitm.block_private

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
            self._last_error = self.tr("Failed to restore the original system proxy")
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
        """Change the listen port only; kept for callers that touch nothing else."""
        self.update_proxy_settings(listen_port=new_port)

    def update_proxy_settings(
        self,
        *,
        listen_host: str | None = None,
        listen_port: int | None = None,
        block_global: bool | None = None,
        block_private: bool | None = None,
    ) -> None:
        """Commit the proxy settings dialog in one shot and persist the result.

        两组设置的代价完全不同：来源过滤开关是热生效的（`options.update`），绑定地址
        和端口要重开监听 socket。所以先做热的那组 —— 它落地后再重启，重启失败也不会
        丢掉已经生效的开关；反过来则会留下「重启成功但开关没跟上」。
        """
        self._apply_block_options(
            block_global=block_global, block_private=block_private
        )
        self._apply_listen_endpoint(listen_host=listen_host, listen_port=listen_port)

    def _apply_block_options(
        self, *, block_global: bool | None, block_private: bool | None
    ) -> None:
        wanted_global = self.block_global if block_global is None else block_global
        wanted_private = self.block_private if block_private is None else block_private
        if (wanted_global, wanted_private) == (self.block_global, self.block_private):
            return
        self._mitm.set_block_options(
            block_global=wanted_global, block_private=wanted_private
        )
        CONFIG.set(CONFIG.block_global, wanted_global)
        CONFIG.set(CONFIG.block_private, wanted_private)

    def _apply_listen_endpoint(
        self, *, listen_host: str | None, listen_port: int | None
    ) -> None:
        wanted_host = self.current_host if listen_host is None else listen_host
        wanted_port = self.current_port if listen_port is None else listen_port
        if (wanted_host, wanted_port) == (self.current_host, self.current_port):
            return
        was_capturing = self._capture_state == CaptureState.RUNNING
        if was_capturing:
            self.stop_capture()
        self._runtime.restart(listen_host=wanted_host, listen_port=wanted_port)
        # 读回内核实际采纳的值：normalize_listen_host 可能把非法地址纠成环回。
        CONFIG.set(CONFIG.listen_host, self.current_host)
        CONFIG.set(CONFIG.listen_port, self.current_port)
        if was_capturing:
            self._pending_attach = True
            self._set_capture_state(CaptureState.STARTING)

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        return self._mitm.get_flow(flow_id)

    def flow_detail(self, flow_id: str) -> dict[str, Any]:
        return self._mitm.flow_detail(flow_id)

    def websocket_frames(self, flow_id: str) -> list[WsFrame]:
        return self._mitm.websocket_frames(flow_id)

    def websocket_close(self, flow_id: str) -> WsClose:
        return self._mitm.websocket_close(flow_id)

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
            # 必须是环回，不是 listen_host：绑定 0.0.0.0 时把 `0.0.0.0:8080` 写进
            # 系统代理，Windows 会拿它当目标地址去连，抓包会整体失效。
            self._system_proxy.attach(
                self._mitm.local_client_host, self._mitm.listen_port
            )
        except Exception as exc:  # noqa: BLE001
            with_recording = self._mitm.runtime.is_running
            if with_recording:
                try:
                    self._mitm.stop_capture_recording()
                except Exception:
                    log.exception("failed to roll back capture recording")
            self._pending_attach = False
            message = resolve_marker(
                _SYSTEM_PROXY_ERRORS, str(exc), "CaptureController", fallback=str(exc)
            )
            self._last_error = message
            self._set_capture_state(CaptureState.FAILED)
            self.proxy_failed.emit(message)
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
            self._on_runtime_failed(self.tr("The mitmproxy core has stopped"))

    def set_flow_comment(self, flow_id: str, comment: str) -> None:
        self._mitm.set_flow_comment(flow_id, comment)

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        self._mitm.set_flow_marked(flow_id, marked)
