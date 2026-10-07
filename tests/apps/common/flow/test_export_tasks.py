"""Menu exports run away from Qt and retain workers until their final signal."""

from __future__ import annotations

import gc
import os
import unittest
import weakref
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.flow.menus import FlowContextMenu, _FlowFileExport
from ferret.core.mitm import flow_row
from tests.core.mitm._qt import wait_until

app = QApplication.instance() or QApplication([])


class MenuExportTaskTests(unittest.TestCase):
    def test_menu_closure_does_not_drop_task_or_return_live_flows(self):
        for kind in ("har", "flow"):
            with self.subTest(kind=kind):
                self._check_export(kind)

    def test_failure_reports_on_gui_and_releases_worker(self):
        self._check_export("flow", fail=True)

    def test_closing_window_during_export_drops_only_the_notification(self):
        self._check_export("har", close_window=True)

    def _check_export(self, kind, *, fail=False, close_window=False):
        started = Event()
        release = Event()
        calls = []
        ui_thread = get_ident()

        def export(flow_ids, path):
            calls.append((get_ident(), flow_ids, path))
            started.set()
            release.wait(5)
            if fail:
                raise OSError("export failed")

        window = QWidget()
        controller = SimpleNamespace(export_har=export, save_flows=export)
        menu = FlowContextMenu(window, controller)
        row = flow_row(tflow.tflow(resp=True))
        menu.update_context(0, {"id": row.id}, [row])
        task_ref = None
        with (
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                return_value=(f"test.{kind}", ""),
            ),
            patch("ferret.apps.common.flow.menus.show_success") as success,
            patch("ferret.apps.common.flow.menus.show_error") as error,
            patch("ferret.apps.common.tasks.log.exception"),
        ):
            try:
                action = (
                    menu.export_menu.har_action
                    if kind == "har"
                    else menu.export_menu.save_flows_action
                )
                action.trigger()
                self.assertTrue(wait_until(started.is_set, timeout_ms=5000))
                jobs = app.findChildren(_FlowFileExport)
                self.assertEqual(len(jobs), 1)
                task_ref = weakref.ref(jobs[0]._task)
                self.assertNotEqual(calls[0][0], ui_thread)
                self.assertEqual(calls[0][1:], ([row.id], f"test.{kind}"))
                success.assert_not_called()
                menu.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertIsNotNone(task_ref())
                if close_window:
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                release.set()
                self.assertTrue(
                    wait_until(lambda: jobs[0]._task is None, timeout_ms=5000)
                )
                if close_window:
                    success.assert_not_called()
                    error.assert_not_called()
                elif fail:
                    success.assert_not_called()
                    error.assert_called_once()
                else:
                    success.assert_called_once()
                    error.assert_not_called()
            finally:
                release.set()
                wait_until(
                    lambda: all(
                        job._task is None for job in app.findChildren(_FlowFileExport)
                    ),
                    timeout_ms=5000,
                )
                if not close_window:
                    window.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        gc.collect()
        self.assertIsNotNone(task_ref)
        assert task_ref is not None
        self.assertIsNone(task_ref())
