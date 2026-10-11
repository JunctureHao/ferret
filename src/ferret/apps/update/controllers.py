"""应用内更新的后台任务编排（docs/design.md#update）。

检查/下载是阻塞网络 IO，一律经 ``FunctionTask`` 挪出主线程；velopack 类型作为
不透明句柄在 ``core.update`` 的三步之间透传，本层不读其属性。``apply`` 留在主
线程同步调用：它只是启动 Update.exe 并退出进程，耗时极短，且成功即退出、
无所谓阻塞。

更新使用独立的单线程池，不与抓包、证书和会话任务争用全局池；空闲 30 秒后
回收线程。先完成任务收尾再公布结果，结果槽可立即下载或重试。

两个沿用 session 控制器的既定模式，勿「简化」掉：

- 任务对象进 ``self._tasks`` 集合持有，finished 时才丢弃 —— 池的 autoDelete 只
  管 C++ 侧，Python 包装（连同 signals 上的连接）没人持有时会在信号投递前被
  GC 收走，症状是「worker 跑完了但成功信号永远不到」。
- 槽一律用真实 ``@Slot`` 方法、跨调用状态放 ``self``，不在闭包里挂游离 lambda。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal, Slot

from ferret.apps.common.tasks import FunctionTask
from ferret.core import update as update_core
from ferret.core.log import get_logger

log = get_logger("settings")


class UpdateController(QObject):
    """「检查 → 下载 → 应用」的任务编排：防重入，结果经信号回主线程。"""

    check_started = Signal()
    busy_changed = Signal(bool)
    update_available = Signal(object, object)  # (不透明句柄, UpdateBrief)
    no_update = Signal()
    check_failed = Signal(str)
    check_finished = Signal()
    download_progress = Signal(int)
    download_finished = Signal(object)  # 不透明句柄
    download_failed = Signal(str)
    apply_failed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._pool.setExpiryTimeout(30_000)
        self._busy = False
        self._tasks: set[FunctionTask] = set()
        self._active_task: FunctionTask | None = None
        self._is_check = False
        self._result: Any = None
        self._error: str | None = None

    @property
    def busy(self) -> bool:
        return self._busy

    def check(self) -> None:
        """发起一次检查；任务在飞时直接忽略（防重入）。"""
        if self._busy:
            return
        self._start(update_core.check, is_check=True)

    def download(self, info: Any) -> None:
        """后台下载更新包；进度回调在工作线程上发信号（Qt 自动排队回主线程）。"""
        if self._busy:
            return
        self._start(self._download_work, info, is_check=False)

    def apply_and_restart(self, info: Any) -> None:
        """同步调用：成功即进程退出，失败才发 ``apply_failed``。"""
        try:
            update_core.apply_and_restart(info)
        except update_core.UpdateError as exc:
            log.warning("应用更新失败：%s", exc)
            self.apply_failed.emit(str(exc))

    def _download_work(self, info: Any) -> Any:
        update_core.download(info, self.download_progress.emit)
        return info

    def _start(self, fn: Callable[..., Any], *args: Any, is_check: bool) -> None:
        task = FunctionTask(fn, *args)
        task.setAutoDelete(True)
        self._tasks.add(task)
        self._active_task = task
        self._is_check = is_check
        self._busy = True
        task.signals.succeeded.connect(self._on_task_succeeded)
        task.signals.failed.connect(self._on_task_failed)
        task.signals.finished.connect(self._on_task_finished)
        if is_check:
            self.check_started.emit()
        self.busy_changed.emit(True)
        self._pool.start(task)

    @Slot(object)
    def _on_task_succeeded(self, result: Any) -> None:
        self._result = result

    @Slot(str)
    def _on_task_failed(self, message: str) -> None:
        self._error = message

    @Slot(object)
    def _on_check_done(self, result: Any) -> None:
        if result is None:
            self.no_update.emit()
        else:
            info, brief = result
            self.update_available.emit(info, brief)

    @Slot(object)
    def _on_download_done(self, info: Any) -> None:
        self.download_finished.emit(info)

    @Slot()
    def _on_task_finished(self) -> None:
        task = self._active_task
        assert task is not None
        is_check, result, error = self._is_check, self._result, self._error
        self._tasks.discard(task)
        task.signals.succeeded.disconnect()
        task.signals.failed.disconnect()
        task.signals.finished.disconnect()
        self._active_task = None
        self._is_check = False
        self._result = None
        self._error = None
        self._busy = False
        # 之后的信号可能立即启动下一项任务；旧任务不得再改动共享状态。
        self.busy_changed.emit(False)
        if error is not None:
            failure_signal = self.check_failed if is_check else self.download_failed
            failure_signal.emit(error)
        elif is_check:
            self._on_check_done(result)
        else:
            self._on_download_done(result)
        if is_check:
            self.check_finished.emit()
