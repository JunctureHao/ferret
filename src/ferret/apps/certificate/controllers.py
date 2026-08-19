"""证书页控制器：把阻塞的 certutil 与文件操作挪出 Qt 主线程。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, Signal

from ferret.apps.certificate.models import CertificateState
from ferret.apps.common.tasks import FunctionTask
from ferret.core.log import get_logger
from ferret.core.mitm import (
    CertificateCancelled,
    CertificateError,
    MitmFacade,
    SystemCertificateService,
    export_format,
)

log = get_logger("certificate")


class CertificateController(QObject):
    """CA 证书的唯一操作入口。

    certutil 一次查询实测 90~400ms，生成一套 CA 约 65ms，全部走线程池；
    池限一个线程，保证「先卸载旧的、再装新的」这类连续操作不会互相插队。
    """

    state_changed = Signal(object)  # CertificateState
    busy_changed = Signal(bool)
    operation_failed = Signal(str, str)  # title, detail
    operation_succeeded = Signal(str)
    exported = Signal(object)  # Path

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        mitm: MitmFacade,
        service: SystemCertificateService | None = None,
    ) -> None:
        super().__init__(parent)
        self._mitm = mitm
        self._service = service or SystemCertificateService()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._tasks: set[FunctionTask] = set()
        self._active = 0
        self._state = CertificateState()

    @property
    def state(self) -> CertificateState:
        return self._state

    @property
    def certs_dir(self) -> Path:
        return self._service.certs_dir

    @property
    def busy(self) -> bool:
        return self._active > 0

    # --- 对外动作 ---

    def refresh(self) -> None:
        self._run(self._snapshot, fail_title=self.tr("检测失败"), recover=False)

    def install(self) -> None:
        self._run(
            self._install,
            fail_title=self.tr("安装失败"),
            message=self.tr("证书已安装到系统信任库"),
        )

    def uninstall(self) -> None:
        self._run(
            self._uninstall,
            fail_title=self.tr("卸载失败"),
            message=self.tr("证书已从系统信任库移除"),
        )

    def regenerate(self) -> None:
        self._run(
            self._regenerate,
            fail_title=self.tr("重新生成失败"),
            message=self.tr("已重新生成 CA 证书，请重新安装"),
            after=self._reload_store,
        )

    def export(self, key: str, target: Path | str) -> None:
        try:
            fmt = export_format(key)
        except CertificateError as exc:
            self.operation_failed.emit(self.tr("导出失败"), str(exc))
            return
        self._run(
            self._service.export,
            fmt,
            target,
            fail_title=self.tr("导出失败"),
            on_success=self._on_exported,
        )

    # --- 后台线程里跑的部分 ---

    def _snapshot(self) -> CertificateState:
        """先查信任库再读证书文件：慢的那步（certutil）已经在后台线程里。"""
        return CertificateState(
            trust=self._service.trust_state(),
            info=self._service.load(),
        )

    def _state_after(self, action: Callable[[], Any]) -> CertificateState | None:
        """跑一个信任库操作，返回操作后的状态；用户点「否」时返回 None。

        `certutil -addstore/-delstore` 会弹 Windows 的安全警告，点「否」不是失败，
        是「什么都没做」。返回 None 让 `_run` 走取消分支：不报错、不弹成功提示，
        只把真实状态重新查一遍刷回界面。
        """
        try:
            action()
        except CertificateCancelled as exc:
            log.info("%s", exc)
            return None
        return self._snapshot()

    def _install(self) -> CertificateState | None:
        return self._state_after(self._service.install)

    def _uninstall(self) -> CertificateState | None:
        return self._state_after(self._service.uninstall)

    def _regenerate(self) -> CertificateState:
        self._service.regenerate()
        return self._snapshot()

    # --- 主线程回调 ---

    def _apply_state(self, state: CertificateState) -> None:
        self._state = state
        self.state_changed.emit(state)

    def _on_exported(self, path: object) -> None:
        self.exported.emit(path)
        self.operation_succeeded.emit(self.tr("已导出到 {}").format(path))

    def _reload_store(self) -> None:
        """让正在跑的内核换用新 CA。失败只记日志：下次启动自然会读到新证书。"""
        try:
            if self._mitm.reload_certificate_store():
                log.info("mitmproxy 已重新加载 CertStore")
        except (RuntimeError, TimeoutError) as exc:
            log.warning("新证书未能热加载: %s", exc)

    def _set_active(self, delta: int) -> None:
        was_busy = self._active > 0
        self._active = max(0, self._active + delta)
        if (self._active > 0) != was_busy:
            self.busy_changed.emit(self._active > 0)

    def _run(
        self,
        fn: Callable[..., Any],
        *args: Any,
        fail_title: str,
        message: str = "",
        on_success: Callable[[Any], None] | None = None,
        after: Callable[[], None] | None = None,
        recover: bool = True,
    ) -> None:
        self._set_active(1)
        task = FunctionTask(fn, *args)
        task.setAutoDelete(True)
        self._tasks.add(task)

        def _succeeded(result: Any) -> None:
            if result is None:
                # 后台函数返回 None = 用户在系统弹窗里点了「否」：什么都没发生，
                # 既不该报错也不该弹成功提示，只补一次检测把界面对齐真实状态。
                self._run(self._snapshot, fail_title=fail_title, recover=False)
                return
            if on_success is not None:
                on_success(result)
            elif isinstance(result, CertificateState):
                self._apply_state(result)
            if after is not None:
                after()
            if message:
                self.operation_succeeded.emit(message)

        def _failed(detail: str) -> None:
            self.operation_failed.emit(fail_title, detail)
            # 失败后状态可能已经变了（比如装了一半），补一次检测再刷新界面。
            # recover=False 用于检测任务自身，避免检测出错时无限递归。
            if recover:
                self._run(self._snapshot, fail_title=fail_title, recover=False)

        def _finished() -> None:
            self._set_active(-1)
            self._tasks.discard(task)

        task.signals.succeeded.connect(_succeeded)
        task.signals.failed.connect(_failed)
        task.signals.finished.connect(_finished)
        self._pool.start(task)
