"""Traffic page controller over the application-scoped mitmproxy runtime."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
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
    RequestEdit,
    SseEvent,
    View,
    WsClose,
    WsFrame,
)
from ferret.core.settings import CONFIG, get_config_dir
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

log = get_logger("mitmproxy")

# sysproxy 包按「库不管展示」的约定只抛英文常量；捕获页在展示边界把它们译成
# 当前语言。键是包的对账常量，值是 QT_TRANSLATE_NOOP 标记 —— 模块级不许直接
# translate（AGENTS.md §7），用点经 `resolve_marker` 求值。
_SYSTEM_PROXY_ERRORS = {
    ERR_INVALID_ADDRESS: QT_TRANSLATE_NOOP("CaptureController", "无效的系统代理地址"),
    ERR_RESTORE_FAILED: QT_TRANSLATE_NOOP("CaptureController", "恢复原系统代理失败"),
    ERR_SET_FAILED: QT_TRANSLATE_NOOP("CaptureController", "设置系统代理失败"),
}

# local / wireguard 通道的启动失败没有异常类型可对账（原生 proxyserver 把实例
# 启动错误记日志吞掉，界面侧只能经 channel_health 读 last_exception 原文），
# 所以按特征词映射；都兜不住就原样展示技术串。
_CHANNEL_ERROR_MARKERS = (
    (
        "as administrator",
        QT_TRANSLATE_NOOP(
            "CaptureController",
            "本地重定向需要管理员授权（UAC）",
        ),
    ),
    (
        "spawn more than one local redirector",
        QT_TRANSLATE_NOOP("CaptureController", "本地重定向已在运行"),
    ),
    (
        "wireguard",
        QT_TRANSLATE_NOOP("CaptureController", "WireGuard 隧道启动失败"),
    ),
)


class CaptureState(StrEnum):
    """System traffic attachment state exposed to the traffic page."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class CaptureController(QObject):
    """Coordinate capture channels, the system proxy and the write gate.

    语义（AGENTS.md §5 的现行决策）：应用启动零抓包动作 —— 内核 regular 空转、
    系统代理不挂、写入闸门关。「开始抓包」= 把启用的通道（本地重定向 / WireGuard）
    热更进内核 mode 列表 + 挂系统代理（按勾选）+ 开写入闸门；「停止」整体回落。
    local 的提权守护进程被上游常驻复用，停止只是清掉截流配置，重开不再弹 UAC。
    """

    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()

    # WebSocket 三件事，载荷是 `(flow_id, 值对象)`。原样从 runtime 转过来，理由见
    # `UiBridgeAddon.websocket_message`：钩子在 mitm 线程上跑，过界的只能是值对象。
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)
    # SSE 同形：`FerretSseAddon` 的 tee 在 mitm 线程上解析，过界的只有值对象。
    sse_started = Signal(str)
    sse_event = Signal(str, object)
    sse_ended = Signal(str)

    captureStateChanged = Signal(bool)
    proxy_started = Signal()
    proxy_failed = Signal(str)
    capture_state_changed = Signal(object)
    # 写入闸门与通道状态：流量表/命令栏据此显示「抓包中 / 已停止」与通道摘要。
    recordingChanged = Signal(bool)
    channels_changed = Signal()

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
        # 写入闸门：关着的时候新 flow 不进流量表（通道照常转发）。默认关 ——
        # 应用打开是「已停止」态，点开始抓包才开。
        self._recording = False
        # 系统代理注册表当前是否由我们挂着（attach 成功 / detach 落下）。
        self._sysproxy_attached = False
        # 通道健康检查（异步启动失败只能延迟读 channel_health）的最近结果。
        self._channel_errors: dict[str, str] = {}

        runtime.flow_added.connect(self._on_flow_added)
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
        runtime.sse_started.connect(self.sse_started)
        runtime.sse_event.connect(self.sse_event)
        runtime.sse_ended.connect(self.sse_ended)
        runtime.ready.connect(self._on_runtime_ready)
        runtime.failed.connect(self._on_runtime_failed)
        runtime.stopped.connect(self._on_runtime_stopped)

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
    def recording(self) -> bool:
        """写入闸门。开着时新 flow 才进流量表；与系统代理挂载是两回事。"""
        return self._recording

    @property
    def channel_errors(self) -> dict[str, str]:
        """最近一次健康检查发现的通道错误（键 `local` / `wireguard`）。"""
        return dict(self._channel_errors)

    @property
    def use_local(self) -> bool:
        return self._mitm.use_local

    @property
    def local_spec(self) -> str:
        return self._mitm.local_spec

    @property
    def use_wireguard(self) -> bool:
        return self._mitm.use_wireguard

    @property
    def use_reverse(self) -> bool:
        return self._mitm.use_reverse

    @property
    def reverse_target(self) -> str:
        return self._mitm.reverse_target

    @property
    def reverse_port(self) -> int:
        return self._mitm.reverse_port

    @property
    def use_upstream(self) -> bool:
        return self._mitm.use_upstream

    @property
    def upstream_target(self) -> str:
        return self._mitm.upstream_target

    @property
    def upstream_username(self) -> str:
        return self._mitm.upstream_username

    @property
    def upstream_password(self) -> str:
        return self._mitm.upstream_password

    def upstream_targets_self(
        self, target: str, *, listen_host: str, listen_port: int
    ) -> bool:
        """上游地址是否指回 ferret 自己的监听口（对话框提交前的自环前置校验）。

        监听地址端口由调用方传：用户可能在同一个对话框里同时改监听口，判据得用
        **待提交的**那一组。目标非法时抛 ``ValueError``（已译）。
        """
        return self._mitm.upstream_targets_self(
            target, listen_host=listen_host, listen_port=listen_port
        )

    def system_proxy_enabled(self) -> bool:
        """「开始抓包」时是否挂系统代理（对话框勾选的持久化偏好）。"""
        return bool(CONFIG.get(CONFIG.system_proxy_enabled))

    def wireguard_client_config(self) -> str:
        """WireGuard 客户端配置文本（隧道启动过才有，否则抛 FileNotFoundError）。"""
        return self._mitm.wireguard_client_config()

    def start_capture(self, port: int | None = None) -> None:
        """Open the capture session: channels + system proxy + write gate.

        任何一步失败都以 FAILED 收场并回滚已生效的步骤 —— 「半开」状态比抓不到包
        更难排查。启动失败的具体通道文案进 ``last_error``，其余通道随整体回落。
        """
        if self._capture_state in (CaptureState.STARTING, CaptureState.RUNNING):
            return
        if port is not None and port != self.current_port:
            if self._runtime.is_running:
                self._runtime.restart(listen_port=port)
            else:
                self._runtime.listen_port = port

        self._last_error = ""
        self._channel_errors = {}
        self._pending_attach = True
        self._set_capture_state(CaptureState.STARTING)

        # 通道先行：mode 热更失败（坏过滤串、内核拒绝）说明会话开不起来，整体回落。
        try:
            self._mitm.engage_channels()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            self._fail_start(self._translate_channel_error(str(exc)))
            return
        # 内核还没跑就把监听也拉起来：ready 信号会回来调 _on_runtime_ready → attach。
        if not self._runtime.is_running:
            if self._runtime.state in (
                MitmRuntimeState.STOPPED,
                MitmRuntimeState.FAILED,
            ):
                self._runtime.start()
            return
        self._attach_system_proxy()

    def stop_capture(self) -> None:
        """Close the capture session: detach proxy, drop channels, close the gate.

        内核继续以 regular 空转（compose / 详情页依赖它活着）；已入表的行保留。
        """
        self._pending_attach = False
        if self._capture_state == CaptureState.STOPPED:
            return
        self._set_capture_state(CaptureState.STOPPING)
        if self._sysproxy_attached:
            detach_ok = self._system_proxy.detach()
            self._sysproxy_attached = False
            if not detach_ok:
                self._last_error = self.tr("恢复原系统代理失败")
                self._set_capture_state(CaptureState.FAILED)
                self.captureStateChanged.emit(False)
                return
        try:
            self._mitm.stop_capture_recording()
        except Exception:
            log.exception("failed to stop capture recording")
        self._set_recording(False)
        # 通道回落：OS 级截流停止（上游只清截流配置，守护进程驻留 → 重开免 UAC）。
        # 只动接通位，use_local/use_wireguard 意图值原样保留。
        try:
            self._mitm.disengage_channels()
        except (RuntimeError, TimeoutError, ValueError):
            log.exception("failed to drop capture channels")
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
            # stop_capture 把会话接通位落下了；重启前先抬回去，内核才会在启动的
            # Options 里带上通道（ready 之后 _attach_system_proxy 只补挂代理）。
            try:
                self._mitm.engage_channels()
            except (RuntimeError, TimeoutError, ValueError):
                log.exception("failed to re-engage channels before restart")
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

    def request_edit(self, flow_id: str) -> RequestEdit:
        return self._mitm.request_edit(flow_id)

    def websocket_frames(self, flow_id: str) -> list[WsFrame]:
        return self._mitm.websocket_frames(flow_id)

    def websocket_close(self, flow_id: str) -> WsClose:
        return self._mitm.websocket_close(flow_id)

    def sse_events(self, flow_id: str) -> list[SseEvent]:
        return self._mitm.sse_events(flow_id)

    def total_count(self) -> int:
        return self._mitm.total_count()

    def visible_http_flows(self) -> list[HTTPFlow]:
        return self._mitm.visible_http_flows()

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

    def get_request_body(self, flow_id: str) -> bytes:
        return self._mitm.get_request_body(flow_id)

    def get_response_body(self, flow_id: str) -> bytes:
        return self._mitm.get_response_body(flow_id)

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
        """Arm the capture session once the kernel is up: attach proxy, open the gate.

        通道接通已在 ``start_capture`` / 重挂路径里完成（boot 时就带着 mode 列表），
        这里负责剩下的两步：按勾选挂系统代理、开写入闸门。挂代理失败不撤通道 ——
        本地重定向 / WireGuard 与注册表互不相干，会话保持 FAILED 让用户重试。
        """
        if not self._pending_attach or not self._runtime.is_running:
            return
        if self.system_proxy_enabled():
            try:
                self._mitm.start_capture_recording()
                # 必须是环回，不是 listen_host：绑定 0.0.0.0 时把 `0.0.0.0:8080` 写进
                # 系统代理，Windows 会拿它当目标地址去连，抓包会整体失效。local 的
                # WinDivert 过滤器放行全部环回流量，所以这个地址也保证不会被
                # 本地重定向二次截走。
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
                    _SYSTEM_PROXY_ERRORS,
                    str(exc),
                    "CaptureController",
                    fallback=str(exc),
                )
                self._last_error = message
                self._set_capture_state(CaptureState.FAILED)
                self.proxy_failed.emit(message)
                self.captureStateChanged.emit(False)
                return
            self._sysproxy_attached = True

        self._pending_attach = False
        self._set_recording(True)
        self._set_capture_state(CaptureState.RUNNING)
        self.proxy_started.emit()
        self.captureStateChanged.emit(True)
        self._schedule_channel_check()

    def _set_recording(self, recording: bool) -> None:
        if recording == self._recording:
            return
        self._recording = recording
        self.recordingChanged.emit(recording)

    def _fail_start(self, message: str) -> None:
        self._pending_attach = False
        self._last_error = message
        self._set_capture_state(CaptureState.FAILED)
        self.proxy_failed.emit(message)
        self.captureStateChanged.emit(False)

    def update_channels(
        self,
        *,
        use_system_proxy: bool,
        use_local: bool,
        local_spec: str,
        use_wireguard: bool,
        use_reverse: bool = False,
        reverse_target: str = "",
        reverse_port: int = 8081,
        use_upstream: bool = False,
        upstream_target: str = "",
        upstream_username: str = "",
        upstream_password: str = "",
    ) -> None:
        """Commit the capture-channels dialog: persist, then hot-apply what is live.

        未抓包时只落盘 + 更新内核意图值，下次「开始抓包」按新配置开会话；抓包中
        则实时增删通道、按需挂/摘系统代理。
        """
        # 校验先行：坏过滤串连落盘都不该发生（否则坏串会一直躺在配置里）。
        # 仅在开启本地重定向时校验——关闭通道不该被残留过滤串卡住（关的动作
        # 本身就值得放行；开启时 apply_channels 还会再过一遍原生解析器）。
        if use_local:
            self._mitm.validate_local_spec(local_spec)
        # 上游地址同款「关的动作放行」：只在开启时过原生解析器，关闭时哪怕地址
        # 是历史坏值也不该卡住提交。自环那一道在对话框里（它要知道待提交的监听
        # 口，见 apps/capture/views.py::__show_proxy_port_dialog）。
        if use_upstream:
            self._mitm.validate_upstream_target(upstream_target)
        CONFIG.set(CONFIG.system_proxy_enabled, use_system_proxy)
        CONFIG.set(CONFIG.local_enabled, use_local)
        CONFIG.set(CONFIG.local_spec, local_spec)
        CONFIG.set(CONFIG.wireguard_enabled, use_wireguard)
        # reverse 三参落盘：reverse 与 regular 共用 listen_host，端口由对话框
        # 前置校验保证错开（apps/capture/views.py::__show_proxy_port_dialog
        # 的 try 块之前），这里只走 CONFIG 持久化与 MitmFacade.set_channels。
        CONFIG.set(CONFIG.reverse_enabled, use_reverse)
        CONFIG.set(CONFIG.reverse_target, reverse_target)
        CONFIG.set(CONFIG.reverse_port, reverse_port)
        # 上游代理四参落盘：它替换的是 regular 槽位（不是第五条通道），凭证明文
        # 落盘，语义见 core/settings.py 的注释。
        CONFIG.set(CONFIG.upstream_enabled, use_upstream)
        CONFIG.set(CONFIG.upstream_target, upstream_target)
        CONFIG.set(CONFIG.upstream_username, upstream_username)
        CONFIG.set(CONFIG.upstream_password, upstream_password)
        # 提交意图：抓包中会顺带热更 mode 列表与 block 联动（runtime 内部处理）。
        self._mitm.set_channels(
            use_local=use_local,
            local_spec=local_spec,
            use_wireguard=use_wireguard,
            use_reverse=use_reverse,
            reverse_target=reverse_target,
            reverse_port=reverse_port,
            use_upstream=use_upstream,
            upstream_target=upstream_target,
            upstream_username=upstream_username,
            upstream_password=upstream_password,
        )
        self.channels_changed.emit()

        if self._capture_state != CaptureState.RUNNING:
            return
        want_proxy = use_system_proxy
        if want_proxy and not self._sysproxy_attached:
            self._pending_attach = True
            self._attach_system_proxy()
        elif not want_proxy and self._sysproxy_attached:
            detach_ok = self._system_proxy.detach()
            self._sysproxy_attached = False
            if not detach_ok:
                self._last_error = self.tr("恢复原系统代理失败")
                self._set_capture_state(CaptureState.FAILED)
                self.captureStateChanged.emit(False)
                return
        self._schedule_channel_check()

    def _translate_channel_error(self, raw: str) -> str:
        """把通道启动失败的技术串映射成可展示文案；兜不住就原样返回。"""
        lowered = raw.lower()
        for marker, marked in _CHANNEL_ERROR_MARKERS:
            if marker in lowered:
                return QCoreApplication.translate("CaptureController", marked)
        return raw

    def _schedule_channel_check(self) -> None:
        """通道实例是异步启动的（UAC 弹窗可能挂起数秒），延迟一轮再查健康。"""
        QTimer.singleShot(1500, self._check_channel_health)

    def _check_channel_health(self) -> None:
        if self._capture_state != CaptureState.RUNNING or not self._runtime.is_running:
            return
        try:
            health = self._mitm.channel_health()
        except Exception:
            log.exception("failed to inspect channel health")
            return
        errors = {
            key: self._translate_channel_error(value)
            for key, value in health.items()
            if isinstance(value, str)
        }
        if errors == self._channel_errors:
            return
        self._channel_errors = errors
        self.channels_changed.emit()

    def _set_capture_state(self, state: CaptureState) -> None:
        if state == self._capture_state:
            return
        self._capture_state = state
        self.capture_state_changed.emit(state)

    def _on_runtime_ready(self, _view: View) -> None:
        self._attach_system_proxy()

    def _on_runtime_failed(self, message: str) -> None:
        self._pending_attach = False
        if self._sysproxy_attached:
            self._system_proxy.detach()
            self._sysproxy_attached = False
        self._set_recording(False)
        self._last_error = message
        self._set_capture_state(CaptureState.FAILED)
        self.proxy_failed.emit(message)
        self.captureStateChanged.emit(False)

    def _on_runtime_stopped(self) -> None:
        if self._runtime.state == MitmRuntimeState.STOPPED and self._capture_state in (
            CaptureState.STARTING,
            CaptureState.RUNNING,
        ):
            self._on_runtime_failed(self.tr("mitmproxy 内核已停止"))

    def _on_flow_added(self, flow: object) -> None:
        """写入闸门：闸门关着时新 flow 不进流量表。

        只挡新增：已有行的更新（响应到达、拦截标记等）照常转发 —— 表里已存在的
        内容永远保持鲜活，暂停语义是「不进新行」而不是「冻结整张表」。compose /
        手工请求的结果同样从这条路走，一并受闸门约束。
        """
        if not self._recording:
            return
        self.flow_added.emit(flow)

    def set_flow_comment(self, flow_id: str, comment: str) -> None:
        self._mitm.set_flow_comment(flow_id, comment)

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        self._mitm.set_flow_marked(flow_id, marked)
