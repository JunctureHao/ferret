from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.capture.controllers import CaptureController
from ferret.apps.capture.views import _CaptureFlowSource
from ferret.apps.common.flow.models import FlowTableModel
from ferret.core.mitm import FlowFile, MitmFacade, MitmRuntime
from ferret.core.mitm.runtime import UiBridgeAddon

app = QApplication.instance() or QApplication([])


class CaptureAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = MitmRuntime()
        self.facade = MitmFacade(self.runtime)
        self.controller = CaptureController(mitm=self.facade, system_proxy=Mock())
        self.bridge = UiBridgeAddon(self.runtime.view, self.runtime, Mock(), 0)
        self.addCleanup(self.bridge.done)
        self.model = FlowTableModel(self.controller)
        self.model.set_source(_CaptureFlowSource(self.controller))
        self.controller.flow_added.connect(self.model.handle_add)
        self.controller.flow_removed.connect(self.model.handle_remove)
        self.controller.view_refreshed.connect(self.model.handle_refresh)
        self.addCleanup(self.controller.deleteLater)

    def add(self, host):
        flow = tflow.tflow(resp=True)
        flow.request.host = host
        self.runtime.view.add([flow])
        return flow

    def test_refresh_cannot_admit_traffic_from_stopped_capture(self):
        stopped = self.add("stopped.test")
        self.assertEqual(self.model.rowCount(), 0)
        self.controller.apply_filter("")
        self.assertEqual(self.model.rowCount(), 0)
        self.assertEqual(self.controller.total_count(), 0)
        self.assertIs(self.facade.get_flow(stopped.id), stopped)

    def test_filtered_capture_flows_keep_admission_when_capture_stops(self):
        self.controller._set_recording(True)
        self.controller.apply_filter("~d visible.test")
        shown = self.add("visible.test")
        hidden = self.add("hidden.test")
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.controller.total_count(), 2)
        self.controller._set_recording(False)
        stopped = self.add("stopped.test")
        self.controller.apply_filter("")
        self.assertEqual(
            {flow.id for flow in self.controller.visible_http_flows()},
            {shown.id, hidden.id},
        )
        self.assertEqual(self.model.rowCount(), 2)
        self.controller.clear_flows()
        self.assertEqual(self.controller.total_count(), 0)
        self.assertIs(self.facade.get_flow(stopped.id), stopped)

    def test_explicit_import_is_admitted_while_stopped(self):
        def load(path, *, imported_ids):
            flows = FlowFile.read(path)
            self.runtime.view.add(flows)
            imported_ids.update(flow.id for flow in flows)
            return len(flows)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "import.flow"
            FlowFile.write(path, [tflow.tflow(resp=True)])
            with patch.object(self.facade, "load_flow_file", side_effect=load):
                self.controller.load_flow_file(path)
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.controller.total_count(), 1)

    def test_concurrent_stopped_flow_is_not_admitted_by_import(self):
        def load(path, *, imported_ids):
            flows = FlowFile.read(path)
            self.runtime.view.add(flows)
            imported_ids.update(flow.id for flow in flows)
            self.add("during-import.test")
            return len(flows)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "import.flow"
            imported = tflow.tflow(resp=True)
            FlowFile.write(path, [imported])
            with patch.object(self.facade, "load_flow_file", side_effect=load):
                self.controller.load_flow_file(path)
        self.assertEqual(self.controller._admitted_ids, {imported.id})
        self.assertEqual(self.controller.total_count(), 1)

    def test_native_stop_keeps_already_admitted_history(self):
        self.controller._set_recording(True)
        captured = self.add("captured.test")
        self.controller._set_recording(False)
        self.controller._on_runtime_stopped()
        self.controller.apply_filter("")
        self.assertEqual(self.controller._admitted_ids, {captured.id})
        self.assertEqual(self.model.rowCount(), 1)
