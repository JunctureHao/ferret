"""Session controllers: read-only view controller and page-level controller."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    Signal,
)

from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.repository import SessionRepository
from ferret.core.log import get_logger
from ferret.core.mitm import (
    FlowExporter,
    FlowFile,
    HTTPFlow,
    View,
    parse_filter,
)

log = get_logger("session")


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

    def get_raw_flow(self, flow_id: str) -> bytes:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.raw(flow)
        return b""

    def get_httpie_command(self, flow_id: str) -> str:
        flow = self.get_flow(flow_id)
        if flow:
            return FlowExporter.httpie_command(flow)
        return ""

    def save_flows(self, flows: list[HTTPFlow], path: str) -> int:
        return FlowFile.write(path, flows)

    def export_har(self, flows: list[HTTPFlow], path: str) -> None:
        # save_har 是纯函数、不读 ctx，所以只读会话页没有 master 也能导出。
        FlowExporter.save_har(flows, path)


class WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(str)
    finished = Signal()


class FunctionTask(QRunnable):
    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
            self.signals.succeeded.emit(result)
        except Exception as e:
            log.exception("后台任务失败")
            self.signals.failed.emit(str(e))
        finally:
            self.signals.finished.emit()


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
        write: bool = False,
    ) -> None:
        self._set_task_active(True)
        task = FunctionTask(fn, *args)
        task.setAutoDelete(True)
        self._tasks.add(task)

        def _on_succeeded(result):
            if on_success:
                on_success(result)

        def _on_failed(msg: str):
            if on_failure:
                on_failure(msg)
            self.operation_failed.emit("操作失败", msg)

        def _on_finished():
            self._set_task_active(False)
            self._tasks.discard(task)

        task.signals.succeeded.connect(_on_succeeded)
        task.signals.failed.connect(_on_failed)
        task.signals.finished.connect(_on_finished)
        pool = self._write_pool if write else QThreadPool.globalInstance()
        pool.start(task)

    def refresh(self) -> None:
        self._run(self._repo.list_all, on_success=self.sessions_loaded.emit)

    def save_capture(self, name: str, flows: list[HTTPFlow]) -> None:
        def _on_created(meta: SessionMeta):
            self.session_created.emit(meta)
            self.operation_succeeded.emit("会话已保存")

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
            self.operation_succeeded.emit("会话已导入")

        self._run(
            self._repo.import_file,
            Path(path),
            on_success=_on_imported,
            write=True,
        )

    def open_session(self, session_id: str) -> None:
        self._open_generation += 1
        generation = self._open_generation

        def _on_loaded(result):
            if generation != self._open_generation:
                return
            meta, flows = result
            vc = SessionViewController(meta, flows, self)
            self.session_opened.emit(meta, vc)

        def _do_open(sid: str):
            meta = self._repo.get(sid)
            flows = self._repo.load_flows(sid)
            return (meta, flows)

        self._run(_do_open, session_id, on_success=_on_loaded)

    def rename_session(self, session_id: str, name: str) -> None:
        def _on_renamed(meta: SessionMeta):
            self.session_updated.emit(session_id, meta)
            self.operation_succeeded.emit("会话已重命名")

        self._run(
            self._repo.rename,
            session_id,
            name,
            on_success=_on_renamed,
            write=True,
        )

    def delete_session(self, session_id: str) -> None:
        def _on_deleted(_):
            self.session_deleted.emit(session_id)
            self.operation_succeeded.emit("会话已删除")

        self._run(
            self._repo.delete,
            session_id,
            on_success=_on_deleted,
            on_failure=lambda _: self.refresh(),
            write=True,
        )

    def export_session(self, session_id: str, path: Path) -> None:
        self._run(
            self._repo.export,
            session_id,
            Path(path),
            on_success=lambda _: self.operation_succeeded.emit("会话已导出"),
            write=True,
        )
