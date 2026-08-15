"""Integration tests for native client playback through MitmFacade."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.clientplayback import ClientPlayback
from mitmproxy.exceptions import CommandError
from mitmproxy.test import taddons, tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.capture.controllers import CaptureController
from ferret.core.mitm import FlowFile, MitmFacade, MitmRuntimeState, View


class FakeMaster:
    def __init__(self) -> None:
        self.client_playback = ClientPlayback()
        self.options = MagicMock()


class FakeRuntime(QObject):
    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.view = View()
        self.master = FakeMaster()
        self.state = MitmRuntimeState.RUNNING
        self.is_running = True
        self.listen_host = "127.0.0.1"
        self.listen_port = 8080

    def call(self, callback, *, timeout=5.0):
        return callback()


class FakeSystemProxy:
    def attach(self, host, port):
        return None

    def detach(self):
        return True


def make_http_flow():
    flow = tflow.tflow(resp=True)
    flow.live = False
    flow.intercepted = False
    return flow


class CaptureControllerReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.facade = MitmFacade(self.runtime)
        self.controller = CaptureController(
            mitm=self.facade, system_proxy=FakeSystemProxy()
        )
        self.flow = make_http_flow()
        self.runtime.view.add([self.flow])

    def test_replay_works_when_system_proxy_is_stopped(self) -> None:
        self.assertFalse(self.controller.is_capturing)
        with patch.object(
            self.runtime.master.client_playback, "start_replay"
        ) as start_replay:
            self.controller.replay_flow(self.flow.id)

        replay = start_replay.call_args[0][0][0]
        self.assertIsNot(replay, self.flow)
        self.assertIsNone(replay.response)
        self.assertIsNone(replay.error)
        self.assertEqual(replay.is_replay, "request")
        self.assertIsNotNone(self.flow.response)

    def test_replay_flows_preserves_order_and_skips_invalid_flows(self) -> None:
        flows = [make_http_flow() for _ in range(3)]
        for index, flow in enumerate(flows):
            flow.request.url = f"https://example.com/{index}"
        invalid = make_http_flow()
        invalid.live = True

        with patch.object(
            self.runtime.master.client_playback, "start_replay"
        ) as start_replay:
            self.controller.replay_flows([*flows, invalid])

        replay_flows = start_replay.call_args[0][0]
        self.assertEqual(
            [flow.request.url for flow in replay_flows],
            [f"https://example.com/{index}" for index in range(3)],
        )

    def test_replay_file_delegates_native_errors(self) -> None:
        with self.assertRaises(CommandError):
            self.controller.load_replay_file(Path("nonexistent.flow"))

    def test_replay_file_succeeds_with_valid_flow_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".flow", delete=False) as file:
            path = Path(file.name)
        try:
            FlowFile.write(path, [make_http_flow()])
            with taddons.context(self.runtime.master.client_playback):
                self.controller.load_replay_file(path)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
