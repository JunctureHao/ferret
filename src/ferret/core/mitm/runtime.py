"""Application-scoped mitmproxy runtime and event-loop bridge."""

from __future__ import annotations

import asyncio
import inspect
import socket
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    HTTPFlow,
    LocalRedirectorInstance,
    Options,
    OptionsError,
    View,
    parse_filter,
)
from ferret.core.mitm.gateway import (
    GatewayRule,
    GatewayRuleSet,
    gateway_option_updates,
)
from ferret.core.mitm.intercept import InterceptRule, intercept_option_updates
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.modes import capture_mode_specs, validate_mode_specs
from ferret.core.mitm.rewrite import RewriteRule, RewriteRuleSet
from ferret.core.mitm.wsframe import latest_frame, ws_close
from ferret.core.network import LOOPBACK_HOST, normalize_listen_host
from ferret.core.settings import get_certs_dir

log = get_logger("mitmproxy")

# 固定会话（plans/sticky-session.md）：原生 StickyCookie / StickyAuth 两个 addon
# 的选项名。过滤串恒为「全量」、刻意不暴露匹配条件 —— 一旦可配就退化成规则表，
# 而它是「浏览器行为偏好」式的全局开关。开关状态不在这里：它只是 bool，直接
# 存 MitmRuntime.sticky_session_enabled（配置落盘在 core/settings.py）。
STICKY_SESSION_OPTIONS: tuple[str, ...] = ("stickycookie", "stickyauth")
STICKY_SESSION_FILTER = "~http"


def sticky_session_option_updates(enabled: bool) -> dict[str, str | None]:
    """Translate the switch into the ``options.update`` kwargs for both addons.

    恒写全量：关掉也要把两个选项写回 ``None`` —— 原生 configure 见到空值会清掉
    自家过滤器，留着旧串内核就会继续补头。
    """
    value = STICKY_SESSION_FILTER if enabled else None
    return {"stickycookie": value, "stickyauth": value}


class MitmRuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class UiBridgeAddon:
    """Forward the native View signals and the websocket hooks across the boundary.

    View 的信号只是转发；websocket 那三个钩子是这里**自己**实现的 addon 钩子 ——
    `View` 一个 websocket 钩子都没有（它只有 `requestheaders` / `error` / `response` /
    `tcp_*` / `udp_*` / `update`），所以帧到达这件事没有任何原生信号可借。这与本类
    已经带着 `flow_suspended` / `flow_intercepted` 两个 ferret 自有信号是同一类问题：
    缺钩子的事件只能自己补一次 emit。
    """

    def __init__(
        self,
        view: View,
        bridge: MitmRuntime,
        master: FerretMaster,
        generation: int,
    ) -> None:
        self._view = view
        self._bridge = bridge
        self._master = master
        self._generation = generation
        self._connected = True
        self._on_add = lambda flow: bridge.flow_added.emit(flow)
        self._on_update = lambda flow: bridge.flow_updated.emit(flow)
        self._on_remove = lambda flow, index: bridge.flow_removed.emit(flow, index)
        self._on_refresh = lambda: bridge.view_refreshed.emit()
        view.sig_view_add.connect(self._on_add)
        view.sig_view_update.connect(self._on_update)
        view.sig_view_remove.connect(self._on_remove)
        view.sig_view_refresh.connect(self._on_refresh)

    def running(self) -> None:
        if not self._master.proxyserver.listen_addrs():
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "代理端口监听失败")
            )
        self._bridge._master_running.emit(self._generation)

    def websocket_start(self, flow: HTTPFlow) -> None:
        """101 握手成功、可以收发帧了。"""
        if not self._connected:
            return
        self._bridge.websocket_started.emit(flow.id)

    def websocket_message(self, flow: HTTPFlow) -> None:
        """一帧到达（约定：最新一帧在 ``flow.websocket.messages[-1]``）。

        发的是 :class:`WsFrame` 值对象而不是 flow：钩子跑在 mitm 线程上，发 flow 过去
        等于让 Qt 侧读活 flow（AGENTS.md §3 红线），而且 `WebSocketMessage.content`
        此刻还是可改的 —— 当场取值才对得上「这一帧当时是什么」。
        """
        if not self._connected:
            return
        frame = latest_frame(flow.websocket)
        if frame is not None:
            self._bridge.websocket_frame.emit(flow.id, frame)

    def websocket_end(self, flow: HTTPFlow) -> None:
        """连接关了。关闭码 / 原因 / 谁关的 / 何时关，一并作为值对象发出去。"""
        if not self._connected:
            return
        self._bridge.websocket_closed.emit(flow.id, ws_close(flow.websocket))

    def done(self) -> None:
        self.disconnect()

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._view.sig_view_add.disconnect(self._on_add)
        self._view.sig_view_update.disconnect(self._on_update)
        self._view.sig_view_remove.disconnect(self._on_remove)
        self._view.sig_view_refresh.disconnect(self._on_refresh)


