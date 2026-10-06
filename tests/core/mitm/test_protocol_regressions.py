"""Protocol regressions for fragmented, aborted and deliberately excluded flows."""

from __future__ import annotations

import asyncio
import gc
import gzip
import os
import socket
import sys
import tempfile
import threading
import unittest
import weakref
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.proxy.layers import http
from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    CaptureMaster,
    FlowFile,
    GatewayLayer,
    GatewayPolicy,
    GatewayRule,
    MitmFacade,
    MitmRuntime,
    Response,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
    RewriteRuleSet,
)
from ferret.core.mitm.addons import FerretRewriteAddon
from ferret.core.mitm.gateway import GatewayRuleSet
from ferret.core.mitm.scripts import load_script_module
from ferret.core.mitm.sse import (
    SSE_BODY_TRUNCATED_KEY,
    SSE_EVENTS_TRUNCATED_KEY,
    FerretSseAddon,
    _SseTap,
)
from ferret.core.mitm.view import FerretView

from ._qt import start_runtime, wait_until
from .test_upstream import free_port, http_through


class SseObservationTests(unittest.TestCase):
    def test_every_first_chunk_split_keeps_raw_deflate_events(self) -> None:
        body = b"data: HELLO\n\n"
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        wire = compressor.compress(body) + compressor.flush()
        for boundary in range(1, len(wire)):
            with self.subTest(boundary=boundary):
                events = []
                ended = []
                tap = _SseTap(
                    "utf-8",
                    "deflate",
                    events.extend,
                    lambda ended=ended: ended.append(True),
                )
                chunks = (wire[:boundary], wire[boundary:], b"")
                self.assertEqual(b"".join(tap.tee(chunk) for chunk in chunks), wire)
                self.assertEqual([event.data for event in events], ["HELLO"])
                self.assertEqual(ended, [True])

    def test_parser_failure_never_escapes_the_tee(self) -> None:
        ended = []
        tap = _SseTap(
            "utf-16", "identity", lambda events: None, lambda: ended.append(True)
        )
        for chunk in (b"data: no BOM\n\n", b"more", b"", b""):
            self.assertEqual(tap.tee(chunk), chunk)
        self.assertEqual(ended, [True])

    def test_unbounded_retry_does_not_drop_the_following_data(self) -> None:
        events = []
        tap = _SseTap("utf-8", "identity", events.extend, lambda: None)
        wire = b"retry: " + b"9" * 5000 + b"\ndata: still here\n\n"
        self.assertEqual(tap.tee(wire), wire)
        tap.tee(b"")
        self.assertEqual(events[0].data, "still here")
        self.assertIsNone(events[0].retry)

    def test_unterminated_block_buffer_is_bounded(self) -> None:
        with patch("ferret.core.mitm.sse.SSE_BLOCK_LIMIT", 16):
            tap = _SseTap("utf-8", "identity", lambda events: None, lambda: None)
            for _ in range(10):
                self.assertEqual(tap.tee(b"data: abcdefg"), b"data: abcdefg")
            self.assertFalse(tap._parsing)
            self.assertEqual(tap._feeder._pending, "")

    def test_static_response_and_error_done_have_one_terminal_event(self) -> None:
        ended = Mock()
        addon = FerretSseAddon()
        addon.bridge = SimpleNamespace(  # ty: ignore[invalid-assignment]
            sse_started=Mock(), sse_event=Mock(), sse_ended=ended
        )
        flow = tflow.tflow(resp=True)
        flow.response = Response.make(
            200, b"data: static\n\n", {"Content-Type": "text/event-stream"}
        )
        addon.responseheaders(flow)
        tap_ref = weakref.ref(addon._taps[flow.id])
        addon.response(flow)
        addon.error(flow)
        addon.done()
        self.assertEqual([event.data for event in addon.events(flow.id)], ["static"])
        ended.emit.assert_called_once_with(flow.id)
        self.assertEqual(flow.response.raw_content, b"data: static\n\n")
        self.assertFalse(callable(flow.response.stream))
        gc.collect()
        self.assertIsNone(tap_ref())

    def test_aborted_stream_flushes_once_and_persists_capacity_markers(self) -> None:
        addon = FerretSseAddon()
        flow = tflow.tflow(resp=True)
        flow.response = Response.make(200, b"", {"Content-Type": "text/event-stream"})
        flow.response.raw_content = None
        with (
            patch("ferret.core.mitm.sse.SSE_EVENT_LIMIT", 2),
            patch("ferret.core.mitm.sse.SSE_BODY_LIMIT", 9),
        ):
            addon.responseheaders(flow)
            tap = addon._taps[flow.id]
            for wire in (b"data: a\n\n", b"data: b\n\n", b"data: c"):
                tap.tee(wire)
            addon.error(flow)
            addon.done()
        self.assertEqual([event.data for event in addon.events(flow.id)], ["b", "c"])
        self.assertTrue(flow.metadata[SSE_BODY_TRUNCATED_KEY])
        self.assertTrue(flow.metadata[SSE_EVENTS_TRUNCATED_KEY])
        self.assertEqual(tap.buf, b"")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sse.flow"
            FlowFile.write(path, [flow])
            restored = FlowFile.read(path)[0]
            self.assertTrue(restored.metadata[SSE_BODY_TRUNCATED_KEY])
            self.assertTrue(restored.metadata[SSE_EVENTS_TRUNCATED_KEY])


