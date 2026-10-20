"""An owned WebSocket client routed through the native proxy pipeline.

Only the loopback leg is implemented here. mitmproxy owns routing, remote TLS,
rewrites and the authoritative message history. The socket's two endpoints are
registered before any HTTP bytes are sent, so an early hook can identify the
trusted Compose request without leaking a marker or proxy credentials upstream.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import (
    Flow,
    HTTPFlow,
    View,
    WSConnection,
    WSConnectionState,
    WSConnectionType,
    ws_events,
)
from ferret.core.mitm.compose import COMPOSE_METADATA_KEY, COMPOSE_RECORD_METADATA_KEY
from ferret.core.mitm.detail import build_flow_body, build_flow_summary
from ferret.core.mitm.held_edit import advance_edit_epoch
from ferret.core.mitm.wsframe import (
    WS_FRAME_LIMIT,
    WsClose,
    WsFrame,
    ws_close,
    ws_frame_offset,
    ws_frames,
)

WS_SEND_MAX_BYTES = 1024 * 1024
WS_SEND_QUEUE_BYTES = 4 * 1024 * 1024
WS_SEND_QUEUE_LIMIT = 64
WS_HANDSHAKE_TIMEOUT = 15.0
WS_CLOSE_TIMEOUT = 3.0
WS_WRITE_TIMEOUT = 10.0
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
# Host may be explicitly overridden; all other handshake fields belong to the
# protocol implementation. Compression is intentionally not negotiated.
WEBSOCKET_MANAGED_HEADERS = frozenset(
    {
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-accept",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "content-length",
        "transfer-encoding",
        "proxy-authorization",
        "proxy-connection",
    }
)


class WebSocketState(StrEnum):
    CONNECTING = "connecting"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WebSocketSpec:
    url: str
    headers: tuple[tuple[str, str], ...] = ()
    subprotocols: tuple[str, ...] = ()
    record: bool = True


@dataclass(frozen=True, slots=True)
class WebSocketSnapshot:
    session_id: str
    flow_id: str = ""
    state: WebSocketState = WebSocketState.CONNECTING
    frames: tuple[WsFrame, ...] = ()
    close: WsClose = field(default_factory=WsClose)
    error: str = ""
    detail: dict = field(default_factory=dict)
    subprotocol: str = ""
    message_count: int = 0


def validate_websocket_spec(spec: WebSocketSpec) -> WebSocketSpec:
    """Validate before starting a socket or replacing the previous session."""
    try:
        parsed = urlsplit(spec.url.strip())
        valid = (
            parsed.scheme in {"ws", "wss"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
            and not any(ord(c) < 33 or ord(c) == 127 for c in spec.url.strip())
        )
        port = parsed.port
        if not valid or port == 0:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        if ":" in host:
            host = f"[{host}]"
        authority = host + (f":{port}" if port is not None else "")
        normalized = urlunsplit(
            (
                parsed.scheme,
                authority,
                quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~"),
                quote(parsed.query, safe="/%?:@!$&'()*+,;=-._~"),
                "",
            )
        )
    except (ValueError, UnicodeError) as exc:
        raise ValueError(
            QCoreApplication.translate(
                "WebSocketClient", "请输入有效的 ws:// 或 wss:// 地址"
            )
        ) from exc
    headers = []
    for key, value in spec.headers:
        if not _TOKEN.fullmatch(key) or any(c in value for c in "\r\n\x00"):
            raise ValueError(
                QCoreApplication.translate("WebSocketClient", "WebSocket 请求头无效")
            )
        if key.lower() not in WEBSOCKET_MANAGED_HEADERS:
            headers.append((key, value))
    if len(set(spec.subprotocols)) != len(spec.subprotocols) or any(
        not _TOKEN.fullmatch(protocol) for protocol in spec.subprotocols
    ):
        raise ValueError(
            QCoreApplication.translate(
                "WebSocketClient", "WebSocket 子协议必须是不重复的协议名称"
            )
        )
    return replace(
        spec,
        url=normalized,
        headers=tuple(headers),
        subprotocols=tuple(spec.subprotocols),
    )


def build_websocket_spec(flow: HTTPFlow) -> WebSocketSpec:
    """Build a reusable draft while discarding per-connection handshake fields."""
    request = flow.request
    protocols = tuple(
        part.strip()
        for value in request.headers.get_all("sec-websocket-protocol")
        for part in value.split(",")
        if part.strip()
    )
    url = request.url
    if url.startswith("https:"):
        url = "wss:" + url[6:]
    elif url.startswith("http:"):
        url = "ws:" + url[5:]
    return WebSocketSpec(
        url=url,
        headers=tuple(
            (key, value)
            for key, value in request.headers.items(multi=True)
            if key.lower() not in WEBSOCKET_MANAGED_HEADERS
        ),
        subprotocols=protocols,
    )


@dataclass(eq=False)
class _Session:
    spec: WebSocketSpec
    id: str = field(default_factory=lambda: str(uuid4()))
    state: WebSocketState = WebSocketState.CONNECTING
    flow: HTTPFlow | None = None
    error: str = ""
    subprotocol: str = ""
    detail: dict = field(default_factory=dict)
    protocol: WSConnection = field(
        default_factory=lambda: WSConnection(WSConnectionType.CLIENT)
    )
    writer: asyncio.StreamWriter | None = None
    task: asyncio.Task[None] | None = None
    sender: asyncio.Task[None] | None = None
    close_timer: asyncio.TimerHandle | None = None
    queue: asyncio.Queue[tuple[bytes, bool]] = field(default_factory=asyncio.Queue)
    queued_bytes: int = 0
    endpoints: tuple | None = None
    handshake_done: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def active(self) -> bool:
        return self.state in {
            WebSocketState.CONNECTING,
            WebSocketState.OPEN,
            WebSocketState.CLOSING,
        }


class WebSocketClientAddon:
    """A single editor connection, with independent cancellable task ownership.

    All methods run on the mitm loop (snapshot may also run after it has stopped).
    The last native flow remains reachable until the next connect, including a
    non-recorded flow removed from View. No separate message history is kept.
    """

    def __init__(self, view: View) -> None:
        self._view = view
        self._session: _Session | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[tuple, _Session] = {}
        self.on_changed: Callable[[str], None] | None = None
        self.on_cancel: Callable[[HTTPFlow], None] | None = None

    def connect(self, spec: WebSocketSpec, host: str, port: int) -> str:
        spec = validate_websocket_spec(spec)
        if self._session is not None and self._session.active:
            raise RuntimeError(
                QCoreApplication.translate(
                    "WebSocketClient", "请先断开当前 WebSocket 连接"
                )
            )
        session = _Session(spec)
        self._session = session
        task = asyncio.create_task(
            self._run(session, host, port), name="compose websocket"
        )
        session.task = task
        self._tasks.add(task)

        def task_done(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if session.task is completed:
                session.task = None

        task.add_done_callback(task_done)
        self._notify(session)
        return session.id

    def _notify(self, session: _Session) -> None:
        if self._session is session and self.on_changed is not None:
            self.on_changed(session.id)

    def snapshot(self, session_id: str) -> WebSocketSnapshot | None:
        session = self._session
        if session is None or session.id != session_id:
            return None
        flow = session.flow
        if flow is None:
            return WebSocketSnapshot(
                session.id, state=session.state, error=session.error
            )
        websocket = flow.websocket
        offset = ws_frame_offset(flow)
        return WebSocketSnapshot(
            session.id,
            flow.id,
            session.state,
            tuple(ws_frames(websocket, limit=WS_FRAME_LIMIT, offset=offset)),
            ws_close(websocket),
            session.error,
            session.detail,
            session.subprotocol,
            offset + len(websocket.messages) if websocket is not None else 0,
        )

    def send(self, session_id: str, content: bytes, *, is_text: bool = True) -> None:
        session = self._session
        if (
            session is None
            or session.id != session_id
            or session.state is not WebSocketState.OPEN
        ):
            raise RuntimeError(
                QCoreApplication.translate("WebSocketClient", "WebSocket 连接尚未打开")
            )
        if len(content) > WS_SEND_MAX_BYTES:
            raise ValueError(
                QCoreApplication.translate(
                    "WebSocketClient", "WebSocket 消息不能超过 {limit} 字节"
                ).format(limit=WS_SEND_MAX_BYTES)
            )
        if is_text:
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(
                    QCoreApplication.translate(
                        "WebSocketClient", "文本消息必须是有效的 UTF-8"
                    )
                ) from exc
        if (
            session.queue.qsize() >= WS_SEND_QUEUE_LIMIT
            or session.queued_bytes + len(content) > WS_SEND_QUEUE_BYTES
        ):
            raise ValueError(
                QCoreApplication.translate(
                    "WebSocketClient", "WebSocket 发送队列已满，请稍后重试"
                )
            )
        session.queued_bytes += len(content)
        session.queue.put_nowait((bytes(content), is_text))

    def close(self, session_id: str) -> None:
        session = self._session
        if session is None or session.id != session_id or not session.active:
            return
        if session.state is WebSocketState.CLOSING:
            return
        was_open = session.state is WebSocketState.OPEN
        session.state = WebSocketState.CLOSING
        self._notify(session)
        if was_open and session.writer is not None:
            if session.sender is not None:
                session.sender.cancel()
            try:
                session.writer.write(
                    session.protocol.send(ws_events.CloseConnection(code=1000))
                )
                session.close_timer = asyncio.get_running_loop().call_later(
                    WS_CLOSE_TIMEOUT, self._abort, session
                )
            except Exception as exc:
                session.error = QCoreApplication.translate(
                    "WebSocketClient", "WebSocket 连接失败：{error}"
                ).format(error=exc)
                self._abort(session)
                raise RuntimeError(session.error) from exc
        else:
            self._abort(session)

    def abort_flow(self, flow_id: str) -> None:
        session = self._session
        if (
            session is not None
            and session.flow is not None
            and session.flow.id == flow_id
        ):
            self._abort(session)

    def flow_removed(self, flow: Flow) -> None:
        self.abort_flow(flow.id)

    def _release(self, session: _Session) -> None:
        flow = session.flow
        if flow is None:
            return
        advance_edit_epoch(flow)
        flow.resume()
        if flow.killable:
            flow.kill()
        if self.on_cancel is not None:
            self.on_cancel(flow)

    def _abort(self, session: _Session) -> None:
        if not session.active:
            return
        self._release(session)
        if session.writer is not None:
            session.writer.close()
        if session.task is not None:
            session.task.cancel()
        # Also covers cancellation before the coroutine's first instruction.
        self._finish(session)

    def _finish(self, session: _Session) -> None:
        if session.close_timer is not None:
            session.close_timer.cancel()
            session.close_timer = None
        if session.endpoints is not None:
            self._pending.pop(session.endpoints, None)
        session.state = (
            WebSocketState.FAILED if session.error else WebSocketState.CLOSED
        )
        # Set terminal state first: removal signals must not recurse into abort.
        if not session.spec.record and session.flow is not None:
            self._view.remove([session.flow])
        self._notify(session)

    async def done(self) -> None:
        session = self._session
        if session is not None and session.active:
            self._abort(session)
        tasks = list(self._tasks)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._pending.clear()

    def requestheaders(self, flow: HTTPFlow) -> None:
        endpoints = (flow.client_conn.peername, flow.client_conn.sockname)
        session = self._pending.pop(endpoints, None)
        if session is None or not session.active:
            return
        session.flow = flow
        flow.metadata[COMPOSE_METADATA_KEY] = "1"
        flow.metadata[COMPOSE_RECORD_METADATA_KEY] = session.spec.record
        flow.is_replay = "request"
        # An absolute HTTPS request over the trusted plaintext loopback leg still
        # uses native upstream CONNECT + TLS. Within that tunnel the target must
        # receive origin-form; plain HTTP keeps absolute-form for upstream proxies.
        if flow.request.scheme == "https":
            flow.request.authority = ""
        asyncio.get_running_loop().call_soon(self._refresh_detail, session)

    def _refresh_detail(self, session: _Session) -> None:
        flow = session.flow
        if flow is None:
            return
        # The editor displays the HTTP handshake. Never call get_state here:
        # it would copy native WS messages into a second growing history. Body
        # decoding happens once per HTTP response/error, not on frame arrivals.
        detail = build_flow_summary(flow)
        detail.update(build_flow_body(flow, "Request"))
        detail.update(build_flow_body(flow, "Response"))
        session.detail = detail
        # A cancellation can race the native HTTP error hook's View admission.
        # Keep non-recorded terminal handshakes hidden even when that hook runs
        # after the owned client task has already closed its socket.
        if not session.active and not session.spec.record:
            self._view.remove([flow])
        self._notify(session)

    def _flow_changed(self, flow: HTTPFlow) -> None:
        session = self._session
        if session is not None and session.flow is flow:
            # Observe post-script and post-View trimming values, after the hook.
            asyncio.get_running_loop().call_soon(self._notify, session)

    def response(self, flow: HTTPFlow) -> None:
        session = self._session
        if session is not None and session.flow is flow:
            asyncio.get_running_loop().call_soon(self._refresh_detail, session)

    def error(self, flow: HTTPFlow) -> None:
        self.response(flow)

    def websocket_message(self, flow: HTTPFlow) -> None:
        self._flow_changed(flow)

    def websocket_end(self, flow: HTTPFlow) -> None:
        session = self._session
        if session is not None and session.flow is flow:
            session.ended.set()
            self._flow_changed(flow)

    async def _send_loop(self, session: _Session) -> None:
        assert session.writer is not None
        while True:
            content, is_text = await session.queue.get()
            try:
                event = (
                    ws_events.TextMessage(data=content.decode("utf-8"))
                    if is_text
                    else ws_events.BytesMessage(data=content)
                )
                session.writer.write(session.protocol.send(event))
                await asyncio.wait_for(session.writer.drain(), WS_WRITE_TIMEOUT)
            finally:
                session.queued_bytes -= len(content)
                session.queue.task_done()

    async def _read(self, session: _Session, reader: asyncio.StreamReader) -> None:
        assert session.writer is not None
        while True:
            data = await reader.read(64 * 1024)
            session.protocol.receive_data(data or None)
            for event in session.protocol.events():
                if isinstance(event, ws_events.AcceptConnection):
                    session.subprotocol = event.subprotocol or ""
                    session.state = WebSocketState.OPEN
                    session.sender = asyncio.create_task(
                        self._send_loop(session), name="websocket sender"
                    )
                    session.handshake_done.set()
                    self._notify(session)
                elif isinstance(event, ws_events.RejectConnection):
                    session.error = QCoreApplication.translate(
                        "WebSocketClient", "WebSocket 握手失败：HTTP {status}"
                    ).format(status=event.status_code)
                    if not event.has_body:
                        return
                elif isinstance(event, ws_events.RejectData):
                    # The native HTTP response owns the body and its detail view.
                    if event.body_finished:
                        return
                elif isinstance(event, ws_events.Ping):
                    session.writer.write(session.protocol.send(event.response()))
                elif isinstance(event, ws_events.CloseConnection):
                    if session.protocol.state is WSConnectionState.REMOTE_CLOSING:
                        session.writer.write(session.protocol.send(event.response()))
                        await asyncio.wait_for(session.writer.drain(), WS_CLOSE_TIMEOUT)
                    if (
                        event.code == 1006
                        and session.state is not WebSocketState.CLOSING
                    ):
                        session.error = QCoreApplication.translate(
                            "WebSocketClient", "WebSocket 连接异常关闭"
                        )
                    return
                # Message payloads are consumed by wsproto without accumulating a
                # second history; native websocket_message owns the display data.
            if not data:
                raise ConnectionError(
                    QCoreApplication.translate(
                        "WebSocketClient", "WebSocket 连接已断开"
                    )
                )

    async def _run(self, session: _Session, host: str, port: int) -> None:
        reader_task: asyncio.Task[None] | None = None
        handshake_timer: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(WS_HANDSHAKE_TIMEOUT) as handshake_timer:
                reader, writer = await asyncio.open_connection(host, port)
                session.writer = writer
                session.endpoints = (
                    writer.get_extra_info("sockname"),
                    writer.get_extra_info("peername"),
                )
                self._pending[session.endpoints] = session
                parsed = urlsplit(session.spec.url)
                target = (
                    "https" if parsed.scheme == "wss" else "http"
                ) + session.spec.url[len(parsed.scheme) :]
                hostname = parsed.netloc
                headers = []
                for key, value in session.spec.headers:
                    if key.lower() == "host":
                        hostname = value
                    else:
                        headers.append(
                            (
                                key.encode("ascii"),
                                value.encode("utf-8", "surrogateescape"),
                            )
                        )
                writer.write(
                    session.protocol.send(
                        ws_events.Request(
                            host=hostname,
                            target=target,
                            extra_headers=headers,
                            subprotocols=list(session.spec.subprotocols),
                        )
                    )
                )
                await writer.drain()
                reader_task = asyncio.create_task(
                    self._read(session, reader), name="websocket reader"
                )
                reader_task.add_done_callback(lambda _: session.handshake_done.set())
                # OPEN is set by the reader. Keep this timeout scoped to the HTTP
                # handshake, leaving an open connection without an idle deadline.
                await session.handshake_done.wait()
            if reader_task is not None:
                pending = {reader_task}
                if session.sender is not None:
                    pending.add(session.sender)
                done, _ = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    # Local close cancels just the sender while awaiting peer close.
                    if (
                        task is session.sender
                        and task.cancelled()
                        and session.state is WebSocketState.CLOSING
                    ):
                        await reader_task
                    else:
                        task.result()
        except asyncio.CancelledError:
            self._release(session)
        except TimeoutError:
            session.error = (
                QCoreApplication.translate("WebSocketClient", "WebSocket 握手超时")
                if handshake_timer is not None and handshake_timer.expired()
                else QCoreApplication.translate("WebSocketClient", "WebSocket 写入超时")
            )
            self._release(session)
        except Exception as exc:  # noqa: BLE001
            session.error = QCoreApplication.translate(
                "WebSocketClient", "WebSocket 连接失败：{error}"
            ).format(error=exc)
            self._release(session)
        finally:
            children = [
                task for task in (reader_task, session.sender) if task is not None
            ]
            for task in children:
                task.cancel()
            # Close the transport before any await. A stop arriving during an
            # already-running cleanup must not interrupt socket disposal.
            if session.writer is not None:
                session.writer.close()
            try:
                if children:
                    await asyncio.gather(*children, return_exceptions=True)
                if session.writer is not None:
                    try:
                        await asyncio.wait_for(session.writer.wait_closed(), 1.0)
                    except (OSError, TimeoutError):
                        pass
                if session.flow is not None and session.flow.websocket is not None:
                    try:
                        await asyncio.wait_for(session.ended.wait(), 1.0)
                    except TimeoutError:
                        pass
            finally:
                while not session.queue.empty():
                    session.queue.get_nowait()
                    session.queue.task_done()
                session.queued_bytes = 0
                session.sender = None
                session.writer = None
                session.task = None
                self._finish(session)
