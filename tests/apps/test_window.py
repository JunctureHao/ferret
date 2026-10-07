from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ferret.apps import window as window_module
from ferret.apps.capture.controllers import CaptureState
from ferret.apps.common.lazy_page import LazyPage
from ferret.apps.session.views import SessionsInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.core import update as update_core
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import CONFIG
from tests.core.mitm._qt import wait_until


class StartupLazyLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def create_window(self):
        with (
            TemporaryDirectory() as directory,
            patch("ferret.core.runtime.get_config_dir", return_value=Path(directory)),
            patch("ferret.core.settings.get_config_dir", return_value=Path(directory)),
            patch(
                "ferret.apps.session.controllers.SessionRepository", return_value=Mock()
            ),
            patch.object(window_module.SessionController, "refresh"),
            patch.object(window_module.SystemTray, "show"),
            patch.object(window_module.QTimer, "singleShot") as schedule,
        ):
            runtime = ApplicationRuntime()
            window = window_module.MainWindow(runtime)
        self.addCleanup(self._dispose, window, runtime)
        return window, runtime, schedule

    def test_startup_update_and_repeated_navigation_keep_settings_lazy(self) -> None:
        window, runtime, schedule = self.create_window()
        self.assertIsInstance(window.settings_interface, LazyPage)
        self.assertEqual(window.settings_interface.objectName(), "settingInterface")
        self.assertIsNone(window.settings_interface._page)
        self.assertEqual(window.findChildren(SettingsInterface), [])
        self.assertFalse(runtime.mitm_runtime.is_running)

        startup_checks = [
            call.args[1] for call in schedule.call_args_list if call.args[0] == 5000
        ]
        self.assertEqual(startup_checks, [window.updates.check_auto])
        original = CONFIG.get(CONFIG.auto_check_update)
        self.addCleanup(CONFIG.set, CONFIG.auto_check_update, original, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)

        with (
            patch.object(update_core, "update_supported", return_value=True),
            patch.object(update_core, "check", return_value=None) as check,
        ):
            startup_checks[0]()
            self.assertTrue(wait_until(lambda: not window.updates.busy))
            check.assert_called_once_with()
            self.assertIsNone(window.settings_interface._page)

            window.switchTo(window.settings_interface)
            self.assertTrue(
                wait_until(lambda: window.settings_interface._page is not None)
            )
            page = window.settings_interface.ensure()
            self.assertIsInstance(page, SettingsInterface)
            self.assertIs(page.updates, window.updates)
            self.assertFalse(window.search_host.isVisible())

            window.switchTo(window.captures_interface)
            window.switchTo(window.settings_interface)
            self.assertIs(window.settings_interface.ensure(), page)
            self.assertEqual(window.findChildren(SettingsInterface), [page])
            check.assert_called_once_with()

    def test_sessions_are_created_and_refreshed_only_when_first_opened(self) -> None:
        window, _runtime, _schedule = self.create_window()
        self.assertIsInstance(window.sessions_interface, LazyPage)
        self.assertFalse(window.sessions_interface.is_created)
        self.assertEqual(window.findChildren(SessionsInterface), [])

        with patch.object(window.session_controller, "refresh") as refresh:
            window.captures_interface.controller.capture_state_changed.emit(
                CaptureState.STOPPED
            )
            refresh.assert_not_called()
            self.assertFalse(window.sessions_interface.is_created)

            window.switchTo(window.sessions_interface)
            self.assertTrue(wait_until(lambda: window.sessions_interface.is_created))
            refresh.assert_called_once_with()
            page = window.sessions_interface.ensure()
            assert isinstance(page, SessionsInterface)
            self.assertIsNone(page.viewer_page.splitter)
            window.search_host.search_requested.emit("saved")
            self.assertEqual(page.current_search_text(), "saved")

            window.switchTo(window.captures_interface)
            window.captures_interface.controller.capture_state_changed.emit(
                CaptureState.STOPPED
            )
            self.assertEqual(refresh.call_count, 2)
            window.switchTo(window.sessions_interface)
            self.assertIs(window.sessions_interface.ensure(), page)
            self.assertEqual(window.findChildren(SessionsInterface), [page])
            self.assertEqual(page.current_search_text(), "saved")

    def _dispose(self, window, runtime) -> None:
        window.tray_icon.hide()
        window.deleteLater()
        runtime.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
