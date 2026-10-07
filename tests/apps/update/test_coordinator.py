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
from ferret.core.meta import REPO_URL
from ferret.core.settings import CONFIG


class UpdateCoordinatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.addCleanup(
            QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete
        )
        self.window = QWidget()
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
        if error is None:
            self.controller.no_update.emit()
        else:
            self.controller.check_failed.emit(error)
        self.controller._busy = False
        self.controller.busy_changed.emit(False)
        self.controller.check_finished.emit()

    def test_auto_check_is_blocked_in_dev_mode(self) -> None:
        with patch.object(update_core, "update_supported", return_value=False):
            self.updates.check_auto()
        self.check.assert_not_called()

    def test_auto_check_respects_config_without_disabling_manual_check(self) -> None:
        CONFIG.set(CONFIG.auto_check_update, False, False)
        self.updates.check_auto()
        self.check.assert_not_called()
        self.updates.check_manual()
        self.check.assert_called_once_with()

    def test_unsupported_manual_check_opens_the_release_page(self) -> None:
        with (
            patch.object(update_core, "update_supported", return_value=False),
            patch.object(coordinator_module.QDesktopServices, "openUrl") as open_url,
        ):
            self.updates.check_manual()
        self.check.assert_not_called()
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toString(), f"{REPO_URL}/releases")

    def test_auto_check_no_update_and_failures_stay_silent(self) -> None:
        with (
            patch.object(coordinator_module, "show_success") as success,
            patch.object(coordinator_module, "show_warning") as warning,
        ):
            for error in (None, "network down"):
                self.updates.check_auto()
                self._finish_check(error)
            success.assert_not_called()
            warning.assert_not_called()

    def test_manual_check_reports_no_update_and_failures_on_the_main_window(
        self,
    ) -> None:
        with (
            patch.object(coordinator_module, "show_success") as success,
            patch.object(coordinator_module, "show_warning") as warning,
        ):
            self.updates.check_manual()
            self._finish_check()
            success.assert_called_once_with("", "当前已是最新版本", self.window)
            self.updates.check_manual()
            self._finish_check("network down")
            warning.assert_called_once_with("检查更新失败", "network down", self.window)

    def test_ignored_manual_request_does_not_make_auto_check_noisy(self) -> None:
        with (
            patch.object(coordinator_module, "show_success") as success,
            patch.object(coordinator_module, "show_warning") as warning,
        ):
            for error in (None, "network down"):
                self.updates.check_auto()
                self.updates.check_manual()
                self._finish_check(error)
            self.assertEqual(self.check.call_count, 2)
            success.assert_not_called()
            warning.assert_not_called()

    def test_ignored_auto_request_does_not_silence_manual_check(self) -> None:
        with (
            patch.object(coordinator_module, "show_success") as success,
            patch.object(coordinator_module, "show_warning") as warning,
        ):
            self.updates.check_manual()
            self.updates.check_auto()
            self._finish_check()
            self.updates.check_manual()
            self.updates.check_auto()
            self._finish_check("network down")
            self.assertEqual(self.check.call_count, 2)
            success.assert_called_once_with("", "当前已是最新版本", self.window)
            warning.assert_called_once_with("检查更新失败", "network down", self.window)

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

    def test_auto_update_opens_a_dialog_without_opening_settings(self) -> None:
        dialog = coordinator_module.UpdateDialog(self.brief, self.window)
        handle = object()
        self.updates.check_auto()

        with (
            patch.object(
                coordinator_module, "UpdateDialog", return_value=dialog
            ) as create_dialog,
            patch.object(dialog, "exec", return_value=0),
        ):
            self.controller.update_available.emit(handle, self.brief)

        create_dialog.assert_called_once_with(self.brief, self.window)
        self.assertIsNone(self.updates._update_dialog)

    def test_check_requests_are_ignored_while_the_update_dialog_is_open(self) -> None:
        dialog = coordinator_module.UpdateDialog(self.brief, self.window)
        states: list[bool] = []
        self.updates.state_changed.connect(lambda: states.append(self.updates.busy))
        self.updates.check_auto()

        def while_open() -> int:
            self.controller._busy = False
            self.controller.busy_changed.emit(False)
            self.controller.check_finished.emit()
            self.assertFalse(self.controller.busy)
            self.assertFalse(self.updates.checking)
            self.updates.check_manual()
            self.updates.check_auto()
            return 0

        with (
            patch.object(coordinator_module, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=while_open),
        ):
            self.updates._on_update_available(object(), self.brief)

        self.check.assert_called_once_with()
        self.assertFalse(self.updates.busy)
        self.assertEqual(states[-1], False)

    def test_dialog_download_and_restart_use_the_same_update_handle(self) -> None:
        dialog = coordinator_module.UpdateDialog(self.brief, self.window)
        handle = object()
        restart_handles: list[object] = []
        self.updates.restart_requested.connect(restart_handles.append)

        def while_open() -> int:
            dialog.download_requested.emit()
            self.controller.download_progress.emit(50)
            self.assertEqual(dialog.progress_bar.value(), 50)
            self.controller.download_finished.emit(handle)
            self.assertTrue(dialog.ready)
            return 1

        with (
            patch.object(coordinator_module, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=while_open),
            patch.object(self.controller, "download") as download,
        ):
            self.updates._on_update_available(handle, self.brief)

        download.assert_called_once_with(handle)
        self.assertEqual(restart_handles, [handle])

    def test_accepting_without_a_download_never_requests_restart(self) -> None:
        dialog = coordinator_module.UpdateDialog(self.brief, self.window)
        restart_handles: list[object] = []
        self.updates.restart_requested.connect(restart_handles.append)
        with (
            patch.object(coordinator_module, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", return_value=1),
        ):
            self.updates._on_update_available(object(), self.brief)
        self.assertEqual(restart_handles, [])

    def test_download_failure_is_displayed_in_the_active_dialog(self) -> None:
        dialog = coordinator_module.UpdateDialog(self.brief, self.window)

        def while_open() -> int:
            self.controller.download_failed.emit("download interrupted")
            self.assertEqual(dialog.error_label.text(), "download interrupted")
            return 0

        with (
            patch.object(coordinator_module, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=while_open),
            patch.object(coordinator_module, "show_warning") as warning,
        ):
            self.updates._on_update_available(object(), self.brief)
            self.controller.download_failed.emit("late failure")
            warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
