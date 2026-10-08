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
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import RoundMenu

from ferret.apps.common.edit.widgets import ItemTableToolWidget
from ferret.apps.common.flow import views
from ferret.apps.common.flow.column_settings import ColumnSettingsDialog
from ferret.apps.common.flow.columns import default_layout
from ferret.apps.session.controllers import SessionController
from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.views import SessionListPage
from ferret.apps.update import coordinator as update_coordinator
from ferret.core import update as update_core
from ferret.core.update import UpdateBrief, UpdateError
from tests.core.mitm._qt import wait_until


class TemporaryWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch.object(views, "save_layout"))
        self.addCleanup(self.app.processEvents)
        with patch.object(views, "load_layout", return_value=default_layout()):
            self.pane = views.FlowViewerPane()
        self.addCleanup(self.pane.deleteLater)

    def test_header_menu_reuses_one_instance(self):
        table = self.pane.table
        persistent = set(table.findChildren(RoundMenu))
        header_menu = None
        for _ in range(5):
            table._on_header_menu(QPoint(3, 3))
            menus = set(table.findChildren(RoundMenu)) - persistent
            self.assertEqual(len(menus), 1)
            menu = menus.pop()
            if header_menu is None:
                header_menu = menu
            self.assertIs(menu, header_menu)
            menu.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.assertTrue(shiboken6.isValid(menu))
        self.assertEqual(set(table.findChildren(RoundMenu)), persistent | {header_menu})

    def test_rejected_column_dialog_is_deleted_after_close(self):
        dialogs = []
        closed = []

        def create(*args):
            dialog = ColumnSettingsDialog(*args)
            dialogs.append(dialog)
            dialog.finished.connect(lambda: closed.append(dialog))
            QTimer.singleShot(0, dialog.reject)
            return dialog

        with patch.object(views, "ColumnSettingsDialog", side_effect=create):
            for _ in range(3):
                self.pane.table._open_column_dialog()
                dialog = dialogs[-1]
                self.assertTrue(
                    wait_until(lambda dialog=dialog: dialog in closed, timeout_ms=2000)
                )
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
        window = QWidget()
        self.addCleanup(window.deleteLater)
        updates = update_coordinator.UpdateCoordinator(window)
        brief = UpdateBrief("1.0.0", "1.0.1", 128, "", "https://example.test/releases")
        dialog = update_coordinator.UpdateDialog(brief, window)
        with (
            patch.object(update_coordinator, "UpdateDialog", return_value=dialog),
            patch.object(dialog, "exec", side_effect=RuntimeError("dialog failed")),
            self.assertRaisesRegex(RuntimeError, "dialog failed"),
        ):
            updates._on_update_available(object(), brief)
        self.assertIsNone(updates._update_dialog)
        updates.controller.download_progress.emit(50)
        updates.controller.download_finished.emit(object())
        self.assertEqual(dialog.progress_bar.value(), 0)
        self.assertFalse(dialog.ready)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(dialog))

    def test_apply_failure_remains_visible_after_the_ready_dialog_closes(self):
        window = QWidget()
        self.addCleanup(window.deleteLater)
        updates = update_coordinator.UpdateCoordinator(window)
        brief = UpdateBrief("1.0.0", "1.0.1", 128, "", "https://example.test/releases")
        dialog = update_coordinator.UpdateDialog(brief, window)
        updates.restart_requested.connect(updates.controller.apply_and_restart)
        QTimer.singleShot(0, lambda: (dialog.set_ready(), dialog.accept()))
        with (
            patch.object(update_coordinator, "UpdateDialog", return_value=dialog),
            patch.object(
                update_core,
                "apply_and_restart",
                side_effect=UpdateError("SDK rejected"),
            ),
            patch.object(update_coordinator, "show_warning") as warning,
            self.assertLogs("ferret.settings", "WARNING") as logs,
        ):
            updates._on_update_available(object(), brief)
        self.assertEqual(
            logs.output, ["WARNING:ferret.settings:应用更新失败：SDK rejected"]
        )
        warning.assert_called_once_with("应用更新失败", "SDK rejected", window)
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(updates._update_dialog)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(shiboken6.isValid(dialog))