class ScriptRegistrationTests(unittest.TestCase):
    def test_dataclass_module_registration_reload_and_failure_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.py"
            source = "from __future__ import annotations\nfrom dataclasses import dataclass\n@dataclass\nclass Item:\n    name: str = 'one'\n"
            path.write_text(source, encoding="utf-8")
            report = Mock()
            first = load_script_module(str(path), report)
            assert first is not None
            self.addCleanup(sys.modules.pop, first.__name__, None)
            self.assertIs(sys.modules[first.__name__], first)
            self.assertEqual(first.Item().name, "one")
            path.write_text(source.replace("one", "two"), encoding="utf-8")
            second = load_script_module(str(path), report)
            assert second is not None
            self.assertIsNot(first, second)
            self.assertEqual(second.Item().name, "two")
            self.assertIs(sys.modules[first.__name__], second)
            path.write_text("raise RuntimeError('broken')\n", encoding="utf-8")
            self.assertIsNone(load_script_module(str(path), report))
            self.assertNotIn(first.__name__, sys.modules)
            report.assert_called_once()


class RewriteRegressionTests(unittest.TestCase):
    def test_declared_non_utf8_charset_round_trips_through_compression(self) -> None:
        for charset, original, changed in (
            ("gbk", "价格", "现价"),
            ("iso-8859-1", "café", "thé"),
            ("utf-16", "价格", "现价"),
        ):
            with self.subTest(charset=charset):
                addon = FerretRewriteAddon()
                addon.set_rules(
                    RewriteRuleSet(
                        [
                            RewriteRule(
                                kind=RewriteKind.MODIFY_RESPONSE_BODY,
                                logic=RewriteLogic.CONTAINS,
                                value="example.com",
                                target=original,
                                replacement=changed,
                            )
                        ]
                    )
                )
                flow = tflow.tflow(resp=True)
                flow.request.url = "http://example.com/"
                flow.response = Response.make(
                    200,
                    b"",
                    {
                        "Content-Type": f"text/plain; charset={charset}",
                        "Content-Encoding": "gzip",
                    },
                )
                flow.response.content = original.encode(charset)
                addon.response(flow)
                self.assertEqual(
                    (flow.response.get_content(strict=False) or b"").decode(charset),
                    changed,
                )
                self.assertEqual(
                    gzip.decompress(flow.response.raw_content or b""),
                    changed.encode(charset),
                )

    def test_missing_map_local_candidate_allows_later_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            found = root / "found.txt"
            found.write_bytes(b"second rule")
            addon = FerretRewriteAddon()
            addon.set_rules(
                RewriteRuleSet(
                    [
                        RewriteRule(
                            kind=RewriteKind.MAP_LOCAL,
                            logic=RewriteLogic.CONTAINS,
                            value="example.com",
                            replacement=str(path),
                        )
                        for path in (empty, found)
                    ]
                )
            )
            flow = tflow.tflow()
            flow.request.url = "http://example.com/missing"
            addon.request(flow)
            assert flow.response is not None
            self.assertEqual(flow.response.status_code, 200)
            self.assertEqual(flow.response.get_content(strict=False), b"second rule")


class ViewAndMockRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = CaptureMaster(event_loop=self.loop)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.master.options.update(confdir=directory)

    def test_rewrite_to_bypassed_host_never_enters_capture_store(self) -> None:
        flow = tflow.tflow()
        flow.request.url = "http://example.com/secret"
        self.master.rewrite.set_rules(
            RewriteRuleSet(
                [
                    RewriteRule(
                        kind=RewriteKind.MAP_REMOTE,
                        logic=RewriteLogic.CONTAINS,
                        value="example.com",
                        replacement="private.test",
                    )
                ]
            )
        )
        self.master.gateway.set_rules(
            GatewayRuleSet(
                [
                    GatewayRule(
                        layer=GatewayLayer.L7,
                        policy=GatewayPolicy.BYPASS,
                        value="private.test",
                    )
                ]
            ),
            enabled=True,
        )
        seen = []

        def record(flow):
            seen.append(flow.id)

        self.master.view.sig_view_add.connect(record)
        for hook in (http.HttpRequestHeadersHook, http.HttpRequestHook):
            self.loop.run_until_complete(
                self.master.addons.handle_lifecycle(hook(flow))
            )
        flow.response = Response.make(200, b"private content")
        for hook in (http.HttpResponseHeadersHook, http.HttpResponseHook):
            self.loop.run_until_complete(
                self.master.addons.handle_lifecycle(hook(flow))
            )
        self.assertEqual(flow.request.host, "private.test")
        self.assertEqual(seen, [])
        self.assertEqual(list(self.master.view._store), [])

    def test_history_capacity_retains_live_flows_without_killing(self) -> None:
        view = FerretView()
        old, active, newest = (tflow.tflow() for _ in range(3))
        old.live = newest.live = False
        with patch("ferret.core.mitm.view.FLOW_HISTORY_LIMIT", 2):
            view.add([old, active, newest])
        self.assertEqual(list(view._store), [active.id, newest.id])
        self.assertTrue(active.live)
        self.assertIsNone(active.error)

    def test_removing_mock_material_does_not_restore_consumed_entries(self) -> None:
        consumed, removed, remaining = (tflow.tflow(resp=True) for _ in range(3))
        for index, flow in enumerate((consumed, removed, remaining)):
            flow.request.path = f"/{index}"
        addon = self.master.server_playback
        self.master.options.update(server_replay_reuse=False)
        addon.load_flows([consumed, removed, remaining])
        self.assertIs(addon.next_flow(consumed), consumed)
        runtime = Mock(
            spec=MitmRuntime,
            master=self.master,
            view=self.master.view,
            is_running=True,
            mock_enabled=True,
            mock_pool=[consumed, removed, remaining],
        )
        runtime.call.side_effect = lambda callback: callback()
        facade = MitmFacade(runtime)
        runtime.mock_pool = [consumed, removed, remaining]
        with patch.object(facade, "_persist_mock_pool"):
            self.assertEqual(facade.remove_mock_flows([removed.id]), 1)
        self.assertIsNone(addon.next_flow(consumed))
        self.assertIsNone(addon.next_flow(removed))
        self.assertIs(addon.next_flow(remaining), remaining)


class _WireOrigin:
    def __init__(
        self, body: bytes, *, charset: str = "utf-8", hold: bool = False
    ) -> None:
        self.received = threading.Event()
        self.closed = threading.Event()
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(
            target=self._serve, args=(body, charset, hold), daemon=True
        )
        self.thread.start()

    def _serve(self, body: bytes, charset: str, hold: bool) -> None:
        try:
            connection, _ = self.socket.accept()
            with connection:
                connection.settimeout(5)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    request += chunk
                self.received.set()
                length = "" if hold else f"Content-Length: {len(body)}\r\n"
                connection.sendall(
                    f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset={charset}\r\n{length}Connection: close\r\n\r\n".encode()
                    + body
                )
                if hold:
                    while connection.recv(65536):
                        pass
                self.closed.set()
        except OSError:
            self.closed.set()

    def close(self) -> None:
        self.socket.close()
        self.thread.join(timeout=6)


class ProtocolWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch(
                "ferret.core.mitm.runtime.get_certs_dir",
                return_value=Path(self.directory),
            )
        )
        self.runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(self.runtime.stop)
        start_runtime(self.runtime)
        self.facade = MitmFacade(self.runtime)

    def origin(self, body: bytes, **kwargs) -> _WireOrigin:
        origin = _WireOrigin(body, **kwargs)
        self.addCleanup(origin.close)
        return origin

    def test_observer_errors_preserve_actual_client_body(self) -> None:
        for charset, body in (
            ("utf-16", b"data: no BOM\n\n"),
            ("utf-8", b"retry: " + b"9" * 5000 + b"\ndata: ok\n\n"),
        ):
            with self.subTest(charset=charset):
                origin = self.origin(body, charset=charset)
                received = bytearray()
                with socket.create_connection(
                    ("127.0.0.1", self.runtime.listen_port), timeout=3
                ) as client:
                    client.sendall(
                        f"GET http://127.0.0.1:{origin.port}/events HTTP/1.1\r\nHost: 127.0.0.1:{origin.port}\r\nConnection: close\r\n\r\n".encode()
                    )
                    while chunk := client.recv(65536):
                        received.extend(chunk)
                self.assertEqual(bytes(received).partition(b"\r\n\r\n")[2], body)

    def test_compose_timeout_releases_infinite_stream_and_reports_once(self) -> None:
        origin = self.origin(b"data: partial\n\n", hold=True)
        results = []
        self.runtime.compose_result.connect(results.append)
        flow_id = self.facade.send_custom_request(
            "GET", f"http://127.0.0.1:{origin.port}/events", record=False, timeout=0.15
        )
        self.assertTrue(wait_until(lambda: results, timeout_ms=3000))
        self.assertEqual(results[0].flow_id, flow_id)
        self.assertIn("超时", results[0].error)
        self.assertTrue(wait_until(origin.closed.is_set, timeout_ms=1000))
        self.runtime.stop()
        QCoreApplication.processEvents()
        self.assertEqual(len(results), 1)

    def test_compose_stop_reports_terminal_result_before_restart(self) -> None:
        origin = self.origin(b"data: partial\n\n", hold=True)
        results = []
        self.runtime.compose_result.connect(results.append)
        flow_id = self.facade.send_custom_request(
            "GET", f"http://127.0.0.1:{origin.port}/events"
        )
        self.assertTrue(wait_until(origin.received.is_set, timeout_ms=3000))
        self.runtime.stop()
        self.assertTrue(wait_until(lambda: results, timeout_ms=3000))
        self.assertEqual(results[0].flow_id, flow_id)
        self.assertIn("停止", results[0].error)
        start_runtime(self.runtime)
        self.assertEqual(len(results), 1)

    def test_static_mock_sse_archives_events_and_finishes(self) -> None:
        master = self.runtime.master
        assert master is not None
        source = tflow.tflow(resp=True)
        source.request.url = f"http://127.0.0.1:{free_port()}/mock-events"
        source.request.content = b""
        body = b"data: mock\n\n"
        source.response = Response.make(
            200, body, {"Content-Type": "text/event-stream"}
        )
        self.runtime.call(lambda: master.server_playback.load_flows([source]))
        started, ended = [], []
        self.runtime.sse_started.connect(started.append)
        self.runtime.sse_ended.connect(ended.append)
        response = http_through(self.runtime.listen_port, source.request.url)
        self.assertEqual(response.partition(b"\r\n\r\n")[2], body)
        self.assertTrue(wait_until(lambda: ended))
        self.assertEqual(started, ended)
        self.assertEqual(len(ended), 1)
        self.assertEqual(
            [event.data for event in self.facade.sse_events(ended[0])], ["mock"]
        )
        self.assertEqual(self.runtime.call(lambda: len(master.sse._taps)), 0)

    def test_explicit_compose_cancel_closes_connection_and_finishes_once(self) -> None:
        origin = self.origin(b"data: partial\n\n", hold=True)
        results = []
        self.runtime.compose_result.connect(results.append)
        flow_id = self.facade.send_custom_request(
            "GET", f"http://127.0.0.1:{origin.port}/events"
        )
        self.assertTrue(wait_until(origin.received.is_set, timeout_ms=3000))
        self.assertTrue(self.facade.cancel_custom_request(flow_id))
        self.assertFalse(self.facade.cancel_custom_request(flow_id))
        self.assertTrue(wait_until(lambda: results, timeout_ms=3000))
        self.assertIn("取消", results[0].error)
        self.assertTrue(wait_until(origin.closed.is_set, timeout_ms=1000))
        self.runtime.stop()
        QCoreApplication.processEvents()
        self.assertEqual(len(results), 1)
