"""Real editor WS sessions through Ferret, with local origins and isolated CA files."""

from __future__ import annotations

import datetime
import ipaddress
import os
import select
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from PySide6.QtWidgets import QApplication
from wsproto import ConnectionType, WSConnection
from wsproto import events as ws

from ferret.core.mitm import (
    MitmFacade,
    MitmRuntime,
    WebSocketSnapshot,
    WebSocketSpec,
    WebSocketState,
)
from ferret.core.mitm.bindings import certs
from ferret.core.mitm.compose import COMPOSE_RECORD_METADATA_KEY
from tests.core.mitm._qt import start_runtime, wait_until


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Origin)
        connection = self.request
        server.connections.append(connection)
        connection.settimeout(8)
        protocol = WSConnection(ConnectionType.SERVER)
        try:
            while data := connection.recv(65536):
                protocol.receive_data(data)
                for event in protocol.events():
                    if isinstance(event, ws.Request):
                        server.handshakes.append(event)
                        server.received.set()
                        if server.stall:
                            continue
                        if server.reject:
                            connection.sendall(
                                protocol.send(
                                    ws.RejectConnection(
                                        status_code=403,
                                        has_body=True,
                                        headers=[
                                            (b"content-type", b"text/plain"),
                                            (b"content-length", b"6"),
                                        ],
                                    )
                                )
                            )
                            connection.sendall(protocol.send(ws.RejectData(b"denied")))
                            return
                        selected = event.subprotocols[0] if event.subprotocols else None
                        connection.sendall(
                            protocol.send(ws.AcceptConnection(subprotocol=selected))
                        )
                        connection.sendall(protocol.send(ws.Ping(payload=b"probe")))
                    elif isinstance(event, ws.TextMessage | ws.BytesMessage):
                        connection.sendall(protocol.send(event))
                    elif isinstance(event, ws.Pong):
                        server.ponged.set()
                    elif isinstance(event, ws.CloseConnection):
                        connection.sendall(protocol.send(event.response()))
                        return
        except OSError:
            pass
        finally:
            server.closed.set()


class _Origin(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    handler = _Handler

    def __init__(
        self,
        *,
        tls: ssl.SSLContext | None = None,
        reject: bool = False,
        stall: bool = False,
    ):
        self.tls = tls
        self.reject = reject
        self.stall = stall
        self.received = threading.Event()
        self.ponged = threading.Event()
        self.closed = threading.Event()
        self.handshakes: list[ws.Request] = []
        self.connections: list[socket.socket] = []
        super().__init__(("127.0.0.1", 0), self.handler)
        self.thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def get_request(self):
        connection, address = super().get_request()
        if self.tls is not None:
            connection.settimeout(5)
            try:
                connection = self.tls.wrap_socket(connection, server_side=True)
            except OSError:
                connection.close()
                raise
        return connection, address

    @property
    def url(self) -> str:
        scheme = "wss" if self.tls is not None else "ws"
        return f"{scheme}://127.0.0.1:{self.server_address[1]}/chat?q=a%20b&x=%2f"

    def close(self) -> None:
        for connection in self.connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=3)


def _free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Upstream)
        downstream = self.request
        downstream.settimeout(5)
        server.connections.append(downstream)
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = downstream.recv(65536)
                if not chunk:
                    return
                data += chunk
            header, rest = data.split(b"\r\n\r\n", 1)
            first, *lines = header.split(b"\r\n")
            method, target, version = first.decode("ascii").split(" ")
            headers = dict(line.split(b":", 1) for line in lines)
            authorization = next(
                (
                    value.strip()
                    for key, value in headers.items()
                    if key.lower() == b"proxy-authorization"
                ),
                b"",
            )
            server.requests.append((method, target, authorization))
            if authorization != b"Basic dXNlcjpwYXNz":
                downstream.sendall(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"
                )
                return
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
                forward = b""
            else:
                parsed = urlsplit(target)
                host, port = parsed.hostname, parsed.port or 80
                path = (parsed.path or "/") + (
                    "?" + parsed.query if parsed.query else ""
                )
                forward = (
                    f"{method} {path} {version}\r\n".encode()
                    + b"\r\n".join(
                        line
                        for line in lines
                        if not line.lower().startswith(b"proxy-authorization:")
                    )
                    + b"\r\n\r\n"
                    + rest
                )
            with socket.create_connection((host, int(port)), timeout=5) as upstream:
                if method == "CONNECT":
                    downstream.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                else:
                    upstream.sendall(forward)
                while True:
                    readable, _, _ = select.select([downstream, upstream], [], [], 0.1)
                    if isinstance(downstream, ssl.SSLSocket) and downstream.pending():
                        readable.append(downstream)
                    for source in readable:
                        chunk = source.recv(65536)
                        if not chunk:
                            return
                        (upstream if source is downstream else downstream).sendall(
                            chunk
                        )
        except (OSError, ValueError):
            pass


