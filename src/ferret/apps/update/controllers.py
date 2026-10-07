"""应用内更新的后台任务编排（docs/design.md#update）。

检查/下载是阻塞网络 IO，一律经 ``FunctionTask`` 挪出主线程；velopack 类型作为
不透明句柄在 ``core.update`` 的三步之间透传，本层不读其属性。``apply`` 留在主
线程同步调用：它只是启动 Update.exe 并退出进程，耗时极短，且成功即退出、
无所谓阻塞。

两个沿用 session 控制器的既定模式，勿「简化」掉：

- 任务对象进 ``self._tasks`` 集合持有，finished 时才丢弃 —— 池的 autoDelete 只
  管 C++ 侧，Python 包装（连同 signals 上的连接）没人持有时会在信号投递前被
  GC 收走，症状是「worker 跑完了但成功信号永远不到」。
- 槽一律用真实 ``@Slot`` 方法、跨调用状态放 ``self``，不在闭包里挂游离 lambda。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal, SignalInstance, Slot

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
        self._busy = False
        self._tasks: set[FunctionTask] = set()
        self._download_info: Any = None

    @property
    def busy(self) -> bool:
        return self._busy

    def check(self) -> None:
        """发起一次检查；任务在飞时直接忽略（防重入）。"""
        if self._busy:
            return
        self._busy = True
        self.check_started.emit()
        self.busy_changed.emit(True)
        self._start(update_core.check, self._on_check_done, self.check_failed)

    def download(self, info: Any) -> None:
        """后台下载更新包；进度回调在工作线程上发信号（Qt 自动排队回主线程）。"""
        if self._busy:
            return
        self._busy = True
        self._download_info = info
        self.busy_changed.emit(True)
        self._start(self._download_work, self._on_download_done, self.download_failed)

    def apply_and_restart(self, info: Any) -> None:
        """同步调用：成功即进程退出，失败才发 ``apply_failed``。"""
        try:
            update_core.apply_and_restart(info)
        except update_core.UpdateError as exc:
            log.warning("应用更新失败：%s", exc)
            self.apply_failed.emit(str(exc))

    def _download_work(self) -> None:
        update_core.download(self._download_info, self.download_progress.emit)

    def _start(self, fn, on_success, failure_signal: SignalInstance) -> None:
        task = FunctionTask(fn)
        task.setAutoDelete(True)
        self._tasks.add(task)
        # 具名闭包而非游离 lambda（守则见模块 docstring）：收尾要拿到当次 task
        # 才能拆连接环，与 session 控制器的 _on_finished 同款。
        def on_finished() -> None:
            self._on_task_finished(task)

        task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(failure_signal)
        task.signals.finished.connect(on_finished)
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_check_done(self, result: Any) -> None:
        if result is None:
            self.no_update.emit()
        else:
            info, brief = result
            self.update_available.emit(info, brief)

    @Slot(object)
    def _on_download_done(self, _result: Any) -> None:
        info, self._download_info = self._download_info, None
        self.download_finished.emit(info)

    def _on_task_finished(self, task: FunctionTask) -> None:
        self._busy = False
        self._tasks.discard(task)
        self._download_info = None
        task.signals.succeeded.disconnect()
        task.signals.failed.disconnect()
        task.signals.finished.disconnect()
        self.busy_changed.emit(False)
        self.check_finished.emit()
