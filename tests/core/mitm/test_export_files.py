"""File exports preserve existing data on failure and native HAR timing semantics."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow

from ferret.core.mitm import FlowExporter, FlowFile, MitmFacade, MitmRuntime, View
from ferret.core.mitm.bindings import SaveHar, io


class AtomicExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "existing.flow"
        self.path.write_bytes(b"previous-export")
        self.flows = [tflow.tflow(resp=True), tflow.tflow(resp=True)]

    def assert_preserved(self):
        self.assertEqual(self.path.read_bytes(), b"previous-export")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_flow_serialization_failure_preserves_destination(self):
        with (
            patch.object(io.FlowWriter, "add", side_effect=OSError("disk full")),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            FlowFile.write(self.path, self.flows)
        self.assert_preserved()

    def test_har_partial_write_preserves_destination(self):
        def partial_write(_har, file, **_kwargs):
            file.write('{"log":')
            raise OSError("disk full")

        with (
            patch("ferret.core.mitm.export.json.dump", side_effect=partial_write),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            FlowExporter.save_har(self.flows, str(self.path))
        self.assert_preserved()

    def test_failed_replace_preserves_destination_and_removes_temporary(self):
        with (
            patch("ferret.core.mitm.io.os.replace", side_effect=OSError("locked")),
            self.assertRaisesRegex(OSError, "locked"),
        ):
            FlowFile.write(self.path, self.flows)
        self.assert_preserved()

    def test_success_replaces_complete_flow_file(self):
        self.assertEqual(FlowFile.write(self.path, iter(self.flows)), 2)
        self.assertEqual(
            [flow.id for flow in FlowFile.read(self.path)],
            [flow.id for flow in self.flows],
        )
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_har_iterator_retains_native_shared_connection_timings(self):
        self.flows[1].server_conn = self.flows[0].server_conn
        expected = SaveHar().make_har(self.flows)
        with patch("ferret.core.mitm.export.json.dumps", side_effect=AssertionError):
            FlowExporter.save_har(iter(self.flows), str(self.path))
        self.assertEqual(json.loads(self.path.read_text("utf-8")), expected)
        self.assertEqual(list(self.root.iterdir()), [self.path])


class FacadeExportBatchTests(unittest.TestCase):
    def test_exports_snapshot_bounded_batches_and_keep_ids_and_order(self):
        flows = [tflow.tflow(resp=True) for _ in range(130)]
        for flow in flows:
            flow.server_conn = flows[0].server_conn
        view = View()
        view.add(flows)
        batch_sizes = []

        def call(callback):
            snapshots = callback()
            batch_sizes.append(len(snapshots))
            for snapshot in snapshots:
                self.assertIsNot(snapshot, view.get_by_id(snapshot.id))
            return snapshots

        runtime = SimpleNamespace(
            view=view, is_running=True, call=Mock(side_effect=call)
        )
        facade = object.__new__(MitmFacade)
        facade.runtime = cast(MitmRuntime, runtime)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.flow"
            self.assertEqual(facade.save_flows([f.id for f in flows], path), len(flows))
            self.assertEqual([f.id for f in FlowFile.read(path)], [f.id for f in flows])
            self.assertEqual(batch_sizes, [64, 64, 2])
            batch_sizes.clear()
            facade.export_har([f.id for f in flows], str(path))
            self.assertEqual(batch_sizes, [64, 64, 2])
            self.assertEqual(
                json.loads(path.read_text("utf-8")), SaveHar().make_har(flows)
            )