class _MitmThread(QThread):
    failed = Signal(int, str)

    def __init__(self, runtime: MitmRuntime, generation: int) -> None:
        super().__init__(runtime)
        self.runtime = runtime
        self.generation = generation
        self.loop: asyncio.AbstractEventLoop | None = None
        self.master: FerretMaster | None = None
        self.stop_requested = False

    def run(self) -> None:
        try:
            asyncio.run(self._run_master())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.generation, str(exc))
            try:
                log.error("mitmproxy runtime failed: %s", exc)
            except RuntimeError:
                pass

    async def _run_master(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._ensure_port_available()
        # 上一个循环可能没跑成 disarm（stop 超时、崩溃），把守护进程的旧 spec
        # 与单例占位清干净再起，否则 _start 的 "more than one redirector" 护栏
        # 会让 local 通道静默失效。
        await self.loop.run_in_executor(None, MitmRuntime._disarm_local_redirector)
        options = Options(
            listen_host=self.runtime.listen_host,
            listen_port=self.runtime.listen_port,
            confdir=str(get_certs_dir()),
            mode=self.runtime._mode_specs(),
        )
        master = FerretMaster(options, event_loop=self.loop, view=self.runtime.view)
        master.addons.add(
            UiBridgeAddon(
                self.runtime.view,
                self.runtime,
                master,
                self.generation,
            )
        )
        self._apply_gateway_rules(master)
        self._apply_block_options(master)
        self._apply_rewrite_rules(master)
        self._apply_intercept_rules(master)
        self._apply_sticky_session(master)
        # 编辑页发送结果的回报桥：与 gateway.on_suspend_changed 同一个接法，
        # 回调只做一次 Signal.emit，由 Qt 队列连接跨线程。
        master.compose.on_result = self.runtime.compose_result.emit
        # SSE tee 的信号桥同理（master 装配时 runtime 还不存在，只能在这里补）。
        master.sse.bridge = self.runtime
        self.master = master
        self.runtime._master_created.emit(self.generation, master)
        if self.stop_requested:
            master.shutdown()
        try:
            await master.run()
        finally:
            self.master = None
            self.loop = None

    def _apply_gateway_rules(self, master: FerretMaster) -> None:
        """Seed both gateway planes before serving traffic (on the mitm loop).

        挂起变更回调也在这里接：`GatewayState` 只在 mitm 线程上被读写，回调本身
        只做一次 `Signal.emit`（和 `UiBridgeAddon` 转发 View 信号是同一条路子）。
        """
        master.gateway.on_suspend_changed = self.runtime._on_flow_suspended
        try:
            self.runtime._push_gateway(master, self.runtime._gateway_payload())
        except (ValueError, OptionsError) as exc:
            log.warning("网关规则无法应用，已忽略: %s", exc)

    def _apply_block_options(self, master: FerretMaster) -> None:
        """Seed Block's source filters before serving traffic (on the mitm loop).

        `block_global` / `block_private` 由 Block.load 注册，构造 Options 时还不存在，
        所以只能等 Master 建好之后再写。
        """
        try:
            master.options.update(
                block_global=self.runtime.block_global,
                block_private=self.runtime._effective_block_private(),
            )
        except (ValueError, OptionsError) as exc:
            log.warning("来源过滤开关无法应用，已忽略: %s", exc)

    def _apply_rewrite_rules(self, master: FerretMaster) -> None:
        """Seed the rewrite snapshot before serving traffic (on the mitm loop).

        自研件不吃选项（原生四件退役后 ``rewrite_option_updates`` 一并拆除），
        快照在这里编译（编译不碰任何线程绑定资源）并注入 addon —— 编译失败记
        日志忽略：规则是整批下发的，一条坏规则不该拖死内核启动。
        """
        try:
            snapshot = RewriteRuleSet(self.runtime.rewrite_rules)
        except ValueError as exc:
            log.warning("重写规则无法应用，已忽略: %s", exc)
            return
        master.rewrite.set_rules(snapshot, enabled=self.runtime.rewrite_enabled)

    def _apply_intercept_rules(self, master: FerretMaster) -> None:
        """Seed the breakpoint option before serving traffic (on the mitm loop).

        拦截变更回调也在这里接，理由与 `_apply_gateway_rules` 里的挂起回调逐字相同：
        `InterceptState` 只在 mitm 线程上被读写，回调本身只做一次 `Signal.emit`。
        ``intercept`` 由 Intercept.load 注册，构造 Options 时还不存在，同样只能等
        Master 建好之后再写。
        """
        master.intercept_state.on_intercept_changed = self.runtime._on_flow_intercepted
        try:
            master.options.update(
                **intercept_option_updates(
                    self.runtime.intercept_rules,
                    enabled=self.runtime.intercept_enabled,
                )
            )
        except (ValueError, OptionsError) as exc:
            log.warning("断点规则无法应用，已忽略: %s", exc)

    def _apply_sticky_session(self, master: FerretMaster) -> None:
        """Seed the sticky-session options before serving traffic (on the mitm loop).

        ``stickycookie`` / ``stickyauth`` 由两个原生 addon 的 ``load`` 注册，构造
        Options 时还不存在，只能等 Master 建好之后再写 —— 和 `_apply_rewrite_rules`
        完全同一个约束。
        """
        try:
            master.options.update(
                **sticky_session_option_updates(self.runtime.sticky_session_enabled)
            )
        except (ValueError, OptionsError) as exc:
            log.warning("固定会话开关无法应用，已忽略: %s", exc)

    def _ensure_port_available(self) -> None:
        if self.runtime.listen_port == 0:
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((self.runtime.listen_host, self.runtime.listen_port))
        except OSError as exc:
            # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "端口 {} 已被占用").format(
                    self.runtime.listen_port
                )
            ) from exc

    def request_shutdown(self) -> None:
        self.stop_requested = True
        loop = self.loop
        master = self.master
        if loop is None or master is None:
            return
        try:
            loop.call_soon_threadsafe(master.shutdown)
        except RuntimeError:
            pass


