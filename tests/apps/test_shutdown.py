from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.apps.window import MainWindow
from ferret.core.settings import CONFIG


class ShutdownTests(unittest.TestCase):
    def window(self, *, success=True):
        events = []
        window = SimpleNamespace(
            _shutdown_complete=False,
            captures_interface=SimpleNamespace(
                stop_capture=lambda: events.append("capture")
            ),
            runtime=SimpleNamespace(
                shutdown=lambda: events.append("runtime") or success,
                last_shutdown_error="恢复代理失败",
            ),
            intercept_window=SimpleNamespace(hide=lambda: events.append("hide")),
            settings_interface=SimpleNamespace(
                update_controller=SimpleNamespace(
                    apply_and_restart=lambda _: events.append("apply")
                )
            ),
            tr=lambda text: text,
            show=Mock(),
            raise_=Mock(),
        )
        window.shutdown = lambda: MainWindow.shutdown(cast(MainWindow, window))
        return window, events

    def test_update_flushes_and_shuts_down_before_sdk_exit(self):
        window, events = self.window()
        with patch.object(
            CONFIG, "flush_pending_save", side_effect=lambda: events.append("save")
        ):
            MainWindow._apply_update(window, object())
        self.assertEqual(events, ["save", "capture", "runtime", "hide", "apply"])

    def test_failed_cleanup_stays_open_and_never_launches_updater(self):
        window, events = self.window(success=False)
        with (
            patch.object(CONFIG, "flush_pending_save"),
            patch("ferret.apps.window.show_warning") as warning,
        ):
            MainWindow._apply_update(window, object())
        self.assertNotIn("apply", events)
        self.assertFalse(window._shutdown_complete)
        warning.assert_called_once_with("退出未完成", "恢复代理失败", window)
        window.show.assert_called_once()

    def test_failed_settings_flush_leaves_runtime_running(self):
        window, events = self.window()
        with (
            patch.object(
                CONFIG, "flush_pending_save", side_effect=OSError("disk full")
            ),
            patch("ferret.apps.window.show_warning"),
        ):
            self.assertFalse(MainWindow.shutdown(window))
        self.assertEqual(events, [])

    def test_startup_recovery_failure_is_shown_on_the_next_ui_turn(self):
        window, _events = self.window()
        with (
            patch("ferret.apps.window.QTimer.singleShot") as schedule,
            patch("ferret.apps.window.show_warning") as warning,
        ):
            MainWindow._show_startup_error(window, "恢复失败")
            warning.assert_not_called()
            schedule.call_args.args[1]()
        warning.assert_called_once_with("系统代理恢复失败", "恢复失败", window)
