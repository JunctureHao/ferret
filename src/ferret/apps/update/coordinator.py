"""应用级更新状态、周期检查与按需显示的版本窗口。

只保存一次检查的摘要和 SDK 句柄，窗口关闭不影响后台任务。单次定时器按完成
时间重排；等待期间不占用工作线程，也不会在电脑恢复后补发积压的多轮检查。
"""

from __future__ import annotations

import math
import time
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QWidget

from ferret.apps.update.controllers import UpdateController
from ferret.apps.update.dialogs import UpdateDialog
from ferret.core import update as update_core
from ferret.core.meta import REPO_URL
from ferret.core.settings import CONFIG
from ferret.core.update import UpdateBrief

STARTUP_DELAY_MS = 5_000
CHECK_INTERVAL_MS = 60 * 60 * 1_000
RETRY_DELAYS_MS = (5 * 60 * 1_000, 15 * 60 * 1_000)
# 用墙钟记录期限，兼容休眠和时钟调整。每分钟至多唤醒一次 Qt 定时器做时间比较，
# 不发网络请求、不创建线程；避免平台定时器暂停于休眠期间而延后整整一小时。
MAX_TIMER_INTERVAL_MS = 60_000


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
        self._status = "idle"
        self._brief: UpdateBrief | None = None
        self._info: object | None = None
        self._ready = False
        self._last_checked: datetime | None = None
        self._error = ""
        self._progress = 0

        self._auto_running = False
        self._shutdown = False
        self._auto_due = False
        self._retry_index = 0
        self._next_check_at: float | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_timer)
        CONFIG.auto_check_update.valueChanged.connect(self._on_auto_preference_changed)

        self.controller.check_started.connect(self._on_check_started)
        self.controller.busy_changed.connect(self._on_busy_changed)
        self.controller.no_update.connect(self._on_no_update)
        self.controller.check_failed.connect(self._on_check_failed)
        self.controller.update_available.connect(self._on_update_available)
        self.controller.download_progress.connect(self._on_download_progress)
        self.controller.download_finished.connect(self._on_download_finished)
        self.controller.download_failed.connect(self._on_download_failed)
        self.controller.apply_failed.connect(self._on_apply_failed)

    @property
    def busy(self) -> bool:
        return self.controller.busy

    @property
    def checking(self) -> bool:
        return self._checking

    @property
    def has_update(self) -> bool:
        return self._brief is not None

    def start_auto_checking(self) -> None:
        """由主窗口装配时启动；重复调用不推迟已有期限。"""
        self._shutdown = False
        if self._auto_running:
            return
        self._auto_running = True
        self._retry_index = 0
        self._schedule_next(STARTUP_DELAY_MS)

    def stop_auto_checking(self) -> None:
        """真正退出时撤下调度，忽略在途任务迟到的结果；不强杀 SDK 线程。"""
        self._shutdown = True
        self._auto_running = False
        self._cancel_timer()

    def _can_schedule(self) -> bool:
        return (
            self._auto_running
            and not self._shutdown
            and bool(CONFIG.get(CONFIG.auto_check_update))
            and update_core.update_supported()
        )

    def _cancel_timer(self) -> None:
        self._timer.stop()
        self._next_check_at = None
        self._auto_due = False

    def _schedule_next(self, delay_ms: int) -> None:
        self._cancel_timer()
        if self._can_schedule():
            self._next_check_at = time.time() + delay_ms / 1000
            self._arm_timer()

    def _arm_timer(self) -> None:
        if self._next_check_at is not None:
            remaining_ms = math.ceil((self._next_check_at - time.time()) * 1000)
            self._timer.start(max(1, min(remaining_ms, MAX_TIMER_INTERVAL_MS)))

    @Slot()
    def _on_auto_preference_changed(self) -> None:
        self._retry_index = 0
        self._schedule_next(STARTUP_DELAY_MS)

    @Slot()
    def _on_timer(self) -> None:
        if not self._can_schedule():
            self._cancel_timer()
            return
        if self._next_check_at is None:
            return
        if time.time() < self._next_check_at:
            self._arm_timer()
            return
        self._timer.stop()
        self._next_check_at = None
        self.check_auto()

    @Slot()
    def check_auto(self) -> None:
        if not self._shutdown and CONFIG.get(CONFIG.auto_check_update):
            self._check(auto=True)

    @Slot()
    def check_manual(self) -> None:
        self._check(auto=False)

    def _check(self, *, auto: bool) -> None:
        if self._shutdown:
            return
        if self.busy:
            if auto and self._auto_running:
                self._auto_due = True
            return
        if not update_core.update_supported():
            if not auto:
                QDesktopServices.openUrl(QUrl(f"{REPO_URL}/releases"))
            return
        # 已下载的句柄必须保留到应用更新，不能被下一轮检查换成另一个版本。
        if self._ready:
            if auto:
                self._schedule_next(CHECK_INTERVAL_MS)
            return
        self._cancel_timer()
        if not auto:
            self._retry_index = 0
        self._auto_check = auto
        self._error = ""
        self.controller.check()

    @Slot()
    def _on_check_started(self) -> None:
        self._checking = True
        self._status = "checking"

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        if not busy:
            self._checking = False
            if self._auto_due and self._can_schedule():
                # busy 先于结果发出；到下一轮事件才补查，让成功/失败/ready 先落定。
                self._next_check_at = time.time()
                self._timer.start(1)
        self._publish()

    def _record_check(self) -> None:
        self._last_checked = datetime.fromtimestamp(time.time())

    @Slot()
    def _on_no_update(self) -> None:
        if self._shutdown:
            return
        self._record_check()
        self._brief = None
        self._info = None
        self._ready = False
        self._error = ""
        self._status = "latest"
        self._retry_index = 0
        self._schedule_next(CHECK_INTERVAL_MS)
        self._publish()

    @Slot(str)
    def _on_check_failed(self, message: str) -> None:
        if self._shutdown:
            return
        self._record_check()
        self._status = "check_failed"
        self._error = message
        if self._retry_index < len(RETRY_DELAYS_MS):
            delay = RETRY_DELAYS_MS[self._retry_index]
            self._retry_index += 1
        else:
            delay = CHECK_INTERVAL_MS
            self._retry_index = 0
        self._schedule_next(delay)
        self._publish()

    @Slot(object, object)
    def _on_update_available(self, info: object, brief: UpdateBrief) -> None:
        if self._shutdown:
            return
        self._record_check()
        self._info = info
        self._brief = brief
        self._ready = False
        self._progress = 0
        self._error = ""
        self._status = "available"
        self._retry_index = 0
        self._schedule_next(CHECK_INTERVAL_MS)
        self._publish()

    @Slot()
    def download(self) -> None:
        if self._shutdown or self.busy or self._info is None or self._ready:
            return
        self._status = "downloading"
        self._progress = 0
        self._error = ""
        self.controller.download(self._info)

    @Slot(int)
    def _on_download_progress(self, percent: int) -> None:
        if not self._shutdown:
            self._progress = max(0, min(100, percent))
            self._publish()

    @Slot(object)
    def _on_download_finished(self, info: object) -> None:
        if self._shutdown:
            return
        self._info = info
        self._ready = True
        self._progress = 100
        self._status = "ready"
        self._error = ""
        self._schedule_next(CHECK_INTERVAL_MS)
        self._publish()

    @Slot(str)
    def _on_download_failed(self, message: str) -> None:
        if not self._shutdown:
            self._status = "download_failed"
            self._error = message
            self._publish()

    @Slot()
    def _request_restart(self) -> None:
        if (
            not self._shutdown
            and not self.busy
            and self._ready
            and self._info is not None
        ):
            self.restart_requested.emit(self._info)

    @Slot(str)
    def _on_apply_failed(self, message: str) -> None:
        # 主窗口已先停止调度再调用 SDK；失败后窗口会恢复调度，错误仍须保留。
        self._status = "apply_failed"
        self._error = message
        self._publish()

    @Slot()
    def show_dialog(self) -> None:
        if self._shutdown:
            return
        if self._update_dialog is None:
            dialog = UpdateDialog(
                current_version=update_core.current_version(),
                supported=update_core.update_supported(),
                parent=self._window,
            )
            self._update_dialog = dialog
            dialog.check_requested.connect(self.check_manual)
            dialog.download_requested.connect(self.download)
            dialog.restart_requested.connect(self._request_restart)
            dialog.finished.connect(self._on_dialog_finished)
        self._render_dialog()
        self._update_dialog.show()
        self._update_dialog.raise_()

    @Slot(int)
    def _on_dialog_finished(self, _result: int) -> None:
        dialog, self._update_dialog = self._update_dialog, None
        if dialog is not None:
            dialog.deleteLater()

    def _render_dialog(self) -> None:
        if self._update_dialog is not None:
            self._update_dialog.render_state(
                status=self._status
                if update_core.update_supported()
                else "unsupported",
                brief=self._brief,
                last_checked=self._last_checked,
                error=self._error,
                progress=self._progress,
                busy=self.busy,
                ready=self._ready,
            )

    def _publish(self) -> None:
        self._render_dialog()
        self.state_changed.emit()
