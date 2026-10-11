from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

from ferret.apps import window as window_module
from ferret.apps.capture.controllers import CaptureState
from ferret.apps.common.lazy_page import LazyPage
from ferret.apps.session.views import SessionsInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.apps.update.dialogs import UpdateDialog
from ferret.core import update as update_core
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import CONFIG
from tests.core.mitm._qt import wait_until


class _MainWindowHarness(unittest.TestCase):
    """构造真实 MainWindow 的公共夹具（应用、临时配置目录、清理）。无测试方法。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dispose(self, window, runtime) -> None:
        window.tray_icon.hide()
        window.deleteLater()
        runtime.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

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
            patch.object(window_module.QTimer, "singleShot"),
            patch.object(
                window_module.UpdateCoordinator, "start_auto_checking"
            ) as start_updates,
        ):
            runtime = ApplicationRuntime()
            window = window_module.MainWindow(runtime)
        self.addCleanup(self._dispose, window, runtime)
        return window, runtime, start_updates


class StartupLazyLoadingTests(_MainWindowHarness):
    def test_startup_update_and_repeated_navigation_keep_settings_lazy(self) -> None:
        window, runtime, start_updates = self.create_window()
        self.assertIsInstance(window.settings_interface, LazyPage)
        self.assertEqual(window.settings_interface.objectName(), "settingInterface")
        self.assertIsNone(window.settings_interface._page)
        self.assertEqual(window.findChildren(SettingsInterface), [])
        self.assertFalse(runtime.mitm_runtime.is_running)

        start_updates.assert_called_once_with()
        original = CONFIG.get(CONFIG.auto_check_update)
        self.addCleanup(CONFIG.set, CONFIG.auto_check_update, original, False)
        CONFIG.set(CONFIG.auto_check_update, True, False)

        with (
            patch.object(update_core, "update_supported", return_value=True),
            patch.object(update_core, "check", return_value=None) as check,
        ):
            window.updates.check_auto()
            self.assertTrue(wait_until(lambda: not window.updates.busy))
            check.assert_called_once_with()
            self.assertIsNone(window.settings_interface._page)

            window.switchTo(window.settings_interface)
            self.assertTrue(
                wait_until(lambda: window.settings_interface._page is not None)
            )
            page = window.settings_interface.ensure()
            self.assertIsInstance(page, SettingsInterface)
            self.assertFalse(hasattr(page, "updates"))
            self.assertFalse(window.search_host.isVisible())

            window.switchTo(window.captures_interface)
            window.switchTo(window.settings_interface)
            self.assertIs(window.settings_interface.ensure(), page)
            self.assertEqual(window.findChildren(SettingsInterface), [page])
            check.assert_called_once_with()

    def test_update_menu_opens_a_single_dialog_without_changing_page(self) -> None:
        window, _runtime, _start_updates = self.create_window()
        page = window.stackedWidget.currentWidget()
        self.assertFalse(window.update_item.isSelectable)
        self.assertEqual(window.findChildren(UpdateDialog), [])

        window.update_item.clicked.emit(True)
        first_dialogs = window.findChildren(UpdateDialog)
        self.assertEqual(len(first_dialogs), 1)
        self.assertFalse(first_dialogs[0].isHidden())
        window.update_item.clicked.emit(True)
        self.assertEqual(window.findChildren(UpdateDialog), first_dialogs)
        self.assertIs(window.stackedWidget.currentWidget(), page)
        self.assertIsNone(window.settings_interface._page)

    def test_background_update_only_changes_the_navigation_badge(self) -> None:
        window, _runtime, _start_updates = self.create_window()
        window.show()
        self.app.processEvents()
        self.assertTrue(window.update_badge.isHidden())
        window.resize(1080, 780)
        self.app.processEvents()
        self.assertTrue(window.update_badge.isHidden())
        brief = update_core.UpdateBrief(
            "1.0.0", "1.0.1", 128, "", "https://example.test/releases"
        )
        window.updates.controller.update_available.emit(object(), brief)
        self.assertFalse(window.update_badge.isHidden())
        self.assertTrue(window.update_item.rect().contains(window.update_badge.pos()))
        self.assertEqual(window.findChildren(UpdateDialog), [])

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


class PinButtonTests(_MainWindowHarness):
    """置顶按钮：快捷键与点击必须走同一入口，图标跟随窗口真实状态（issues #13）。"""

    def test_shortcut_and_click_toggle_the_real_window_state(self) -> None:
        window, _runtime, _schedule = self.create_window()
        button = window.pin_button
        flag = Qt.WindowType.WindowStaysOnTopHint

        self.assertFalse(bool(window.windowFlags() & flag))
        self.assertFalse(button._is_pinned)

        # 快捷键：真正置顶，不再是「只翻图标」。图标与真实窗口状态同步。
        button._shortcut_obj.activated.emit()
        self.assertTrue(bool(window.windowFlags() & flag))
        self.assertTrue(button._is_pinned)

        # 随后点击取消置顶：图标与窗口状态保持一致，不反向。
        button.click()
        self.assertFalse(bool(window.windowFlags() & flag))
        self.assertFalse(button._is_pinned)


if __name__ == "__main__":
    unittest.main()
