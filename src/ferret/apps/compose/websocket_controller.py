"""Long-lived Compose WebSocket state, independent of HTTP sends and page visibility."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from ferret.core.mitm import (
    MitmFacade,
    MitmRuntimeState,
    WebSocketSnapshot,
    WebSocketSpec,
    WebSocketState,
)


class ComposeWebSocketController(QObject):
    changed = Signal(object)  # WebSocketSnapshot | None
    failed = Signal(str, str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        self._snapshot: WebSocketSnapshot | None = None
        self._stopped_session = ""
        self._recorded = False
        mitm.runtime.compose_websocket_changed.connect(self._on_changed)
        mitm.runtime.state_changed.connect(self._on_runtime_state)

    @property
    def snapshot(self) -> WebSocketSnapshot | None:
        return self._snapshot

    @property
    def recorded(self) -> bool:
        return self._recorded

    @property
    def active(self) -> bool:
        return self._snapshot is not None and self._snapshot.state in (
            WebSocketState.CONNECTING,
            WebSocketState.OPEN,
            WebSocketState.CLOSING,
        )

    def reset(self) -> None:
        if self.active:
            return
        self._snapshot = None
        self._recorded = False
        self.changed.emit(None)

    def open_connection(self, spec: WebSocketSpec) -> None:
        if self.active:
            return
        try:
            session_id = self._mitm.connect_websocket(spec)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.failed.emit(self.tr("连接失败"), str(exc))
            return
        self._stopped_session = ""
        self._recorded = spec.record
        self._snapshot = WebSocketSnapshot(
            session_id=session_id, state=WebSocketState.CONNECTING
        )
        self.changed.emit(self._snapshot)
        self.refresh()

    def refresh(self) -> None:
        current = self._snapshot
        if current is None:
            return
        try:
            snapshot = self._mitm.websocket_snapshot(current.session_id)
        except (RuntimeError, TimeoutError):
            return
        if snapshot is None or snapshot.session_id != current.session_id:
            return
        if snapshot.session_id == self._stopped_session and snapshot.state not in (
            WebSocketState.CLOSED,
            WebSocketState.FAILED,
        ):
            return
        # Notifications carry IDs only; a late queued notification must never
        # replace the current connection, or revive a locally terminated one.
        self._snapshot = snapshot
        self.changed.emit(snapshot)

    def send(self, content: bytes, *, is_text: bool) -> None:
        snapshot = self._snapshot
        if snapshot is None or snapshot.state != WebSocketState.OPEN:
            return
        try:
            self._mitm.send_websocket(snapshot.session_id, content, is_text=is_text)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.failed.emit(self.tr("消息发送失败"), str(exc))
        self.refresh()

    def close(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or not self.active:
            return
        try:
            self._mitm.close_websocket(snapshot.session_id)
        except (ValueError, RuntimeError, OSError) as exc:
            self.failed.emit(self.tr("断开失败"), str(exc))
            self.refresh()
            return
        self._snapshot = replace(snapshot, state=WebSocketState.CLOSING)
        self.changed.emit(self._snapshot)
        self.refresh()

    def _on_changed(self, session_id: str) -> None:
        if self._snapshot is not None and self._snapshot.session_id == session_id:
            self.refresh()

    def _on_runtime_state(self, state: MitmRuntimeState) -> None:
        if state == MitmRuntimeState.RUNNING:
            return
        snapshot = self._snapshot
        if snapshot is None:
            return
        if self.active:
            self._stopped_session = snapshot.session_id
            self._snapshot = replace(
                snapshot,
                state=WebSocketState.CLOSED,
                error=self.tr("内核已停止，连接已关闭"),
            )
            self.changed.emit(self._snapshot)
        # STOPPING still owns its native flow on the worker. The facade refuses
        # that read; once stopped, fetch the last frames/close without polling.
        # The stop marker rejects stale OPEN snapshots but permits terminal data.
        if state in (MitmRuntimeState.STOPPED, MitmRuntimeState.FAILED):
            self.refresh()
