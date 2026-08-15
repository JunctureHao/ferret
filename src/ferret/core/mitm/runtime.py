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
from ferret.core.mitm.bindings import Options, View, parse_filter
from ferret.core.mitm.master import FerretMaster
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
        bridge: "MitmRuntime",
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
        for signal, slot in (
            (self._view.sig_view_add, self._on_add),
            (self._view.sig_view_update, self._on_update),
            (self._view.sig_view_remove, self._on_remove),
            (self._view.sig_view_refresh, self._on_refresh),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                pass


class _MitmThread(QThread):
    failed = Signal(int, str)

    def __init__(self, runtime: "MitmRuntime", generation: int) -> None:
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
        self.master = master
        self.runtime._master_created.emit(self.generation, master)
        if self.stop_requested:
            master.shutdown()
        try:
            await master.run()
        finally:
            self.master = None
            self.loop = None

    def _ensure_port_available(self) -> None:
        if self.runtime.listen_port == 0:
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((self.runtime.listen_host, self.runtime.listen_port))
        except OSError as exc:
            raise RuntimeError(
                f"端口 {self.runtime.listen_port} 已被占用"
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

    _master_created = Signal(int, object)
    _master_running = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8080,
    ) -> None:
        super().__init__(parent)
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.view = View()
        self.view.set_filter(parse_filter("~http"))
        self._state = MitmRuntimeState.STOPPED
        self._thread: _MitmThread | None = None
        self._master: FerretMaster | None = None
        self._last_error = ""
        self._generation = 0

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

    def restart(self, *, listen_port: int | None = None) -> None:
        if listen_port is not None:
            self.listen_port = listen_port
        if not self.stop():
            raise RuntimeError("mitmproxy 内核停止超时，无法重启")
        self.start()

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
