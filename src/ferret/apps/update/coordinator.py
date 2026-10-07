"""主窗口持有的更新入口与提示，生命周期独立于设置页。

启动定时器和设置页的手动按钮共用这个对象；设置页晚构造时只读取当前状态，
不发起检查。这里只常驻 QObject，发现新版本时才创建更新对话框。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget

from ferret.apps.common.info_bar import show_success, show_warning
from ferret.apps.update.controllers import UpdateController
from ferret.apps.update.dialogs import UpdateDialog
from ferret.core import update as update_core
from ferret.core.meta import REPO_URL
from ferret.core.settings import CONFIG
from ferret.core.update import UpdateBrief


class UpdateCoordinator(QObject):
    state_changed = Signal()
    restart_requested = Signal(object)

    def __init__(
        self, parent: QWidget, *, controller: UpdateController | None = None
    ) -> None:
        super().__init__(parent)
        self._window = parent
        self.controller = controller or UpdateController(self)
        self._update_dialog: UpdateDialog | None = None
        self._auto_check = False
        self._checking = False
        self.controller.check_started.connect(self._on_check_started)
        self.controller.busy_changed.connect(self._on_busy_changed)
        self.controller.no_update.connect(self._on_no_update)
        self.controller.check_failed.connect(self._on_check_failed)
        self.controller.update_available.connect(self._on_update_available)
        self.controller.download_failed.connect(self._on_download_failed)
        self.controller.apply_failed.connect(self._on_apply_failed)

    @property
    def busy(self) -> bool:
        return self.controller.busy

    @property
    def checking(self) -> bool:
        return self._checking

    @Slot()
    def check_auto(self) -> None:
        """启动时静默检查一次；不支持原地更新或用户关闭开关时跳过。"""
        if CONFIG.get(CONFIG.auto_check_update):
            self._check(auto=True)

    @Slot()
    def check_manual(self) -> None:
        self._check(auto=False)

    def _check(self, *, auto: bool) -> None:
        # 被忽略的请求不能改掉正在执行的检查的静默语义；弹窗存续时也不重开。
        if self.busy or self._update_dialog is not None:
            return
        if not update_core.update_supported():
            if not auto:
                QDesktopServices.openUrl(QUrl(f"{REPO_URL}/releases"))
            return
        self._auto_check = auto
        self.controller.check()

    @Slot()
    def _on_check_started(self) -> None:
        self._checking = True

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        if not busy:
            self._checking = False
        self.state_changed.emit()

    @Slot()
    def _on_no_update(self) -> None:
        if not self._auto_check:
            show_success("", self.tr("当前已是最新版本"), self._window)

    @Slot(str)
    def _on_check_failed(self, message: str) -> None:
        # 自动入口的错误已由 FunctionTask 记入日志，只让手动入口弹出提示。
        if not self._auto_check:
            show_warning(self.tr("检查更新失败"), message, self._window)

    @Slot(object, object)
    def _on_update_available(self, info: object, brief: UpdateBrief) -> None:
        dialog = UpdateDialog(brief, self._window)
        try:
            self._update_dialog = dialog
            dialog.download_requested.connect(
                lambda: self._start_download(dialog, info)
            )
            self.controller.download_progress.connect(dialog.set_progress)
            self.controller.download_finished.connect(dialog.set_ready)
            if dialog.exec() and dialog.ready:
                # 应用更新会直接退出进程，必须由主窗口先走统一 shutdown。
                self.restart_requested.emit(info)
        finally:
            self.controller.download_progress.disconnect(dialog.set_progress)
            self.controller.download_finished.disconnect(dialog.set_ready)
            self._update_dialog = None
            dialog.deleteLater()

    def _start_download(self, dialog: UpdateDialog, info: object) -> None:
        dialog.set_downloading()
        self.controller.download(info)

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        if self._update_dialog is not None:
            self._update_dialog.set_failed(message)

    @Slot(str)
    def _on_apply_failed(self, message: str) -> None:
        # 重启请求发出时下载对话框已隐藏，失败必须仍可见。
        show_warning(self.tr("应用更新失败"), message, self._window)
