from __future__ import annotations

import gc
import os
import tempfile
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow

from ferret.apps.session.services import SessionRepository
from ferret.core.mitm import FlowFile


class RepositoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = SessionRepository(self.root / "sessions")
        self.source = self.root / "input.flow"
        FlowFile.write(self.source, [tflow.tflow(resp=True), tflow.tflow(resp=True)])

    def test_copy_failure_never_publishes_a_partial_session(self):
        existing = self.repo.create("existing", [tflow.tflow(resp=True)])
        original = existing.path.read_bytes()

        def partial_copy(source, destination):
            Path(destination).write_bytes(Path(source).read_bytes()[:100])
            raise OSError("disk full")

        with (
            patch(
                "ferret.apps.session.services.shutil.copy2", side_effect=partial_copy
            ),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            self.repo.import_file(self.source)
        self.assertEqual([meta.name for meta in self.repo.list_all()], ["existing"])
        self.assertEqual(existing.path.read_bytes(), original)
        self.assertEqual(list(self.repo.root.glob("*.tmp")), [])

    def test_counts_are_cached_until_the_file_changes(self):
        meta = self.repo.import_file(self.source)
        with patch.object(FlowFile, "count_http", wraps=FlowFile.count_http) as count:
            self.assertEqual(self.repo.list_all()[0].flow_count, 2)
            self.assertEqual(self.repo.list_all()[0].flow_count, 2)
            count.assert_not_called()
            FlowFile.write(meta.path, [tflow.tflow(resp=True)])
            self.assertEqual(self.repo.list_all()[0].flow_count, 1)
            count.assert_called_once()

    def test_open_reads_flows_once_and_does_not_count_separately(self):
        meta = self.repo.import_file(self.source)
        with (
            patch.object(FlowFile, "count_http") as count,
            patch.object(
                FlowFile, "iter_valid_prefix", wraps=FlowFile.iter_valid_prefix
            ) as read,
        ):
            opened, flows = self.repo.open(meta.session_id)
        self.assertEqual(opened.flow_count, len(flows))
        self.assertEqual(len(flows), 2)
        read.assert_called_once()
        count.assert_not_called()

    def test_open_discards_non_http_and_replaced_bodies_while_reading(self):
        observed = []

        def records(_path):
            first = tflow.tflow(resp=True)
            first.id = "duplicate"
            first_ref = weakref.ref(first)
            yield first
            del first
            replacement = tflow.tflow(resp=True)
            replacement.id = "duplicate"
            replacement.request.path = "/latest"
            yield replacement
            gc.collect()
            observed.append(first_ref() is None)
            tcp = tflow.ttcpflow()
            tcp_ref = weakref.ref(tcp)
            yield tcp
            del tcp
            yield tflow.tflow(resp=True)
            observed.append(tcp_ref() is None)

        with patch.object(FlowFile, "iter_valid_prefix", side_effect=records):
            flows = self.repo._read_http(self.source)
        self.assertEqual(observed, [True, True])
        self.assertEqual(len(flows), 2)
        self.assertEqual(flows[0].request.path, "/latest")

    def test_open_keeps_complete_prefix_before_a_truncated_tail(self):
        contents = self.source.read_bytes()
        with self.source.open("ab") as file:
            file.write(contents[:30])
        meta = self.repo.import_file(self.source)
        self.assertEqual(len(self.repo.load_flows(meta.session_id)), 2)
