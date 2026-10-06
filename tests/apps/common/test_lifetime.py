from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication
from qfluentwidgets import RoundMenu

from ferret.apps.common.edit.widgets import ItemTableToolWidget
from ferret.apps.common.flow import views
from ferret.apps.common.flow.column_settings import ColumnSettingsDialog
from ferret.apps.common.flow.columns import default_layout
from ferret.apps.session.controllers import SessionController
from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.views import SessionListPage
from ferret.apps.settings import views as settings_views
from ferret.core.update import UpdateBrief, UpdateError


class TemporaryWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.addCleanup(self.app.processEvents)
        with patch.object(views, "load_layout", return_value=default_layout()):
            self.pane = views.FlowViewerPane()
        self.addCleanup(self.pane.deleteLater)

    def test_closed_header_menus_do_not_remain_children(self):
        table = self.pane.table
        persistent = set(table.findChildren(RoundMenu))
        for _ in range(5):
            table._on_header_menu(QPoint(3, 3))
            menus = set(table.findChildren(RoundMenu)) - persistent
            for menu in menus:
                menu.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(set(table.findChildren(RoundMenu)), persistent)

    def test_rejected_column_dialog_is_deleted_after_exec_returns(self):
        dialogs = []

        def create(*args):
            dialog = ColumnSettingsDialog(*args)
            dialogs.append(dialog)
            QTimer.singleShot(0, dialog.reject)
            return dialog

        with patch.object(views, "ColumnSettingsDialog", side_effect=create):
            for _ in range(3):
                self.pane.table._open_column_dialog()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(all(not shiboken6.isValid(dialog) for dialog in dialogs))

    def test_editor_menu_actions_are_destroyed_when_menu_closes(self):
        editor = ItemTableToolWidget(editable=True)
        self.addCleanup(editor.deleteLater)
        persistent_actions = set(editor.findChildren(QAction))
        for _ in range(3):
            editor._show_context_menu(QPoint(3, 3))
            menus = editor.findChildren(RoundMenu)
            self.assertEqual(len(menus), 1)
            menus[0].close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(set(editor.findChildren(QAction)), persistent_actions)

    def test_session_menu_actions_are_destroyed_when_menu_closes(self):
        controller = SessionController(repository=Mock())
        self.addCleanup(controller.deleteLater)
        with patch.object(controller, "refresh"):
            page = SessionListPage(controller)
        self.addCleanup(page.deleteLater)
        now = datetime.now(UTC)
        page._on_sessions_loaded(
            [
                SessionMeta(
                    1,
                    "sid",
                    "capture",
                    Path("capture.flow"),
                    now,
                    now,
                    1,
                    128,
                    SessionSource.CAPTURE,
                )
            ]
        )
        page.table.selectRow(0)
        index = page.proxy_model.index(0, 0)
        pos = page.table.visualRect(index).center()
        persistent_actions = set(page.findChildren(QAction))
        for _ in range(3):
            page._on_context_menu(pos)
            menus = page.findChildren(RoundMenu)
            self.assertEqual(len(menus), 1)
            menus[0].close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertEqual(set(page.findChildren(QAction)), persistent_actions)

    def test_persistent_flow_menu_can_reopen_after_closing(self):
        menu = self.pane.table.context_menu
        for _ in range(3):
            menu.show()
            menu.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(shiboken6.isValid(menu))

    def test_update_dialog_exec_failure_clears_connections_and_reference(self):
        settings = settings_views.SettingsInterface()
        self.addCleanup(settings.deleteLater)
        brief = UpdateBrief("1.0.0", "1.0.1", 128, "", "https://example.test/releases")
        dialog = settings_views.UpdateDialog(brief, settings)
        with (
            patch.object(settings_views, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=RuntimeError("dialog failed")),
            self.assertRaisesRegex(RuntimeError, "dialog failed"),
        ):
            settings._SettingsInterface__on_update_available(object(), brief)  # ty: ignore[unresolved-attribute]
        self.assertIsNone(settings._update_dialog)
        settings.update_controller.download_progress.emit(50)
        settings.update_controller.download_finished.emit(object())
        self.assertEqual(dialog.progress_bar.value(), 0)
        self.assertFalse(dialog.ready)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(dialog))

    def test_apply_failure_remains_visible_after_the_ready_dialog_closes(self):
        settings = settings_views.SettingsInterface()
        self.addCleanup(settings.deleteLater)
        brief = UpdateBrief("1.0.0", "1.0.1", 128, "", "https://example.test/releases")
        dialog = settings_views.UpdateDialog(brief, settings)
        settings.update_restart_requested.connect(
            settings.update_controller.apply_and_restart
        )
        QTimer.singleShot(0, lambda: (dialog.set_ready(), dialog.accept()))
        with (
            patch.object(settings_views, "UpdateDialog", return_value=dialog),
            patch.object(
                settings_views.update_core,
                "apply_and_restart",
                side_effect=UpdateError("SDK rejected"),
            ),
            patch.object(settings_views, "show_warning") as warning,
        ):
            settings._SettingsInterface__on_update_available(object(), brief)  # ty: ignore[unresolved-attribute]
        warning.assert_called_once_with("应用更新失败", "SDK rejected", settings)
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(settings._update_dialog)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(dialog))
