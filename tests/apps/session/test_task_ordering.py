"""Session task races use controlled workers without starting a proxy."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.session.controllers import SessionController
from ferret.apps.session.services import SessionRepository
from tests.core.mitm._qt import wait_until

app = QApplication.instance() or QApplication([])


class SessionTaskOrderingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = SessionRepository(Path(self.temporary.name))
        self.meta = self.repo.create("session", [tflow.tflow(resp=True)])
        self.controller = SessionController(repository=self.repo)
        self.release = Event()
        self.started = Event()
        self.loaded = []
        self.controller.sessions_loaded.connect(self.loaded.append)

    def tearDown(self):
        self.release.set()
        self.assertTrue(wait_until(lambda: not self.controller._tasks, timeout_ms=5000))

    def test_repeated_refreshes_coalesce_and_discard_the_old_scan(self):
        calls = []

        def list_all():
            calls.append(len(calls))
            if len(calls) == 1:
                self.started.set()
                self.release.wait(5)
                return ["old"]
            return ["latest"]

        with patch.object(self.repo, "list_all", side_effect=list_all):
            self.controller.refresh()
            self.assertTrue(wait_until(self.started.is_set, timeout_ms=5000))
            self.controller.refresh()
            self.controller.refresh()
            self.assertEqual(len(calls), 1)
            self.release.set()
            self.assertTrue(
                wait_until(lambda: not self.controller._tasks, timeout_ms=5000)
            )
        self.assertEqual(self.loaded, [["latest"]])
        self.assertEqual(len(calls), 2)

    def test_write_invalidates_old_scan_and_never_overlaps_it(self):
        writing = Event()
        calls = []

        def list_all():
            calls.append(len(calls))
            if len(calls) == 1:
                self.started.set()
                self.release.wait(5)
                return ["old"]
            return ["new"]

        def rename(*_args):
            writing.set()
            return self.meta

        with (
            patch.object(self.repo, "list_all", side_effect=list_all),
            patch.object(self.repo, "rename", side_effect=rename),
        ):
            self.controller.refresh()
            self.assertTrue(wait_until(self.started.is_set, timeout_ms=5000))
            self.controller.rename_session(self.meta.session_id, "renamed")
            self.assertFalse(writing.wait(0.05))
            self.release.set()
            self.assertTrue(
                wait_until(lambda: not self.controller._tasks, timeout_ms=5000)
            )
        self.assertTrue(writing.is_set())
        self.assertEqual(self.loaded, [["new"]])

    def test_open_runs_one_reader_and_only_the_latest_pending_target(self):
        calls = []
        opened = []
        failed = []
        busy = []
        self.controller.session_opened.connect(lambda meta, _vc: opened.append(meta))
        self.controller.operation_failed.connect(lambda *_args: failed.append(True))
        self.controller.busy_changed.connect(busy.append)

        def open_session(session_id):
            calls.append(session_id)
            if session_id == "first":
                self.started.set()
                self.release.wait(5)
                raise OSError("superseded read failed")
            return self.meta, [tflow.tflow(resp=True)]

        with patch.object(self.repo, "open", side_effect=open_session):
            self.controller.open_session("first")
            self.assertTrue(wait_until(self.started.is_set, timeout_ms=5000))
            self.controller.open_session("middle")
            self.controller.open_session("last")
            self.assertEqual(calls, ["first"])
            self.release.set()
            self.assertTrue(
                wait_until(lambda: not self.controller._tasks, timeout_ms=5000)
            )
        self.assertEqual(calls, ["first", "last"])
        self.assertEqual(opened, [self.meta])
        self.assertEqual(failed, [])
        self.assertEqual(busy, [True, False])
