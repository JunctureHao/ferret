"""共用的后台任务包装：把阻塞调用挪出 Qt 主线程。

只做「跑一个可调用对象，成功/失败各发一个信号」这一件事，业务语义留给各页的
controller。原本内嵌在 `apps/session/controllers.py`，证书页也要用，故上移共用。
"""

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal

from ferret.core.log import get_logger

log = get_logger("tasks")


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
