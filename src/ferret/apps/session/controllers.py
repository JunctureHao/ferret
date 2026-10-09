"""Session controllers: read-only view controller and page-level controller."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from PySide6.QtCore import (
    QObject,
    QThreadPool,
    Signal,
)

from ferret.apps.common.tasks import FunctionTask
from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.repository import SessionRepository
from ferret.core.mitm import (
    FlowExporter,
    FlowFile,
    HTTPFlow,
    View,
    WsClose,
    WsFrame,
    build_flow_body,
    build_flow_detail,
    build_flow_messages,
    build_flow_overview_metadata,
    build_flow_summary,
    build_raw_preview,
    parse_filter,
    ws_close,
)


class SessionViewController(QObject):
    """只读 Flow 查看控制器，满足 FlowViewController 协议。"""

    def __init__(
        self,
        meta: SessionMeta,
        flows: list[HTTPFlow],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.meta = meta
        self._view = View()
        self._view.set_filter(parse_filter("~http"))
        self._view.add(flows)

    @property
    def view(self) -> View:
        return self._view

    def total_count(self) -> int:
        return sum(
            1 for flow in self._view._store.values() if isinstance(flow, HTTPFlow)
        )

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        flow = self._view.get_by_id(flow_id)
        return flow if isinstance(flow, HTTPFlow) else None

    def flow_detail(self, flow_id: str) -> dict[str, Any]:
        """会话页的流量是从文件读回来的，没有 mitm 线程也就没有活 flow —— 直接构建。"""
        flow = self.get_flow(flow_id)
        return build_flow_detail(flow) if flow else {}

    def flow_summary(self, flow_id: str) -> dict[str, Any]:
        flow = self.get_flow(flow_id)
        return build_flow_summary(flow) if flow else {}

    def flow_body(
        self, flow_id: str, side: Literal["Request", "Response"]
    ) -> dict[str, Any]:
        flow = self.get_flow(flow_id)
        return build_flow_body(flow, side) if flow else {}

    def flow_overview_metadata(self, flow_id: str) -> dict[str, Any]:
        flow = self.get_flow(flow_id)
        return build_flow_overview_metadata(flow) if flow else {}

    def flow_messages(self, flow_id: str) -> dict[str, Any]:
        return build_flow_messages(self.get_flow(flow_id))

    def websocket_frames(self, flow_id: str) -> list[WsFrame]:
        """会话页没有 mitm 线程，`flow.websocket` 直接读 —— 文件里的 flow 是死的。

        只读页刻意不接那三个实时信号：这批流量早就结束了，没有「新帧到达」这件事。
        """
        return self.flow_messages(flow_id)["frames"]

    def websocket_close(self, flow_id: str) -> WsClose:
        flow = self.get_flow(flow_id)
        return ws_close(flow.websocket) if flow else WsClose()

    def get_raw_request(self, flow_id: str) -> bytes:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.raw_request(flow)
        return b""

    def get_raw_response(self, flow_id: str) -> bytes:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.raw_response(flow)
        return b""

    def get_raw_request_preview(self, flow_id: str) -> dict[str, Any]:
        flow = self.get_flow(flow_id)
        return build_raw_preview(flow, "Request") if flow else {}

    def get_raw_response_preview(self, flow_id: str) -> dict[str, Any]:
        flow = self.get_flow(flow_id)
        return build_raw_preview(flow, "Response") if flow else {}

    def get_raw_flow(self, flow_id: str) -> bytes:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.raw(flow)
        return b""

    def get_request_body(self, flow_id: str) -> bytes:
        """会话页没有 mitm 线程，死 flow 直接解 body —— 同 raw 三件的读法。"""
        flow = self.get_flow(flow_id)
        return FlowExporter.request_body(flow) if flow else b""

    def get_response_body(self, flow_id: str) -> bytes:
        flow = self.get_flow(flow_id)
        return FlowExporter.response_body(flow) if flow else b""

    def get_httpie_command(self, flow_id: str) -> str:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.httpie_command(flow)
        return ""

    def get_curl_command(self, flow_id: str) -> str:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.curl_command(flow)
        return ""

    def save_flows(self, flow_ids: list[str], path: str) -> int:
        """按 id 解析本会话 View 里的死 flow 再写文件（会话页没有 mitm 线程）。"""
        flows = [
            flow
            for fid in flow_ids
            if isinstance(flow := self._view.get_by_id(fid), HTTPFlow)
        ]
        return FlowFile.write(path, flows)

    def export_har(self, flow_ids: list[str], path: str) -> None:
        """save_flows 同款：死 flow 就地解析，save_har 是纯函数、不读 ctx。"""
        flows = [
            flow
            for fid in flow_ids
            if isinstance(flow := self._view.get_by_id(fid), HTTPFlow)
        ]
        FlowExporter.save_har(flows, path)


class SessionController(QObject):
    """会话页面控制器：管理异步任务和页面业务信号。"""

    sessions_loaded = Signal(list)
    session_created = Signal(object)
    session_updated = Signal(str, object)
    session_deleted = Signal(str)
    session_opened = Signal(object, object)  # SessionMeta, SessionViewController
    busy_changed = Signal(bool)
    operation_failed = Signal(str, str)  # title, detail
    operation_succeeded = Signal(str)

    def __init__(
        self,
        parent: QObject | None = None,
        repository: SessionRepository | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo = repository or SessionRepository()
        self._write_pool = QThreadPool(self)
        self._write_pool.setMaxThreadCount(1)
        self._active_tasks = 0
        self._open_generation = 0
        self._open_running = False
        self._pending_open: tuple[int, str] | None = None
        self._refresh_generation = 0
        self._refresh_running = False
        self._refresh_pending = False
        # 扫描与写入不能交叉：扫描会读路径和计数缓存，写入则可能改名/删文件。
        self._repository_lock = RLock()
        self._repository_revision = 0
        self._tasks: set[FunctionTask] = set()

    def _set_task_active(self, active: bool) -> None:
        was_busy = self._active_tasks > 0
        self._active_tasks = max(0, self._active_tasks + (1 if active else -1))
        is_busy = self._active_tasks > 0
        if is_busy != was_busy:
            self.busy_changed.emit(is_busy)

    def _run(
        self,
        fn: Callable[..., Any],
        *args,
        on_success=None,
        on_failure=None,
        on_finished=None,
        is_current: Callable[[], bool] | None = None,
        write: bool = False,
        exclusive: bool = False,
    ) -> None:
        self._set_task_active(True)
        if write:
            self._refresh_generation += 1

        def _execute():
            if write or exclusive:
                with self._repository_lock:
                    try:
                        return fn(*args)
                    finally:
                        if write:
                            # 修订在真正写入结束时递增，不等 GUI 消费成功/失败信号。
                            self._repository_revision += 1
            return fn(*args)

        task = FunctionTask(_execute)
        task.setAutoDelete(True)
        self._tasks.add(task)

        def _on_succeeded(result):
            if write:
                self._refresh_generation += 1
            if on_success:
                on_success(result)

        def _on_failed(msg: str):
            if is_current is not None and not is_current():
                return
            if on_failure:
                on_failure(msg)
            self.operation_failed.emit(self.tr("操作失败"), msg)

        def _on_finished():
            if write:
                # 即使部分写入失败，旧扫描也不能在写操作后覆盖页面。
                self.refresh()
            if on_finished:
                on_finished()
            self._tasks.discard(task)
            # 拆环：task → signals → Qt 连接 → 本闭包 → task 是横跨 C++ 边界的
            # 引用环，gc.collect() 收不掉；断掉 signals 上的连接后任务才整体可释放。
            task.signals.succeeded.disconnect()
            task.signals.failed.disconnect()
            task.signals.finished.disconnect()
            self._set_task_active(False)

        task.signals.succeeded.connect(_on_succeeded)
        task.signals.failed.connect(_on_failed)
        task.signals.finished.connect(_on_finished)
        pool = self._write_pool if write else QThreadPool.globalInstance()
        pool.start(task)

    def refresh(self) -> None:
        self._refresh_generation += 1
        if self._refresh_running:
            self._refresh_pending = True
            return
        self._start_refresh()

    def _start_refresh(self) -> None:
        generation = self._refresh_generation
        self._refresh_running = True
        self._refresh_pending = False

        def _scan():
            return self._repository_revision, self._repo.list_all()

        def _on_loaded(result):
            revision, sessions = result
            # 检查与发布都在写锁内；锁正被耗时 IO 持有时丢弃并重扫，不能卡 GUI。
            if not self._repository_lock.acquire(blocking=False):
                self._refresh_pending = True
                return
            try:
                if (
                    generation == self._refresh_generation
                    and revision == self._repository_revision
                ):
                    self.sessions_loaded.emit(sessions)
            finally:
                self._repository_lock.release()

        def _on_finished():
            self._refresh_running = False
            if self._refresh_pending:
                self._start_refresh()

        self._run(
            _scan,
            on_success=_on_loaded,
            on_finished=_on_finished,
            is_current=lambda: generation == self._refresh_generation,
            exclusive=True,
        )

    def save_capture(self, name: str, flows: list[HTTPFlow]) -> None:
        def _on_created(meta: SessionMeta):
            self.session_created.emit(meta)
            self.operation_succeeded.emit(self.tr("会话已保存"))

        self._run(
            self._repo.create,
            name,
            flows,
            SessionSource.CAPTURE,
            on_success=_on_created,
            write=True,
        )

    def import_session(self, path: Path) -> None:
        def _on_imported(meta: SessionMeta):
            self.session_created.emit(meta)
            self.operation_succeeded.emit(self.tr("会话已导入"))

        self._run(
            self._repo.import_file,
            Path(path),
            on_success=_on_imported,
            write=True,
        )

    def open_session(self, session_id: str) -> None:
        self._open_generation += 1
        self._pending_open = (self._open_generation, session_id)
        if not self._open_running:
            self._start_open()

    def _start_open(self) -> None:
        pending = self._pending_open
        if pending is None:
            return
        generation, session_id = pending
        self._pending_open = None
        self._open_running = True

        def _on_loaded(result):
            if generation != self._open_generation:
                return
            meta, flows = result
            # 不给长期存活的 SessionController 当 parent：Qt 的父-子所有权会让每个
            # 会话把整份流量副本（含正文）钉在父对象上直到进程退出，GC 收不掉。
            # 旧 vc 的释放交给 SessionViewerPage.load 覆盖 self.vc，见 views.py。
            vc = SessionViewController(meta, flows)
            self.session_opened.emit(meta, vc)

        def _on_finished():
            self._open_running = False
            self._start_open()

        self._run(
            self._repo.open,
            session_id,
            on_success=_on_loaded,
            on_finished=_on_finished,
            is_current=lambda: generation == self._open_generation,
            exclusive=True,
        )

    def rename_session(self, session_id: str, name: str) -> None:
        def _on_renamed(meta: SessionMeta):
            self.session_updated.emit(session_id, meta)
            self.operation_succeeded.emit(self.tr("会话已重命名"))

        self._run(
            self._repo.rename,
            session_id,
            name,
            on_success=_on_renamed,
            write=True,
        )

    def delete_sessions(self, session_ids: list[str]) -> None:
        def _delete_all():
            for sid in session_ids:
                self._repo.delete(sid)

        def _on_deleted(_):
            for sid in session_ids:
                self.session_deleted.emit(sid)  # N 次（表格逐行删）
            self.operation_succeeded.emit(  # 1 次（只弹一个框）
                self.tr("会话已删除")
            )

        self._run(
            _delete_all,
            on_success=_on_deleted,
            on_failure=lambda _: self.refresh(),
            write=True,
        )

    def export_session(self, session_id: str, path: Path) -> None:
        self._run(
            self._repo.export,
            session_id,
            Path(path),
            on_success=lambda _: self.operation_succeeded.emit(self.tr("会话已导出")),
            write=True,
        )

    def export_sessions(self, metas: list[SessionMeta], directory: Path) -> None:
        """批量导出：每个会话按自己的名字落一个 ``<名字>.flow``。

        会话名在仓库内唯一（create/rename 走 ``_unique_path``），同批互不覆盖；
        逐条 try 是刻意的：一个会话的文件没了不该拖垮整批。
        """

        def _export_all() -> list[str]:
            failures: list[str] = []
            for meta in metas:
                try:
                    self._repo.export(meta.session_id, directory / f"{meta.name}.flow")
                except OSError as exc:
                    failures.append(f"{meta.name}: {exc}")
            return failures

        def _on_exported(failures: list[str]) -> None:
            if failures:
                self.operation_failed.emit(
                    self.tr("部分会话导出失败"), "\n".join(failures)
                )
            else:
                self.operation_succeeded.emit(
                    self.tr("已导出 {} 个会话").format(len(metas))
                )

        self._run(_export_all, on_success=_on_exported, write=True)
