"""自动更新的时钟与生命周期测试；网络任务由可控的信号链代替。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.update import coordinator as coordinator_module
from ferret.apps.update.controllers import UpdateController
from ferret.apps.update.coordinator import UpdateCoordinator
from ferret.core import update as update_core
from ferret.core.settings import CONFIG
from tests.core.mitm._qt import wait_until


class UpdateScheduleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        original_auto = CONFIG.get(CONFIG.auto_check_update)
        self.addCleanup(CONFIG.set, CONFIG.auto_check_update, original_auto, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)
        self.now = 1_750_000_000.0
        clock = patch.object(
            coordinator_module.time, "time", side_effect=lambda: self.now
        )
        clock.start()
        self.addCleanup(clock.stop)
        supported = patch.object(update_core, "update_supported", return_value=True)
        self.supported = supported.start()
        self.addCleanup(supported.stop)
        self.window = QWidget()
        self.controller = UpdateController(self.window)
        self.updates = UpdateCoordinator(self.window, controller=self.controller)
        self.handle = object()
        self.brief = update_core.UpdateBrief(
            "1.0.0", "1.0.1", 128, "", "https://example.test/releases"
        )
        check = patch.object(self.controller, "check", side_effect=self._start_check)
        self.check = check.start()
        self.addCleanup(check.stop)
        download = patch.object(
            self.controller, "download", side_effect=self._start_download
        )
        self.download = download.start()
        self.addCleanup(download.stop)
        self.addCleanup(self._dispose)

    def _dispose(self) -> None:
        self.updates.stop_auto_checking()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _start_check(self) -> None:
        self.controller._busy = True
        self.controller.check_started.emit()
        self.controller.busy_changed.emit(True)

    def _finish_check(
        self, error: str | None = None, *, available: bool = False
    ) -> None:
        # 与真实 Controller 一致：先退出 busy，再交付结果与 finished。
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        if error is not None:
            self.controller.check_failed.emit(error)
        elif available:
            self.controller.update_available.emit(self.handle, self.brief)
        else:
            self.controller.no_update.emit()
        self.controller.check_finished.emit()

    def _start_download(self, _info: object) -> None:
        self.controller._busy = True
        self.controller.busy_changed.emit(True)

    def _finish_download(self, error: str | None = None) -> None:
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        if error is None:
            self.controller.download_finished.emit(self.handle)
        else:
            self.controller.download_failed.emit(error)

    def _fire_timer(self) -> None:
        # 模拟单次 QTimer 到期；不等待真实的分钟或小时。
        self.updates._timer.stop()
        self.updates._timer.timeout.emit()

    def _fire_due(self) -> None:
        deadline = self.updates._next_check_at
        self.assertIsNotNone(deadline)
        assert deadline is not None
        self.now = deadline
        self._fire_timer()

    def _assert_next_in(self, seconds: int) -> None:
        self.assertEqual(self.updates._next_check_at, self.now + seconds)
        self.assertTrue(self.updates._timer.isActive())

    def test_startup_waits_five_seconds(self) -> None:
        self.assertIsNone(self.updates._next_check_at)
        self.assertFalse(self.updates._timer.isActive())

        self.updates.start_auto_checking()

        self._assert_next_in(5)
        self.check.assert_not_called()
        self.now += 4
        self._fire_timer()
        self.check.assert_not_called()
        self._assert_next_in(1)
        self._fire_due()
        self.check.assert_called_once_with()

    def test_success_repeats_one_hour_after_completion(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self.now += 90
        self._finish_check()

        self._assert_next_in(3600)
        self.now += 3599
        self._fire_timer()
        self.check.assert_called_once_with()
        self._fire_due()
        self.assertEqual(self.check.call_count, 2)
        self.now += 30
        self._finish_check()
        self._assert_next_in(3600)

    def test_failures_retry_after_five_and_fifteen_minutes_then_reset(self) -> None:
        self.updates.start_auto_checking()
        for attempt, delay in enumerate((300, 900, 3600, 300), start=1):
            with self.subTest(attempt=attempt):
                self._fire_due()
                self.now += 2
                self._finish_check("network unavailable")
                self._assert_next_in(delay)
                self.assertEqual(self.check.call_count, attempt)

    def test_manual_success_resets_the_hour_and_failure_backoff(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self._finish_check("network unavailable")
        self.now += 10

        self.updates.check_manual()
        self.now += 20
        self._finish_check()

        self._assert_next_in(3600)
        self._fire_due()
        self._finish_check("network unavailable")
        self._assert_next_in(300)

    def test_disabling_auto_cancels_the_timer_and_enabling_waits_five_seconds(
        self,
    ) -> None:
        self.updates.start_auto_checking()
        CONFIG.set(CONFIG.auto_check_update, False, False)

        self.assertFalse(self.updates._timer.isActive())
        self.assertIsNone(self.updates._next_check_at)
        self.now += 7200
        self._fire_timer()
        self.check.assert_not_called()

        CONFIG.set(CONFIG.auto_check_update, True, False)

        self._assert_next_in(5)
        self._fire_due()
        self.check.assert_called_once_with()

    def test_disabling_during_check_does_not_restart_after_a_late_result(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        CONFIG.set(CONFIG.auto_check_update, False, False)

        self._finish_check("network unavailable")

        self.assertFalse(self.updates._timer.isActive())
        self.assertIsNone(self.updates._next_check_at)
        CONFIG.set(CONFIG.auto_check_update, True, False)
        self._assert_next_in(5)

    def test_requests_during_check_do_not_start_another_check(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self.updates.check_auto()
        self.updates.check_manual()
        self.updates.check_auto()
        self.check.assert_called_once_with()

        self._finish_check()
        QCoreApplication.processEvents()

        self.check.assert_called_once_with()
        self._assert_next_in(3600)

    def test_overdue_check_preserves_a_successfully_downloaded_update(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self._finish_check(available=True)
        self._assert_next_in(3600)
        self.updates.download()
        self.download.assert_called_once_with(self.handle)

        self._fire_due()
        self.updates.check_auto()
        self.updates.check_manual()
        self.check.assert_called_once_with()
        self._finish_download()
        QCoreApplication.processEvents()

        self.check.assert_called_once_with()
        self._assert_next_in(3600)
        self._fire_due()
        self.check.assert_called_once_with()
        self._assert_next_in(3600)
        restart_handles: list[object] = []
        self.updates.restart_requested.connect(restart_handles.append)
        self.updates._request_restart()
        self.assertEqual(restart_handles, [self.handle])

    def test_download_failure_runs_a_single_overdue_check_after_releasing_busy(
        self,
    ) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self._finish_check(available=True)
        self.updates.download()
        self._fire_due()
        self.updates.check_auto()
        self.updates.check_auto()
        self.check.assert_called_once_with()

        self._finish_download("download interrupted")

        self.assertTrue(wait_until(lambda: self.check.call_count == 2, timeout_ms=1000))
        self._finish_check()
        QCoreApplication.processEvents()
        self.assertEqual(self.check.call_count, 2)
        self._assert_next_in(3600)

    def test_an_overdue_timer_after_sleep_checks_once_without_catching_up(self) -> None:
        self.updates.start_auto_checking()
        self.now += 12 * 3600

        self._fire_timer()

        self.check.assert_called_once_with()
        self.now += 3
        self._finish_check()
        self._assert_next_in(3600)
        self._fire_timer()
        QCoreApplication.processEvents()
        self.check.assert_called_once_with()

    def test_clock_going_backwards_does_not_check_before_the_deadline(self) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self._finish_check()
        deadline = self.updates._next_check_at
        self.now -= 7200

        self._fire_timer()

        self.assertEqual(self.updates._next_check_at, deadline)
        self.assertTrue(self.updates._timer.isActive())
        self.assertLessEqual(self.updates._timer.interval(), 60_000)
        self.check.assert_called_once_with()

    def test_hiding_the_parent_window_keeps_timer_delivery_active(self) -> None:
        self.window.show()
        self.updates.start_auto_checking()
        self.window.hide()
        deadline = self.updates._next_check_at
        assert deadline is not None
        self.now = deadline

        self.updates._timer.start(0)

        self.assertTrue(wait_until(lambda: self.check.call_count == 1, timeout_ms=1000))
        self.assertFalse(self.window.isVisible())

    def test_shutdown_blocks_late_results_and_future_configuration_changes(
        self,
    ) -> None:
        self.updates.start_auto_checking()
        self._fire_due()
        self.updates.check_auto()

        self.updates.stop_auto_checking()
        self._finish_check("late network failure")
        CONFIG.set(CONFIG.auto_check_update, False, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)
        self.now += 7200
        self._fire_timer()
        QCoreApplication.processEvents()

        self.assertFalse(self.updates._timer.isActive())
        self.assertIsNone(self.updates._next_check_at)
        self.check.assert_called_once_with()

    def test_unsupported_build_does_not_schedule_automatic_checks(self) -> None:
        self.supported.return_value = False

        self.updates.start_auto_checking()
        CONFIG.set(CONFIG.auto_check_update, False, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)
        self.updates.check_auto()

        self.assertFalse(self.updates._timer.isActive())
        self.assertIsNone(self.updates._next_check_at)
        self.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
