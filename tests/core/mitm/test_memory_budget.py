from __future__ import annotations

import gc
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    FlowFile,
    HTTPFlow,
    MitmRuntime,
    MitmRuntimeState,
    Opcode,
    WebSocketMessage,
)
from ferret.core.mitm.rows import flow_row
from ferret.core.mitm.sse import FerretSseAddon
from ferret.core.mitm.view import FerretView
from ferret.core.mitm.wsframe import WS_PREVIEW_BYTES, WsFrame, ws_frames
from tests.core.mitm._qt import wait_until


class WebSocketRetentionTests(unittest.TestCase):
    """内核在原生 messages 列表上按预览窗口裁剪（不落盘、无并行存储）。"""

    def setUp(self):
        self.view = FerretView()
        self.addCleanup(self.view.clear)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def _feed(self, flow: HTTPFlow, count: int, size: int = 1) -> None:
        assert flow.websocket is not None
        for index in range(count):
            flow.websocket.messages.append(
                WebSocketMessage(Opcode.TEXT, True, bytes([index]) * size)
            )
            self.view.websocket_message(flow)

    def test_history_is_trimmed_inside_the_native_list(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        with patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 4):
            self._feed(flow, 20)
        messages = flow.websocket.messages
        self.assertEqual(len(messages), 4)
        self.assertEqual(
            [message.content for message in messages],
            [bytes([index]) for index in range(16, 20)],
        )

    def test_byte_budget_keeps_at_least_the_newest_message(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        with patch("ferret.core.mitm.view.WS_WINDOW_BYTES", 10):
            self._feed(flow, 8, size=6)
        messages = flow.websocket.messages
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[-1].content, b"\x07" * 6)

    def test_export_contains_the_retained_window(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        with patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 4):
            self._feed(flow, 20)
        flow.live = False
        destination = Path(self.directory.name) / "retained.flow"
        FlowFile.write(destination, [flow])
        restored = FlowFile.read(destination)[0]
        assert isinstance(restored, HTTPFlow) and restored.websocket is not None
        self.assertEqual(
            [message.content for message in restored.websocket.messages],
            [bytes([index]) for index in range(16, 20)],
        )

    def test_view_accounts_trimmed_history(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        flow.live = False
        self.view.add([flow])
        with patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 4):
            self._feed(flow, 20)
        self.assertEqual(len(flow.websocket.messages), 4)
        self.assertEqual(self.view._stored_bytes, self.view._size(flow))

    def test_preview_window_matches_trimmed_history(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        with patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 4):
            self._feed(flow, 20, size=WS_PREVIEW_BYTES + 1)
        frames = ws_frames(flow.websocket, limit=2000)
        self.assertEqual(len(frames), 4)
        self.assertEqual(frames[-1].index, 3)
        self.assertTrue(frames[-1].truncated)
        self.assertEqual(frames[-1].size, WS_PREVIEW_BYTES + 1)
        self.assertLessEqual(
            sum(len(frame.content) for frame in frames), 2 * 1024 * 1024
        )

    def test_retained_message_edits_stay_visible(self):
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        with patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 2):
            self._feed(flow, 4)
        messages = flow.websocket.messages
        messages[0].content = b"hook edited"
        self.assertEqual(messages[0].content, b"hook edited")
        self.assertEqual(messages[0].dropped, False)
        messages[0].drop()
        self.assertTrue(messages[0].dropped)


class HistoryBudgetTests(unittest.TestCase):
    def test_static_sse_archive_is_accounted_after_view_response(self):
        view = FerretView()
        sse = FerretSseAddon()
        sse.on_flow_updated = lambda flow: view.update([flow])
        view.additional_size = sse.memory_size
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.live = False
        flow.request.content = b""
        flow.response.headers["content-type"] = "text/event-stream"
        flow.response.content = b"data: a\n\n"
        sse.responseheaders(flow)
        view.response(flow)
        size_before = view._stored_bytes
        sse.response(flow)
        self.assertGreater(sse.memory_size(flow), 0)
        self.assertEqual(view._stored_bytes, size_before + sse.memory_size(flow))
        view.clear()

    def test_byte_budget_evicts_completed_history_but_not_breakpoints(self):
        view = FerretView()
        old, held, recent = (tflow.tflow(resp=True) for _ in range(3))
        for flow in (old, held, recent):
            assert flow.response is not None
            flow.live = False
            flow.request.content = b""
            flow.response.content = b"x" * 10
        held.intercept()
        with patch("ferret.core.mitm.view.FLOW_HISTORY_BYTES", 20):
            view.add([old, held, recent])
        self.assertNotIn(old.id, view._store)
        self.assertIn(held.id, view._store)
        self.assertIn(recent.id, view._store)
        self.assertTrue(held.intercepted)
        self.assertEqual(view._stored_bytes, 20)
        view.clear()
        self.assertEqual(view._stored_bytes, 0)


class UiMailboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.runtime = MitmRuntime()
        self.addCleanup(self.runtime._ui_events.close)

    def post_in_worker(self, callback):
        errors = []

        def run():
            try:
                callback()
            except BaseException as exc:  # noqa: BLE001 -- propagate worker failures to unittest
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

    def test_message_burst_queues_only_one_body_free_notification(self):
        seen = []
        self.runtime.messages_changed.connect(lambda *args: seen.append(args))

        def burst():
            for index in range(3000):
                self.runtime.post_ui_event(
                    "websocket_frame",
                    "flow",
                    WsFrame(index, True, 1, b"x" * 4096, 1.0, False, False),
                )

        self.post_in_worker(burst)
        gc.collect()
        self.assertEqual(len(self.runtime._ui_events._events), 1)
        (_, name, args), = self.runtime._ui_events._events.values()
        self.assertEqual(name, "messages_changed")
        self.assertEqual(args, ("flow", "websocket", 3000))
        self.assertEqual(seen, [])
        self.app.processEvents()
        self.assertEqual(seen, [("flow", "websocket", 3000)])

    def test_updates_coalesce_without_reordering_remove_and_readd(self):
        row = flow_row(tflow.tflow(resp=True))
        names = []
        for name in ("flow_added", "flow_updated", "flow_removed"):
            getattr(self.runtime, name).connect(
                lambda *args, name=name: names.append(name)
            )

        def burst():
            self.runtime.post_ui_event("flow_added", row)
            self.runtime.post_ui_event("flow_updated", row)
            self.runtime.post_ui_event("flow_removed", row, 0)
            self.runtime.post_ui_event("flow_added", row)
            self.runtime.post_ui_event("flow_updated", row)

        self.post_in_worker(burst)
        self.app.processEvents()
        self.assertEqual(
            names, ["flow_added", "flow_removed", "flow_added", "flow_updated"]
        )

    def test_failure_cannot_overtake_previous_batches(self):
        seen = []
        self.runtime.flow_stored.connect(lambda row: seen.append("stored"))
        self.runtime.flow_added.connect(lambda row: seen.append("added"))
        self.runtime.failed.connect(lambda error: seen.append("failed"))

        def burst():
            for _ in range(300):
                row = flow_row(tflow.tflow(resp=True))
                self.runtime.post_ui_event("flow_stored", row)
                self.runtime.post_ui_event("flow_added", row)
            self.runtime.post_ui_event(
                "_master_start_failed", self.runtime._generation, "failure"
            )

        self.post_in_worker(burst)
        self.assertTrue(wait_until(lambda: "failed" in seen))
        self.assertEqual(seen, ["stored", "added"] * 300 + ["failed"])

    def test_reentrant_flush_consumes_current_batch_before_gate_closes(self):
        seen = []
        gate = [True]

        def receive(row):
            seen.append(gate[0])
            if len(seen) == 1:
                self.runtime.flush_ui_events()
                gate[0] = False

        self.runtime.flow_stored.connect(receive)
        row = flow_row(tflow.tflow(resp=True))
        self.post_in_worker(
            lambda: [self.runtime.post_ui_event("flow_stored", row) for _ in range(600)]
        )
        self.app.processEvents()
        self.assertEqual(seen, [True] * 600)
        self.assertFalse(gate[0])

    def test_flush_stops_at_current_prefix_when_handler_posts_more(self):
        seen = []
        row = flow_row(tflow.tflow(resp=True))

        def receive(value):
            seen.append(value)
            if len(seen) == 1:
                self.runtime.post_ui_event("flow_stored", row)

        self.runtime.flow_stored.connect(receive)
        self.post_in_worker(lambda: self.runtime.post_ui_event("flow_stored", row))
        self.runtime.flush_ui_events()
        self.assertEqual(len(seen), 1)
        self.app.processEvents()
        self.assertEqual(len(seen), 2)

    def test_stale_wakeup_cannot_deliver_readiness_before_master_creation(self):
        self.post_in_worker(lambda: self.runtime.post_ui_event("view_refreshed"))
        self.runtime.flush_ui_events()
        self.runtime._ui_events.close()
        master = Mock()
        self.runtime._thread = Mock(master=master)
        self.runtime._generation = 1
        self.runtime._state = MitmRuntimeState.STARTING
        self.runtime._master_running.disconnect()
        seen = []
        self.runtime._master_running.connect(
            lambda generation: seen.append(self.runtime._master)
        )

        def startup():
            self.runtime.post_ui_event("_master_created", 1)
            self.runtime.post_ui_event("_master_running", 1)

        self.post_in_worker(startup)
        self.app.processEvents()
        self.assertEqual(seen, [master])
        self.runtime._thread = None


if __name__ == "__main__":
    unittest.main()
