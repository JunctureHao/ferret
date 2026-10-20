"""Editor WebSocket validation, draft conversion and lifecycle boundaries."""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.core.mitm import (
    MitmFacade,
    MitmRuntime,
    RequestEdit,
    WebSocketData,
    WebSocketSpec,
    WebSocketState,
)
from ferret.core.mitm.bindings import WSConnection, WSConnectionType, ws_events
from ferret.core.mitm.compose import build_compose_flow
from ferret.core.mitm.ui_events import UiEventQueue
from ferret.core.mitm.websocket_client import (
    WebSocketClientAddon,
    validate_websocket_spec,
)


class WebSocketDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_invalid_addresses_rejected_without_starting_runtime(self) -> None:
        facade = MitmFacade(MitmRuntime())
        for url in (
            "",
            "https://example.com",
            "ws://",
            "ws://example.com:0",
            "ws://example.com:65536",
            "ws://user:password@example.com/",
            "ws://example.com/#fragment",
            "ws://example.com/\nheader",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                facade.connect_websocket(WebSocketSpec(url))

    def test_bad_headers_and_subprotocols_rejected(self) -> None:
        for spec in (
            WebSocketSpec("ws://example.com", headers=(("X-Test", "one\r\ntwo"),)),
            WebSocketSpec("ws://example.com", headers=(("bad name", "value"),)),
            WebSocketSpec("ws://example.com", subprotocols=("chat", "chat")),
            WebSocketSpec("ws://example.com", subprotocols=("two names",)),
            WebSocketSpec("ws://example.com", subprotocols=("",)),
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate_websocket_spec(spec)

    def test_handshake_fields_and_local_proxy_credentials_are_discarded(self) -> None:
        spec = validate_websocket_spec(
            WebSocketSpec(
                "wss://example.com/你好?x=1",
                headers=(
                    ("Sec-WebSocket-Key", "stale"),
                    ("Proxy-Authorization", "secret"),
                    ("Cookie", "session=abc"),
                    ("Host", "virtual.example"),
                ),
                subprotocols=("chat",),
            )
        )
        self.assertEqual(
            spec.headers, (("Cookie", "session=abc"), ("Host", "virtual.example"))
        )
        self.assertEqual(spec.url, "wss://example.com/%E4%BD%A0%E5%A5%BD?x=1")

    def test_compose_draft_uses_native_websocket_marker(self) -> None:
        runtime = MitmRuntime()
        facade = MitmFacade(runtime)
        flow = build_compose_flow(
            "GET",
            "https://example.com/chat?x=1",
            [
                ("Cookie", "a=b"),
                ("Sec-WebSocket-Key", "old"),
                ("Sec-WebSocket-Protocol", "chat, v2"),
            ],
            None,
        )
        runtime.view.add([flow])
        # Upgrade-like headers alone do not turn an ordinary HTTP request into WS.
        self.assertIsInstance(facade.compose_draft(flow.id), RequestEdit)
        flow.websocket = WebSocketData()
        draft = facade.compose_draft(flow.id)
        self.assertIsInstance(draft, WebSocketSpec)
        assert isinstance(draft, WebSocketSpec)
        self.assertEqual(draft.url, "wss://example.com/chat?x=1")
        self.assertEqual(draft.subprotocols, ("chat", "v2"))
        self.assertEqual(dict(draft.headers), {"Cookie": "a=b", "Host": "example.com"})
        self.assertIsInstance(facade.request_edit(flow.id), RequestEdit)

    def test_stopping_worker_never_exposes_a_live_flow_to_gui(self) -> None:
        runtime = MitmRuntime()
        runtime._thread = Mock()
        runtime._thread.isRunning.return_value = True
        client = Mock()
        runtime._websocket_client = client
        self.assertIsNone(MitmFacade(runtime).websocket_snapshot("current"))
        client.snapshot.assert_not_called()

    def test_compose_draft_waits_for_worker_ownership_before_reading(self) -> None:
        runtime = MitmRuntime()
        flow = build_compose_flow("GET", "https://example.com/chat", None, None)
        flow.websocket = WebSocketData()
        runtime.view.add([flow])
        runtime._thread = Mock()
        runtime._thread.isRunning.return_value = True
        facade = MitmFacade(runtime)
        with patch.object(runtime.view, "get_by_id") as get_flow:
            with self.assertRaises(RuntimeError):
                facade.compose_draft(flow.id)
            get_flow.assert_not_called()
        runtime._thread.isRunning.return_value = False
        self.assertIsInstance(facade.compose_draft(flow.id), WebSocketSpec)


class WebSocketLifetimeTests(unittest.IsolatedAsyncioTestCase):
    async def _close_peer(self, *, acknowledge: bool, fail_close: bool = False) -> None:
        """A delayed peer exercises the reader/sender task cancellation boundary."""
        received_close = asyncio.Event()
        release_close = asyncio.Event()
        disconnected = asyncio.Event()

        async def serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            protocol = WSConnection(WSConnectionType.SERVER)
            try:
                while data := await reader.read(65536):
                    protocol.receive_data(data)
                    for event in protocol.events():
                        if isinstance(event, ws_events.Request):
                            writer.write(protocol.send(ws_events.AcceptConnection()))
                            await writer.drain()
                        elif isinstance(event, ws_events.CloseConnection):
                            received_close.set()
                            if acknowledge:
                                await release_close.wait()
                                writer.write(protocol.send(event.response()))
                                await writer.drain()
                                return
            finally:
                writer.close()
                await writer.wait_closed()
                disconnected.set()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        client = WebSocketClientAddon(MitmRuntime().view)
        opened = asyncio.Event()
        terminal = asyncio.Event()

        def changed(session_id: str) -> None:
            snapshot = client.snapshot(session_id)
            assert snapshot is not None
            if snapshot.state is WebSocketState.OPEN:
                opened.set()
            elif snapshot.state in (WebSocketState.CLOSED, WebSocketState.FAILED):
                terminal.set()

        client.on_changed = changed
        try:
            session_id = client.connect(
                WebSocketSpec("ws://example.com"),
                "127.0.0.1",
                server.sockets[0].getsockname()[1],
            )
            await asyncio.wait_for(opened.wait(), 2)
            if fail_close:
                with (
                    patch.object(
                        WSConnection,
                        "send",
                        side_effect=RuntimeError("closed transport"),
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    client.close(session_id)
                await asyncio.wait_for(terminal.wait(), 2)
                await asyncio.wait_for(disconnected.wait(), 2)
                snapshot = client.snapshot(session_id)
                assert snapshot is not None
                self.assertEqual(snapshot.state, WebSocketState.FAILED)
                return
            with patch("ferret.core.mitm.websocket_client.WS_CLOSE_TIMEOUT", 0.2):
                client.close(session_id)
                await asyncio.wait_for(received_close.wait(), 2)
                # Cancelling the sender must leave the close reader waiting.
                self.assertFalse(terminal.is_set())
                if acknowledge:
                    release_close.set()
                await asyncio.wait_for(terminal.wait(), 2)
                await asyncio.wait_for(disconnected.wait(), 2)
        finally:
            release_close.set()
            await client.done()
            server.close()
            await server.wait_closed()

    async def test_close_waits_for_peer_after_sender_is_cancelled(self) -> None:
        await self._close_peer(acknowledge=True)

    async def test_close_timeout_really_closes_the_socket(self) -> None:
        await self._close_peer(acknowledge=False)

    async def test_failed_close_frame_still_closes_transport(self) -> None:
        await self._close_peer(acknowledge=False, fail_close=True)

    async def test_close_before_task_starts_releases_slot_and_reports_terminal(
        self,
    ) -> None:
        runtime = MitmRuntime()
        client = WebSocketClientAddon(runtime.view)
        changes = []
        client.on_changed = changes.append
        first = client.connect(WebSocketSpec("ws://example.com"), "127.0.0.1", 1)
        with self.assertRaises(RuntimeError):
            client.connect(WebSocketSpec("ws://example.com"), "127.0.0.1", 1)
        client.close("stale-id")
        first_snapshot = client.snapshot(first)
        assert first_snapshot is not None
        self.assertEqual(first_snapshot.state, WebSocketState.CONNECTING)
        client.close(first)
        await client.done()
        closed = client.snapshot(first)
        assert closed is not None
        self.assertEqual(closed.state, WebSocketState.CLOSED)
        self.assertEqual(client._tasks, set())
        self.assertEqual(client._pending, {})
        second = client.connect(WebSocketSpec("ws://example.com"), "127.0.0.1", 1)
        self.assertNotEqual(second, first)
        self.assertIsNone(client.snapshot(first))
        client.close(first)
        second_snapshot = client.snapshot(second)
        assert second_snapshot is not None
        self.assertEqual(second_snapshot.state, WebSocketState.CONNECTING)
        await client.done()
        self.assertEqual(changes[-1], second)

    async def test_normal_flow_cannot_claim_editor_trust_with_headers(self) -> None:
        runtime = MitmRuntime()
        client = WebSocketClientAddon(runtime.view)
        flow = build_compose_flow(
            "GET",
            "http://example.com/",
            [
                ("Upgrade", "websocket"),
                ("X-Ferret-Compose", "1"),
            ],
            None,
        )
        client.requestheaders(flow)
        self.assertIsNone(flow.is_replay)
        self.assertNotIn("ferret.compose", flow.metadata)


class _Target(QObject):
    compose_websocket_changed = Signal(str)


class WebSocketMailboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_busy_gui_coalesces_by_session_without_message_bodies(self) -> None:
        target = _Target()
        queue = UiEventQueue(target)
        # Simulate an already scheduled wakeup: posting cannot deliver inline.
        queue._scheduled = True
        for _ in range(2000):
            queue.post("compose_websocket_changed", "current")
        queue.post("compose_websocket_changed", "old")
        self.assertEqual(len(queue._events), 2)
        emitted = []
        target.compose_websocket_changed.connect(emitted.append)
        queue.flush()
        self.assertEqual(emitted, ["current", "old"])
        queue.close()
