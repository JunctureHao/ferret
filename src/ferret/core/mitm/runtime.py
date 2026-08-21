"""Application-scoped mitmproxy runtime and event-loop bridge."""

from __future__ import annotations

import asyncio
import inspect
import socket
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import Options, OptionsError, View, parse_filter
from ferret.core.mitm.gateway import (
    GatewayRule,
    GatewayRuleSet,
    gateway_option_updates,
)
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.rewrite import RewriteRule, rewrite_option_updates
from ferret.core.network import LOOPBACK_HOST, normalize_listen_host
from ferret.core.settings import get_certs_dir

log = get_logger("mitmproxy")


class MitmRuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class UiBridgeAddon:
    """Forward the native View signals across the runtime boundary."""

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
            raise RuntimeError("代理端口监听失败")
        self._bridge._master_running.emit(self._generation)

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
        options = Options(
            listen_host=self.runtime.listen_host,
            listen_port=self.runtime.listen_port,
            confdir=str(get_certs_dir()),
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
                block_private=self.runtime.block_private,
            )
        except (ValueError, OptionsError) as exc:
            log.warning("来源过滤开关无法应用，已忽略: %s", exc)

    def _apply_rewrite_rules(self, master: FerretMaster) -> None:
        """Seed the rewrite options before serving traffic (on the mitm loop).

        `map_remote` 由 MapRemote.load 注册，构造 Options 时还不存在（实测
        `addons.add()` 之前 `"map_remote" in options` 为 False），只能等 Master
        建好之后再写 —— 和 `block_global` 完全同一个约束。
        """
        try:
            master.options.update(**rewrite_option_updates(self.runtime.rewrite_rules))
        except (ValueError, OptionsError) as exc:
            log.warning("重写规则无法应用，已忽略: %s", exc)

    def _ensure_port_available(self) -> None:
        if self.runtime.listen_port == 0:
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((self.runtime.listen_host, self.runtime.listen_port))
        except OSError as exc:
            raise RuntimeError(f"端口 {self.runtime.listen_port} 已被占用") from exc

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
    ) -> None:
        super().__init__(parent)
        self.listen_host = normalize_listen_host(listen_host)
        self.listen_port = listen_port
        # 原生 Block addon 的两个来源过滤开关（mitmproxy/addons/block.py）。
        # 默认沿用 mitmproxy 出厂姿态：拒公网、放局域网；环回永远放行且不可配。
        self.block_global = block_global
        self.block_private = block_private
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

    def restart(
        self, *, listen_host: str | None = None, listen_port: int | None = None
    ) -> None:
        if listen_host is not None:
            self.listen_host = normalize_listen_host(listen_host)
        if listen_port is not None:
            self.listen_port = listen_port
        if not self.stop():
            raise RuntimeError("mitmproxy 内核停止超时，无法重启")
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

    def apply_rewrite_rules(self, rules: list[RewriteRule] | None = None) -> None:
        """Store rewrite rules and push them to the Master when one is running.

        与 `apply_block_rules` 同构：specs 在提交任何东西之前就全部构造并过一遍原生
        解析器，坏规则不会留下「内存副本已换、内核没收到」的状态。下发的 kwargs 恒
        含全部重写选项，所以删光规则也会把对应选项清成空列表。
        """
        previous = self.rewrite_rules
        candidate = self.rewrite_rules if rules is None else list(rules)
        updates = rewrite_option_updates(candidate)
        self.rewrite_rules = candidate
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.options.update(**updates))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.rewrite_rules = previous
            raise ValueError(str(exc)) from exc

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
        wanted = (self.block_global, self.block_private)
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
            raise RuntimeError("mitmproxy 内核未运行")
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
            raise TimeoutError("mitmproxy 任务执行超时") from exc

    def _on_flow_suspended(self, flow: Any) -> None:
        """Republish a suspend/release from the mitm thread as a Qt signal.

        挂起（出）发生在 `request`，而 `View` 只有 `requestheaders` / `response` /
        `error` 几个钩子 —— 不自己发一次，流量表那一行不会重绘、「挂起中」标记就
        永远不上屏。跨线程 emit 走 Qt 的队列连接，和 `UiBridgeAddon` 同一条路子。
        """
        self.flow_suspended.emit(flow)

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