class MitmRuntime(QObject):
    """Own one Master for the whole application lifetime."""

    state_changed = Signal(object)
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    flow_suspended = Signal(object)
    flow_intercepted = Signal(object)
    # flow_id + 值对象。用 str 而不是整条 flow：见 `UiBridgeAddon.websocket_message`。
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)
    # SSE 三件事，与 WS 同形：flow_id + SseEvent 值对象（core/mitm/sse.py）。
    sse_started = Signal(str)
    sse_event = Signal(str, object)
    sse_ended = Signal(str)
    # 编辑页手工发送的落地结果：ComposeResult 值对象（见 core/mitm/compose.py）。
    compose_result = Signal(object)

    _master_created = Signal(int, object)
    _master_running = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        listen_host: str = LOOPBACK_HOST,
        listen_port: int = 8080,
        block_global: bool = True,
        block_private: bool = False,
        use_local: bool = False,
        local_spec: str = "",
        use_wireguard: bool = False,
        sticky_session_enabled: bool = False,
    ) -> None:
        super().__init__(parent)
        self.listen_host = normalize_listen_host(listen_host)
        self.listen_port = listen_port
        # 原生 Block addon 的两个来源过滤开关（mitmproxy/addons/block.py）。
        # 默认沿用 mitmproxy 出厂姿态：拒公网、放局域网；环回永远放行且不可配。
        self.block_global = block_global
        self.block_private = block_private
        # 抓包通道（见 core/mitm/modes.py）：local = 本地重定向、wireguard = VPN
        # 隧道。类默认**全关** —— 直接构造 MitmRuntime 的场景（测试、兜底组合根）
        # 不该一启动就弹 UAC；真实应用的默认全开由 CONFIG 种子决定（见
        # core/runtime.py::_build_mitm_runtime）。
        self.use_local = use_local
        self.local_spec = local_spec
        self.use_wireguard = use_wireguard
        # 「抓包会话」是否接通：False 时 _mode_specs 只回 regular（应用启动态），
        # True 才把启用的通道拼进 mode 列表。与上面三个**意图值**分开 —— 停止
        # 会话只动这一位，用户的通道偏好原样保留，下次点开始照旧拼装。
        self.channels_engaged = False
        self.view = View()
        self.view.set_filter(parse_filter("~http"))
        self._state = MitmRuntimeState.STOPPED
        self._thread: _MitmThread | None = None
        self._master: FerretMaster | None = None
        self._last_error = ""
        self._generation = 0
        self.gateway_rules: list[GatewayRule] = []
        self.gateway_enabled = True
        self.rewrite_rules: list[RewriteRule] = []
        # 重写总开关：关掉后自研件对所有流量一律不判（网关规则模式，见
        # apply_rewrite_rules）。刻意**不落盘**（plans/rewrite-ui.md §8：
        # settings.py 零改动）—— 每次启动都是开，「临时下发空规则」的语义由
        # 这个内存位承担，不碰各行规则的 enabled 落盘值。
        self.rewrite_enabled = True
        self.intercept_rules: list[InterceptRule] = []
        # 断点默认**关**：拦截会把客户端连接一直钉住等人处理，一启动就生效等于用户
        # 还没打开界面、流量就先卡住了。开关由界面显式打开（见 core/settings.py 的
        # intercept_enabled）。
        self.intercept_enabled = False
        # 固定会话默认**关**：开启会改写实时抓取所见的请求头（代理侧补
        # Cookie / Authorization），与「抓包应如实转发原件」冲突。类默认关、真实
        # 应用由 CONFIG 种子决定（core/runtime.py::_build_mitm_runtime），开关
        # 在设置页（apps/settings）。
        self.sticky_session_enabled = sticky_session_enabled

        self._master_created.connect(self._on_master_created)
        self._master_running.connect(self._on_master_running)

    @property
    def state(self) -> MitmRuntimeState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == MitmRuntimeState.RUNNING and self._master is not None

    @property
    def master(self) -> FerretMaster | None:
        return self._master

    @property
    def last_error(self) -> str:
        return self._last_error

    def _mode_specs(self) -> list[str]:
        """完整 ``mode`` 选项：regular 恒在（常驻底盘），通道按「启用 × 已接通」拼接。"""
        engaged = self.channels_engaged
        return capture_mode_specs(
            use_local=engaged and self.use_local,
            local_spec=self.local_spec,
            use_wireguard=engaged and self.use_wireguard,
        )

    def set_channels_engaged(self, engaged: bool) -> None:
        """Open or close the capture session on the kernel.

        True 把启用的通道热更进 mode 列表（local 的提权守护进程由上游常驻复用，
        重开不弹 UAC）；False 回到 regular-only，OS 级截流全部解除 —— 内核继续
        空转，compose / 详情页不受影响。只动接通位，不碰 use_local/use_wireguard
        这些意图值。

        Raises:
            ValueError: 内核拒绝（坏 spec / 重复监听地址等），内存副本已回滚。
        """
        previous = self.channels_engaged
        self.channels_engaged = engaged
        specs = self._mode_specs()
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(
                    mode=specs,
                    block_global=self.block_global,
                    block_private=self._effective_block_private(),
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.channels_engaged = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            self.channels_engaged = previous
            raise

    def _effective_block_private(self) -> bool:
        """下发 Block 选项时实际采用的 ``block_private`` 值。

        WireGuard 客户端全部来自 10.0.0.1/32（上游 ``WireGuardServerInstance`` 固定
        分配的隧道网段），``block_private`` 开着会把它们当「局域网来源」全杀 ——
        而原生 Block 对 LocalMode 连接已有豁免（block.py:35），环回也恒放行，所以
        这里只需要为 wireguard 让路。用户配置的 ``block_private`` 原样保留在
        ``self.block_private``，通道撤下后自动恢复，不丢用户偏好。
        """
        return self.block_private and not (self.use_wireguard and self.channels_engaged)

    def apply_channels(
        self,
        *,
        use_local: bool | None = None,
        local_spec: str | None = None,
        use_wireguard: bool | None = None,
    ) -> None:
        """Switch the local-redirect / WireGuard channels on a running kernel.

        与 `apply_rewrite_rules` 同构：spec 在提交任何东西之前先过一遍原生解析器，
        坏值不会走到「内核已受理一半」。热更走 ``options.update(mode=...)`` ——
        原生 proxyserver 监听 mode 变更，diff 出增删并热启停实例；local 的提权
        守护进程被上游刻意常驻（`LocalRedirectorInstance._stop` 只清截流配置），
        所以关了再开不会再次弹 UAC。

        Raises:
            ValueError: spec 不合法，或内核拒绝（超时/启动失败等，此时内存副本回滚）。
        """
        previous = (self.use_local, self.local_spec, self.use_wireguard)
        if use_local is not None:
            self.use_local = use_local
        if local_spec is not None:
            self.local_spec = local_spec.strip()
        if use_wireguard is not None:
            self.use_wireguard = use_wireguard
        specs = self._mode_specs()
        try:
            validate_mode_specs(specs)
        except ValueError:
            (self.use_local, self.local_spec, self.use_wireguard) = previous
            raise

        # block_private 要跟着让路（见 _effective_block_private）；这一步无先决
        # 条件 —— 内核没跑时先把内存副本对齐，下次启动的 _apply_block_options 才
        # 能读到正确值。
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(
                    mode=specs,
                    block_global=self.block_global,
                    block_private=self._effective_block_private(),
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            (self.use_local, self.local_spec, self.use_wireguard) = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            (self.use_local, self.local_spec, self.use_wireguard) = previous
            raise

    def start(self) -> None:
        if self._thread is not None or self._state in (
            MitmRuntimeState.STARTING,
            MitmRuntimeState.RUNNING,
            MitmRuntimeState.STOPPING,
        ):
            return
        self._last_error = ""
        self._set_state(MitmRuntimeState.STARTING)
        self._generation += 1
        generation = self._generation
        thread = _MitmThread(self, generation)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(
            lambda t=thread, g=generation: self._on_thread_finished(g, t)
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout_ms: int = 5000) -> bool:
        thread = self._thread
        if thread is None:
            self._master = None
            self._set_state(MitmRuntimeState.STOPPED)
            return True
        # local 的提权守护进程是**进程外**服务，内核线程死了它还活着。正常停机
        # 路径由 Servers.update 的 stop 任务清截流配置；但事件循环关闭时挂起的
        # 任务会被静默丢弃——抓包中改端口/地址触发的 restart 必然如此。守护进程
        # 随后继续按旧 spec 截流，而新内核的 mode 列表里没有 local，再没有谁会
        # 去清它（界面症状：关了本地重定向还能抓到本机流量）。所以趁事件循环
        # 还活着在它上面同步拔掉截流，不依赖任何异步任务。内核从未跑成（端口
        # 被占、立即停止）时 call 没有循环可投，此刻本进程也没接过守护进程，
        # 跳过即可。
        try:
            self.call(self._disarm_local_redirector)
        except (RuntimeError, TimeoutError):
            pass
        self._set_state(MitmRuntimeState.STOPPING)
        thread.request_shutdown()
        stopped = thread.wait(timeout_ms)
        if not stopped:
            log.error("mitmproxy runtime did not stop within %d ms", timeout_ms)
            return False
        if self._thread is thread:
            self._thread = None
        self._master = None
        self._set_state(MitmRuntimeState.STOPPED)
        return True

    @staticmethod
    def _disarm_local_redirector() -> None:
        """Runs on the mitm loop: clear the local redirector daemon's intercept spec.

        除清 spec 外还要清掉 `_instance` 占位——这是上游 `_stop` 的簿记，丢了它
        新 Master 的 `_start` 会被 "Cannot spawn more than one local redirector"
        护栏拒掉，local 通道在重启后静默死亡。`_server` 为 None 说明本进程从未
        接过守护进程，直接跳过。
        """
        if LocalRedirectorInstance._server is None:
            return
        LocalRedirectorInstance._server.set_intercept("")
        LocalRedirectorInstance._instance = None

    def restart(
        self, *, listen_host: str | None = None, listen_port: int | None = None
    ) -> None:
        if listen_host is not None:
            self.listen_host = normalize_listen_host(listen_host)
        if listen_port is not None:
            self.listen_port = listen_port
        if not self.stop():
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmRuntime",
                    "mitmproxy 内核停止超时，无法重启",
                )
            )
        self.start()

    def apply_gateway_rules(
        self,
        rules: list[GatewayRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store gateway rules and push both planes to the Master when one runs.

        与 `apply_rewrite_rules` 同构，但要多守一条：两个平面的载荷（编译好的规则
        快照 + 原生 `allow_hosts` / `ignore_hosts`）在提交**任何**东西之前就全部构造
        并校验完。坏规则不会留下「一个平面换了、另一个还是老的」的状态 —— 两个平面
        对同一条流量给出不同判定，是这套设计最不能出的错。
        """
        previous = (self.gateway_rules, self.gateway_enabled)
        self.gateway_rules = self.gateway_rules if rules is None else list(rules)
        if enabled is not None:
            self.gateway_enabled = enabled
        try:
            payload = self._gateway_payload()
        except ValueError:
            self.gateway_rules, self.gateway_enabled = previous
            raise
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: self._push_gateway(master, payload))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.gateway_rules, self.gateway_enabled = previous
            raise ValueError(str(exc)) from exc

    def _gateway_payload(self) -> tuple[GatewayRuleSet, dict[str, list[str]], bool]:
        """Compile both planes' payloads from the stored rules; commits nothing.

        Raises:
            ValueError: 任何一条启用的规则不合法（编译在这里发生，运行期钩子不编译）。
        """
        enabled = self.gateway_enabled
        return (
            GatewayRuleSet(self.gateway_rules),
            gateway_option_updates(self.gateway_rules, enabled=enabled),
            enabled,
        )

    @staticmethod
    def _push_gateway(
        master: FerretMaster,
        payload: tuple[GatewayRuleSet, dict[str, list[str]], bool],
    ) -> None:
        """Commit both planes. Only ever runs on the mitm loop."""
        ruleset, updates, enabled = payload
        # 先写原生选项（唯一还可能抛的一步），再换钩子平面的快照。
        master.options.update(**updates)
        # set_rules 顺手放行挂起中的流量：规则一变旧判定就不算数了，而挂起是永久的，
        # 不在这里放就再也没人放了。
        master.gateway.set_rules(ruleset, enabled=enabled)

    def release_suspended(self) -> int:
        """Let every suspended flow go; 返回放行条数（内核没跑就是 0）。

        规则变更与总开关都由 `_push_gateway` 顺带放行，这里是给「不改规则也要放行」
        的路径用的（清空/删除流量行，见 `MitmFacade`）。
        """
        master = self._master
        if not self.is_running or master is None:
            return 0
        return int(self.call(lambda: master.gateway.release_all()))

    def apply_rewrite_rules(
        self,
        rules: list[RewriteRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store rewrite rules and push them to the Master when one runs.

        与 `apply_gateway_rules` 同构（网关规则模式）：规则快照在提交**任何**
        东西之前就编译完（坏规则在这里抛，内存副本回滚），内核没跑就只存副本
        （下次启动 `_apply_rewrite_rules` 补推），在跑则经 ``self.call`` 把预编译
        快照换进自研 addon。总开关关掉时下发的 enabled=False 等价于空规则列表，
        但各行规则的 enabled 落盘值原样保留。

        Raises:
            ValueError: 任何一条启用且填完的规则不合法（整批回滚，绝不留半套）。
        """
        previous = (self.rewrite_rules, self.rewrite_enabled)
        candidate = self.rewrite_rules if rules is None else list(rules)
        wanted = self.rewrite_enabled if enabled is None else enabled
        try:
            snapshot = RewriteRuleSet(candidate)
        except ValueError:
            self.rewrite_rules, self.rewrite_enabled = previous
            raise
        self.rewrite_rules, self.rewrite_enabled = candidate, wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.rewrite.set_rules(snapshot, enabled=wanted))
        except Exception:
            # call 只会抛 RuntimeError / TimeoutError 这类运行期故障；与网关
            # 同一条纪律：下发失败内存副本回滚，界面显示的状态必须内核真收到了。
            self.rewrite_rules, self.rewrite_enabled = previous
            raise

    def apply_intercept_rules(
        self,
        rules: list[InterceptRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store breakpoint rules and push them to the Master when one is running.

        与 `apply_rewrite_rules` 同构：表达式在提交任何东西之前就编译并过一遍原生
        解析器。总开关关掉时下发的是 ``intercept=None``，所以「删光规则」和「关掉
        开关」走的是同一条清空路径。

        规则变更**不**顺带放行已拦下的流量（和网关 `set_rules` 刻意相反）：网关的
        挂起是规则的副产物，规则一改旧判定就不算数了；断点拦下的这条流量用户正在
        编辑器里改，改到一半去动规则列表不该把它冲掉。放行始终是显式动作。
        """
        previous = (self.intercept_rules, self.intercept_enabled)
        candidate = self.intercept_rules if rules is None else list(rules)
        wanted = self.intercept_enabled if enabled is None else enabled
        updates = intercept_option_updates(candidate, enabled=wanted)
        self.intercept_rules, self.intercept_enabled = candidate, wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.options.update(**updates))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.intercept_rules, self.intercept_enabled = previous
            raise ValueError(str(exc)) from exc

    def apply_sticky_session(self, enabled: bool | None = None) -> None:
        """Store the sticky-session switch and push it to a running Master.

        与 `apply_block_options` 同构：内核没跑就只对齐内存副本（下次启动的
        `_apply_sticky_session` 会读到它），下发失败回滚，绝不留下「界面显示已
        生效、内核其实没收到」的状态。关掉只下发 ``None`` —— 原生 addon 的 jar /
        hosts 缓存刻意不倒，重开开关立即复用（只在代理内存、不落盘）。
        """
        wanted = self.sticky_session_enabled if enabled is None else enabled
        previous = self.sticky_session_enabled
        self.sticky_session_enabled = wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(**sticky_session_option_updates(wanted))
            )
        except OptionsError as exc:
            self.sticky_session_enabled = previous
            raise ValueError(str(exc)) from exc

    def release_intercepted(self) -> int:
        """Let every breakpoint-held flow go; 返回放行条数（内核没跑就是 0）。

        只管断点这本账。网关挂起的那批走 `release_suspended` —— 两边各记一本，
        谁拦的谁放（见 `InterceptState.arm` 对 `flow.intercepted` 的退让）。
        """
        master = self._master
        if not self.is_running or master is None:
            return 0
        return int(self.call(lambda: master.intercept_state.release_all()))

    def channel_health(self) -> dict[str, bool | str]:
        """Report per-channel liveness of the running kernel.

        ``options.update(mode=...)`` 只保证 spec 语法合法并触发热启停，实例**启动**
        失败（UAC 拒绝、端口被占等）由原生 proxyserver 记日志吞掉，不会同步抛回。
        这里逐实例读 ``is_running`` / ``last_exception``，给界面一个可靠的「通道
        真的起来了吗」。键是 ``local`` / ``wireguard``，只在对应通道启用时出现；
        regular 由端口占用与 UiBridgeAddon.running 的全局探测兜底，不单列。
        """
        master = self._master
        if not self.is_running or master is None:
            return {}
        health: dict[str, bool | str] = {}
        for server in master.proxyserver.servers:
            spec = server.mode.full_spec
            if spec.startswith("local"):
                key = "local"
            elif spec.startswith("wireguard"):
                key = "wireguard"
            else:
                continue
            health[key] = server.is_running
            if not server.is_running and server.last_exception is not None:
                health[key] = str(server.last_exception)
        return health

    def apply_block_options(
        self, *, block_global: bool | None = None, block_private: bool | None = None
    ) -> None:
        """Store Block's source filters and push them to a running Master.

        与 `apply_block_rules` 同构：下发失败就把内存副本回滚，绝不留下「界面显示
        已生效、内核其实没收到」的状态。
        """
        previous = (self.block_global, self.block_private)
        if block_global is not None:
            self.block_global = block_global
        if block_private is not None:
            self.block_private = block_private
        master = self._master
        if not self.is_running or master is None:
            return
        wanted = (self.block_global, self._effective_block_private())
        try:
            self.call(
                lambda: master.options.update(
                    block_global=wanted[0], block_private=wanted[1]
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.block_global, self.block_private = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            # 这两个是 bool 选项，传错类型 optmanager 抛的是 TypeError（未知键则是
            # KeyError），都不经 OptionsError。那属于编程错误，照原样抛出去 ——
            # 但内存副本必须先回滚，否则界面会显示一个内核根本没收到的状态。
            self.block_global, self.block_private = previous
            raise

    def reload_certificate_store(self) -> bool:
        """Rebuild the live CertStore through mitmproxy's own TlsConfig hook.

        `optmanager.update_known` 对传入的每个键都发 `changed`（即使值没变），所以
        重写一次 `confdir` 就会触发原生 `TlsConfig.configure({"confdir"})` →
        `CertStore.from_store`：重新生成后的新 CA 立刻对后续连接生效，无需重启内核。
        返回 False 表示内核没在跑，下次启动时自然会读到新证书。
        """
        master = self._master
        if not self.is_running or master is None:
            return False
        self.call(lambda: master.options.update(confdir=str(get_certs_dir())))
        return True

    def call(self, callback: Callable[[], Any], *, timeout: float = 5.0) -> Any:
        thread = self._thread
        master = self._master
        loop = thread.loop if thread is not None else None
        if not self.is_running or master is None or loop is None:
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "mitmproxy 内核未运行")
            )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError("不能在 mitmproxy event loop 中同步调用")

        async def invoke() -> Any:
            result = callback()
            if inspect.isawaitable(result):
                return await result
            return result

        future = asyncio.run_coroutine_threadsafe(invoke(), loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                QCoreApplication.translate("MitmRuntime", "mitmproxy 任务执行超时")
            ) from exc

    def _on_flow_suspended(self, flow: Any) -> None:
        """Republish a suspend/release from the mitm thread as a Qt signal.

        挂起（出）发生在 `request`，而 `View` 只有 `requestheaders` / `response` /
        `error` 几个钩子 —— 不自己发一次，流量表那一行不会重绘、「挂起中」标记就
        永远不上屏。跨线程 emit 走 Qt 的队列连接，和 `UiBridgeAddon` 同一条路子。
        """
        self.flow_suspended.emit(flow)

    def _on_flow_intercepted(self, flow: Any) -> None:
        """Republish a breakpoint hold/release from the mitm thread as a Qt signal.

        与 `_on_flow_suspended` 同一个理由：请求期拦截发生在 `request` 钩子，而
        `View` 只在 `requestheaders` / `response` / `error` 上更新行，不自己补发
        一次「已拦截」就永远不上屏。
        """
        self.flow_intercepted.emit(flow)

    def _set_state(self, state: MitmRuntimeState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _on_master_created(self, generation: int, master: FerretMaster) -> None:
        if generation != self._generation or self._state != MitmRuntimeState.STARTING:
            return
        self._master = master

    def _on_master_running(self, generation: int) -> None:
        if (
            generation != self._generation
            or self._state != MitmRuntimeState.STARTING
            or self._master is None
        ):
            return
        self._set_state(MitmRuntimeState.RUNNING)
        try:
            self.apply_gateway_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("网关规则下发失败: %s", exc)
        try:
            self.apply_block_options()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("来源过滤开关下发失败: %s", exc)
        try:
            self.apply_rewrite_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("重写规则下发失败: %s", exc)
        try:
            self.apply_intercept_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("断点规则下发失败: %s", exc)
        try:
            self.apply_sticky_session()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("固定会话开关下发失败: %s", exc)
        self.ready.emit(self.view)

    def _on_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._last_error = message
        self._master = None
        self._set_state(MitmRuntimeState.FAILED)
        self.failed.emit(message)

    def _on_thread_finished(self, generation: int, thread: _MitmThread) -> None:
        if generation != self._generation or self._thread is not thread:
            return
        self._thread = None
        self._master = None
        if self._state not in (MitmRuntimeState.FAILED, MitmRuntimeState.STOPPED):
            self._set_state(MitmRuntimeState.STOPPED)
        self.stopped.emit()
