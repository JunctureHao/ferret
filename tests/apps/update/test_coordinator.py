from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.update.controllers import UpdateController
from ferret.apps.update.coordinator import UpdateCoordinator
from ferret.core import update as update_core
from ferret.core.meta import REPO_URL
from ferret.core.settings import CONFIG
from tests.core.mitm._qt import wait_until


class UpdateCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.addCleanup(
            QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete
        )
        self.window = QWidget()
        self.window.resize(960, 780)
        self.addCleanup(self.window.deleteLater)
        self.controller = UpdateController(self.window)
        self.updates = UpdateCoordinator(self.window, controller=self.controller)
        original_auto = CONFIG.get(CONFIG.auto_check_update)
        self.addCleanup(CONFIG.set, CONFIG.auto_check_update, original_auto, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)
        supported = patch.object(update_core, "update_supported", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)
        check = patch.object(self.controller, "check", side_effect=self._start_check)
        self.check = check.start()
        self.addCleanup(check.stop)
        self.brief = update_core.UpdateBrief(
            "1.0.0", "1.0.1", 128, "", "https://example.test/releases"
        )

    def _start_check(self) -> None:
        self.controller._busy = True
        self.controller.check_started.emit()
        self.controller.busy_changed.emit(True)

    def _finish_check(self, error: str | None = None) -> None:
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        if error is None:
            self.controller.no_update.emit()
        else:
            self.controller.check_failed.emit(error)
        self.controller.check_finished.emit()

    def _available(self, handle: object) -> None:
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        self.controller.update_available.emit(handle, self.brief)
        self.controller.check_finished.emit()

    def test_auto_check_is_blocked_in_dev_mode(self) -> None:
        with patch.object(update_core, "update_supported", return_value=False):
            self.updates.check_auto()
        self.check.assert_not_called()

    def test_disabling_auto_does_not_disable_manual_check(self) -> None:
        CONFIG.set(CONFIG.auto_check_update, False, False)
        self.updates.check_auto()
        self.check.assert_not_called()
        self.updates.check_manual()
        self.check.assert_called_once_with()

    def test_unsupported_manual_check_opens_the_release_page(self) -> None:
        with (
            patch.object(update_core, "update_supported", return_value=False),
            patch(
                "ferret.apps.update.coordinator.QDesktopServices.openUrl"
            ) as open_url,
        ):
            self.updates.show_dialog()
            dialog = self.updates._update_dialog
            assert dialog is not None
            dialog.yesButton.click()
        self.check.assert_not_called()
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), f"{REPO_URL}/releases")
        self.assertIs(self.updates._update_dialog, dialog)

    def test_background_results_are_retained_without_opening_a_dialog(self) -> None:
        self.updates.check_auto()
        self._finish_check("network down")
        self.assertIsNone(self.updates._update_dialog)
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        assert dialog is not None
        self.assertEqual(dialog.error_label.text(), "network down")
        self.assertIn("最近检查：", dialog.last_checked_label.text())

    def test_manual_check_reports_status_in_the_existing_dialog(self) -> None:
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        assert dialog is not None
        dialog.yesButton.click()
        self.assertFalse(dialog.yesButton.isEnabled())
        self.assertEqual(dialog.status_label.text(), "正在检查更新…")
        self._finish_check()
        self.assertTrue(dialog.yesButton.isEnabled())
        self.assertEqual(dialog.status_label.text(), "当前已是最新版本")
        self.assertIs(self.updates._update_dialog, dialog)

    def test_showing_dialog_is_single_instance_and_does_not_check(self) -> None:
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        self.updates.show_dialog()
        self.assertIs(self.updates._update_dialog, dialog)
        self.check.assert_not_called()
        assert dialog is not None
        dialog.reject()
        self.assertTrue(wait_until(lambda: self.updates._update_dialog is None))
        self.updates.show_dialog()
        self.assertIsNot(self.updates._update_dialog, dialog)
        self.check.assert_not_called()

    def test_reentrant_requests_do_not_change_active_check_origin(self) -> None:
        for auto in (True, False):
            (self.updates.check_auto if auto else self.updates.check_manual)()
            self.updates.check_auto()
            self.updates.check_manual()
            self.assertEqual(self.updates._auto_check, auto)
            self._finish_check()
        self.assertEqual(self.check.call_count, 2)

    def test_check_state_is_observable_without_a_settings_page(self) -> None:
        states: list[tuple[bool, bool]] = []
        self.updates.state_changed.connect(
            lambda: states.append((self.updates.busy, self.updates.checking))
        )
        self.updates.check_auto()
        self.assertTrue(self.updates.busy)
        self.assertTrue(self.updates.checking)
        self._finish_check()
        self.assertFalse(self.updates.busy)
        self.assertFalse(self.updates.checking)
        self.assertIn((True, True), states)
        self.assertEqual(states[-1], (False, False))

    def test_repeated_available_results_only_publish_badge_state(self) -> None:
        states: list[bool] = []
        self.updates.state_changed.connect(
            lambda: states.append(self.updates.has_update)
        )
        for _ in range(2):
            self.updates.check_auto()
            self._available(object())
        self.assertTrue(states[-1])
        self.assertIsNone(self.updates._update_dialog)
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        assert dialog is not None
        self.assertEqual(dialog.yesButton.text(), "下载更新")
        self.assertIn("1.0.1", dialog.desc_label.text())

    def test_download_survives_closed_dialog_and_restart_uses_original_handle(
        self,
    ) -> None:
        handle = object()
        self._available(handle)
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        assert dialog is not None

        def downloading(info: object) -> None:
            self.assertIs(info, handle)
            self.controller._busy = True
            self.controller.busy_changed.emit(True)

        with patch.object(
            self.controller, "download", side_effect=downloading
        ) as download:
            dialog.yesButton.click()
        download.assert_called_once_with(handle)
        self.controller.download_progress.emit(50)
        self.assertEqual(dialog.progress_bar.value(), 50)
        self.assertTrue(dialog.cancelButton.isEnabled())
        dialog.reject()
        self.assertTrue(wait_until(lambda: self.updates._update_dialog is None))
        self.controller.download_progress.emit(75)
        self.updates.show_dialog()
        reopened = self.updates._update_dialog
        assert reopened is not None
        self.assertEqual(reopened.progress_bar.value(), 75)
        self.assertFalse(reopened.yesButton.isEnabled())

        restarts: list[object] = []
        self.updates.restart_requested.connect(restarts.append)
        self.updates._request_restart()
        self.assertEqual(restarts, [])
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        self.controller.download_finished.emit(handle)
        self.assertTrue(reopened.ready)
        self.assertEqual(reopened.yesButton.text(), "重启更新")
        self.updates.check_auto()
        self.check.assert_not_called()
        reopened.yesButton.click()
        self.assertEqual(restarts, [handle])

    def test_download_and_apply_errors_keep_retry_state(self) -> None:
        handle = object()
        self._available(handle)
        self.updates.show_dialog()
        dialog = self.updates._update_dialog
        assert dialog is not None
        self.controller.download_failed.emit("download interrupted")
        self.assertEqual(dialog.error_label.text(), "download interrupted")
        self.assertEqual(dialog.yesButton.text(), "重试下载")
        with patch.object(self.controller, "download") as download:
            dialog.yesButton.click()
        download.assert_called_once_with(handle)

        self.controller.download_finished.emit(handle)
        self.updates.stop_auto_checking()
        self.controller.apply_failed.emit("apply denied")
        self.updates.start_auto_checking()
        self.assertTrue(dialog.ready)
        self.assertEqual(dialog.error_label.text(), "apply denied")
        restarts: list[object] = []
        self.updates.restart_requested.connect(restarts.append)
        dialog.yesButton.click()
        self.assertEqual(restarts, [handle])
        self.updates.stop_auto_checking()


if __name__ == "__main__":
    unittest.main()