class _Upstream(_Origin):
    handler = _ProxyHandler

    def __init__(self, *, tls: ssl.SSLContext | None = None):
        self.requests: list[tuple[str, str, bytes]] = []
        super().__init__(tls=tls)


class WebSocketLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            patch(
                "ferret.core.mitm.runtime.get_certs_dir",
                return_value=self.directory / "proxy",
            )
        )
        self.enterContext(
            patch(
                "ferret.core.mitm.certificate.get_certs_dir",
                return_value=self.directory / "proxy",
            )
        )
        self.runtime = MitmRuntime(listen_port=_free_port())
        self.addCleanup(self.runtime.stop)
        self.facade = MitmFacade(self.runtime)
        self.changed: list[str] = []
        self.runtime.compose_websocket_changed.connect(self.changed.append)

    def origin(self, **kwargs) -> _Origin:
        origin = _Origin(**kwargs)
        self.addCleanup(origin.close)
        return origin

    def snapshot(self, session_id: str) -> WebSocketSnapshot:
        result = self.facade.websocket_snapshot(session_id)
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def wait_state(self, session_id: str, state: WebSocketState) -> WebSocketSnapshot:
        self.assertTrue(
            wait_until(
                lambda: self.snapshot(session_id).state == state, timeout_ms=6000
            ),
            repr(self.snapshot(session_id)),
        )
        return self.snapshot(session_id)

    def connect(self, origin: _Origin, **kwargs) -> str:
        session_id = self.facade.connect_websocket(WebSocketSpec(origin.url, **kwargs))
        self.wait_state(session_id, WebSocketState.OPEN)
        return session_id

    def send_echo(
        self, session_id: str, payload: bytes, *, is_text: bool = True
    ) -> None:
        before = self.snapshot(session_id).message_count
        self.facade.send_websocket(session_id, payload, is_text=is_text)
        self.assertTrue(
            wait_until(
                lambda: self.snapshot(session_id).message_count >= before + 2,
                timeout_ms=5000,
            )
        )
        frames = self.snapshot(session_id).frames[-2:]
        self.assertEqual([frame.content for frame in frames], [payload, payload])
        self.assertEqual([frame.from_client for frame in frames], [True, False])
        self.assertEqual([frame.opcode for frame in frames], [1 if is_text else 2] * 2)

    def test_text_binary_empty_subprotocol_and_ping_through_proxy(self) -> None:
        origin = self.origin()
        self.runtime.proxyauth_enabled = True
        self.runtime.proxyauth_username = "local"
        self.runtime.proxyauth_password = "secret"
        start_runtime(self.runtime)
        session_id = self.connect(
            origin,
            subprotocols=("chat.v2", "chat"),
            headers=(("Origin", "https://example.test"),),
        )
        self.assertFalse(self.runtime.channels_engaged)
        self.assertEqual(self.snapshot(session_id).subprotocol, "chat.v2")
        self.assertEqual(origin.handshakes[0].target, "/chat?q=a%20b&x=%2f")
        self.assertNotIn(
            b"proxy-authorization", dict(origin.handshakes[0].extra_headers)
        )
        for payload, text in (
            ("你好\r\n".encode(), True),
            (b"\x00\xff", False),
            (b"", True),
            (b"", False),
        ):
            self.send_echo(session_id, payload, is_text=text)
        self.assertTrue(wait_until(origin.ponged.is_set))
        self.assertTrue(self.changed)
        self.facade.close_websocket(session_id)
        snapshot = self.wait_state(session_id, WebSocketState.CLOSED)
        self.assertEqual(snapshot.close.close_code, 1000)
        self.assertTrue(wait_until(origin.closed.is_set))

    def test_unrecorded_session_keeps_final_history_outside_view(self) -> None:
        origin = self.origin()
        start_runtime(self.runtime)
        session_id = self.connect(origin, record=False)
        self.send_echo(session_id, b"retained")
        self.assertEqual(self.facade.visible_flow_rows(), [])
        flow_id = self.snapshot(session_id).flow_id
        self.facade.close_websocket(session_id)
        self.wait_state(session_id, WebSocketState.CLOSED)
        self.assertTrue(
            wait_until(
                lambda: (
                    self.runtime.call(lambda: self.runtime.view.get_by_id(flow_id))
                    is None
                )
            )
        )
        self.assertEqual(self.snapshot(session_id).frames[-1].content, b"retained")
        self.assertEqual(self.facade.total_count(), 0)

    def test_removal_disconnects_socket_and_reconnect_is_a_new_session(self) -> None:
        origin = self.origin()
        start_runtime(self.runtime)
        session_id = self.connect(origin)
        flow_id = self.snapshot(session_id).flow_id
        self.facade.remove_flows([flow_id])
        self.assertTrue(wait_until(origin.closed.is_set, timeout_ms=5000))
        self.assertTrue(
            wait_until(
                lambda: (
                    self.snapshot(session_id).state
                    in (WebSocketState.CLOSED, WebSocketState.FAILED)
                )
            )
        )
        next_id = self.connect(origin)
        self.assertNotEqual(session_id, next_id)
        self.assertNotEqual(flow_id, self.snapshot(next_id).flow_id)
        with self.assertRaises((ValueError, RuntimeError)):
            self.facade.send_websocket(session_id, b"stale")
        self.send_echo(next_id, b"fresh")

    def test_runtime_shutdown_closes_connection_and_retains_terminal_snapshot(
        self,
    ) -> None:
        origin = self.origin()
        start_runtime(self.runtime)
        session_id = self.connect(origin)
        self.send_echo(session_id, b"before-stop")
        self.assertTrue(self.runtime.stop())
        self.assertTrue(wait_until(origin.closed.is_set, timeout_ms=5000))
        snapshot = self.snapshot(session_id)
        self.assertIn(snapshot.state, (WebSocketState.CLOSED, WebSocketState.FAILED))
        self.assertEqual(snapshot.frames[-1].content, b"before-stop")

    def test_rejected_handshake_has_response_details_and_can_retry(self) -> None:
        rejected = self.origin(reject=True)
        start_runtime(self.runtime)
        session_id = self.facade.connect_websocket(
            WebSocketSpec(rejected.url, record=False)
        )
        snapshot = self.wait_state(session_id, WebSocketState.FAILED)
        self.assertTrue(snapshot.error)
        self.assertEqual(snapshot.detail["Status Code"], 403)
        self.assertTrue(wait_until(lambda: self.facade.all_http_flows() == []))
        self.connect(self.origin())

    def test_connect_can_be_cancelled_and_has_a_handshake_timeout(self) -> None:
        origin = self.origin(stall=True)
        start_runtime(self.runtime)
        session_id = self.facade.connect_websocket(WebSocketSpec(origin.url))
        self.assertTrue(wait_until(origin.received.is_set))
        self.facade.close_websocket(session_id)
        self.wait_state(session_id, WebSocketState.CLOSED)
        origin.received.clear()
        with patch("ferret.core.mitm.websocket_client.WS_HANDSHAKE_TIMEOUT", 0.15):
            session_id = self.facade.connect_websocket(WebSocketSpec(origin.url))
            self.assertTrue(self.wait_state(session_id, WebSocketState.FAILED).error)

    def tls_material(
        self, *, require_client: bool = False
    ) -> tuple[ssl.SSLContext, Path, Path]:
        key, ca = certs.create_ca("Ferret WS Test", "Ferret WS CA", 2048)
        # The proxy's normal CA is deliberately restricted to SERVER_AUTH.
        # This test authority also issues a client certificate for mTLS.
        builder = (
            x509.CertificateBuilder()
            .subject_name(ca.subject)
            .issuer_name(ca.issuer)
            .public_key(ca.public_key())
            .serial_number(ca.serial_number)
            .not_valid_before(ca.not_valid_before_utc)
            .not_valid_after(ca.not_valid_after_utc)
        )
        for extension in ca.extensions:
            if not isinstance(extension.value, x509.ExtendedKeyUsage):
                builder = builder.add_extension(extension.value, extension.critical)
        ca = builder.sign(key, hashes.SHA256())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        leaf = certs.dummy_cert(
            key,
            ca,
            "localhost",
            [
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                x509.DNSName("localhost"),
            ],
        )
        root = self.directory / "origin-ca.pem"
        root.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
        server_pem = self.directory / "origin.pem"
        server_pem.write_bytes(pem + leaf.to_pem())
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_pem)
        now = datetime.datetime.now(datetime.UTC)
        client = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ws-client")])
            )
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        client_pem = self.directory / "client.pem"
        client_pem.write_bytes(pem + client.public_bytes(serialization.Encoding.PEM))
        if require_client:
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations(root)
        return context, root, client_pem

    def test_wss_uses_native_trust_and_client_certificate(self) -> None:
        tls, root, client_pem = self.tls_material(require_client=True)
        origin = self.origin(tls=tls)
        self.runtime.ssl_trusted_ca_files = [str(root)]
        self.runtime.client_certs_path = str(client_pem)
        start_runtime(self.runtime)
        session_id = self.connect(origin)
        self.send_echo(session_id, b"secure")
        flow = self.facade.all_http_flows()[0]
        self.assertTrue(flow.server_conn.tls)
        self.assertIs(flow.metadata[COMPOSE_RECORD_METADATA_KEY], True)

    def test_wss_rejects_untrusted_origin(self) -> None:
        tls, _, _ = self.tls_material()
        origin = self.origin(tls=tls)
        start_runtime(self.runtime)
        session_id = self.facade.connect_websocket(WebSocketSpec(origin.url))
        snapshot = self.wait_state(session_id, WebSocketState.FAILED)
        self.assertTrue(snapshot.error)
        self.assertFalse(origin.handshakes)

    def test_http_upstream_auth_and_wss_origin_form(self) -> None:
        tls, root, _ = self.tls_material()
        upstream = _Upstream()
        self.addCleanup(upstream.close)
        self.runtime.ssl_trusted_ca_files = [str(root)]
        self.runtime.use_upstream = True
        self.runtime.upstream_target = f"http://127.0.0.1:{upstream.server_address[1]}"
        self.runtime.upstream_username = "user"
        self.runtime.upstream_password = "pass"
        self.runtime.set_channels_engaged(True)
        start_runtime(self.runtime)
        for secure in (False, True):
            with self.subTest(secure=secure):
                origin = self.origin(tls=tls if secure else None)
                session_id = self.connect(origin)
                self.send_echo(session_id, b"through-upstream")
                self.assertEqual(origin.handshakes[0].target, "/chat?q=a%20b&x=%2f")
                self.assertEqual(
                    upstream.requests[-1][0], "CONNECT" if secure else "GET"
                )
                self.assertEqual(upstream.requests[-1][2], b"Basic dXNlcjpwYXNz")
                self.facade.close_websocket(session_id)
                self.wait_state(session_id, WebSocketState.CLOSED)

    def test_https_upstream_keeps_active_session_when_capture_disengages(self) -> None:
        tls, root, _ = self.tls_material()
        upstream = _Upstream(tls=tls)
        self.addCleanup(upstream.close)
        origin = self.origin(tls=tls)
        self.runtime.ssl_trusted_ca_files = [str(root)]
        self.runtime.use_upstream = True
        self.runtime.upstream_target = f"https://127.0.0.1:{upstream.server_address[1]}"
        self.runtime.upstream_username = "user"
        self.runtime.upstream_password = "pass"
        self.runtime.set_channels_engaged(True)
        start_runtime(self.runtime)
        session_id = self.connect(origin)
        self.send_echo(session_id, b"before-stop-capture")
        self.runtime.set_channels_engaged(False)
        self.send_echo(session_id, b"after-stop-capture")
        self.assertEqual(upstream.requests[-1][0], "CONNECT")
