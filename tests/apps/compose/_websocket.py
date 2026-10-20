"""Detached WebSocket facade double for Compose widgets and lifecycle tests."""

from __future__ import annotations

import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal

from ferret.core.mitm import WebSocketSnapshot, WebSocketSpec, WebSocketState


class WebSocketRuntime(QObject):
    compose_result = Signal(object)
    compose_websocket_changed = Signal(str)
    state_changed = Signal(object)


class WebSocketFacade:
    def __init__(self):
        self.runtime = WebSocketRuntime()
        self.snapshots: dict[str, WebSocketSnapshot] = {}
        self.specs: list[WebSocketSpec] = []
        self.sent: list[tuple[str, bytes, bool]] = []
        self.closed: list[str] = []

    def connect_websocket(self, spec: WebSocketSpec) -> str:
        self.specs.append(spec)
        session_id = f"session-{len(self.specs)}"
        self.snapshots[session_id] = WebSocketSnapshot(session_id=session_id)
        return session_id

    def websocket_snapshot(self, session_id: str) -> WebSocketSnapshot | None:
        return self.snapshots.get(session_id)

    def send_websocket(self, session_id: str, content: bytes, *, is_text: bool):
        self.sent.append((session_id, content, is_text))

    def close_websocket(self, session_id: str):
        self.closed.append(session_id)
        self.snapshots[session_id] = replace(
            self.snapshots[session_id], state=WebSocketState.CLOSING
        )

    def update(self, session_id: str = "session-1", **changes) -> WebSocketSnapshot:
        snapshot = replace(self.snapshots[session_id], **changes)
        self.snapshots[session_id] = snapshot
        self.runtime.compose_websocket_changed.emit(session_id)
        return snapshot
