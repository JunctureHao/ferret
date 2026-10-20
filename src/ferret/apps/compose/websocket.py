"""Compose WebSocket drafts and incremental message history."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, FluentIcon, LineEdit, PrimaryPushButton

from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.flow.messages import MessagesPane
from ferret.apps.compose.models import SavedWebSocket
from ferret.core.mitm import WebSocketSnapshot, WebSocketState, WsClose


class WebSocketEditor(QWidget):
    """Text/JSON share exact text; binary has its own hex draft, never HTTP headers."""

    changed = Signal()
    sendRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._loading = False
        self._kind = "text"
        self._text = ""
        self._binary_hex = ""
        self._display_text = ""
        self._original_text = ""
        self.subprotocols_edit = LineEdit(self)
        self.subprotocols_edit.setPlaceholderText(self.tr("子协议，以逗号分隔（可选）"))
        self.subprotocols_edit.setAccessibleName(self.tr("WebSocket 子协议"))
        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems(
            [self.tr("文本"), "JSON", self.tr("二进制（十六进制）")]
        )
        self.send_btn = PrimaryPushButton(FluentIcon.SEND, self.tr("发送消息"), self)
        self.send_btn.setEnabled(False)
        self.text = ToolPlainTextEdit(self)
        self.text.set_read_only(False)
        self.text.set_text("", lang=Language.TEXT)
        self.text.code_widget.setAccessibleName(self.tr("待发送的 WebSocket 消息"))
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.kind_combo)
        toolbar.addStretch(1)
        toolbar.addWidget(self.send_btn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(self.subprotocols_edit)
        layout.addLayout(toolbar)
        layout.addWidget(self.text, 1)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.subprotocols_edit.textChanged.connect(self._on_config_changed)
        self.text.changed.connect(self._on_text_changed)
        self.send_btn.clicked.connect(self.sendRequested)

    @property
    def kind(self) -> str:
        return self._kind

    def _document(self) -> str:
        # Qt normalises CRLF in setPlainText. Preserve saved UTF-8 text until an
        # actual edit; JSON highlighting and switching formats are not edits.
        text = self.text.text()
        return self._original_text if text == self._display_text else text

    def _capture(self) -> None:
        if self._kind == "binary":
            self._binary_hex = self._document()
        else:
            self._text = self._document()

    def _show_document(self) -> None:
        value = self._binary_hex if self._kind == "binary" else self._text
        self.text.set_text(
            value, lang=Language.JSON if self._kind == "json" else Language.TEXT
        )
        self._original_text = value
        self._display_text = self.text.text()

    @Slot(int)
    def _on_kind_changed(self, index: int) -> None:
        if self._loading:
            return
        self._capture()
        self._kind = ("text", "json", "binary")[index]
        self._show_document()
        self.changed.emit()

    @Slot()
    def _on_config_changed(self) -> None:
        if not self._loading:
            self.changed.emit()

    @Slot()
    def _on_text_changed(self) -> None:
        if not self._loading and self.text.text() != self._display_text:
            self._original_text = self._display_text = self.text.text()
            self._capture()
            self.changed.emit()

    def subprotocols(self) -> tuple[str, ...]:
        value = self.subprotocols_edit.text()
        return tuple(part.strip() for part in value.split(",")) if value.strip() else ()

    def saved(self) -> SavedWebSocket:
        self._capture()
        return SavedWebSocket(
            subprotocols=self.subprotocols(),
            kind=self._kind,
            text=self._text,
            binary_hex=self._binary_hex,
        )

    def load(self, value: SavedWebSocket) -> None:
        self._loading = True
        try:
            self._kind = value.kind
            self._text = value.text
            self._binary_hex = value.binary_hex
            self.subprotocols_edit.setText(", ".join(value.subprotocols))
            self.kind_combo.setCurrentIndex(
                ("text", "json", "binary").index(value.kind)
            )
            self._show_document()
        finally:
            self._loading = False

    def payload(self) -> tuple[bytes, bool]:
        value = self.saved()
        if value.kind != "binary":
            return value.text.encode("utf-8"), True
        # A compact pair sequence, with optional ASCII whitespace between bytes.
        # Reject odd nibbles, prefixes and other separators instead of repairing.
        if not re.fullmatch(
            r"(?:[ \t\r\n]*[0-9a-fA-F]{2})*[ \t\r\n]*", value.binary_hex
        ):
            raise ValueError(
                self.tr("十六进制消息必须由完整的字节对组成，可用空格或换行分隔。")
            )
        return bytes.fromhex(value.binary_hex), False

    def set_connection_state(self, state: WebSocketState | None) -> None:
        active = state in (
            WebSocketState.CONNECTING,
            WebSocketState.OPEN,
            WebSocketState.CLOSING,
        )
        self.subprotocols_edit.setDisabled(active)
        self.send_btn.setEnabled(state == WebSocketState.OPEN)


class WebSocketHistory(QWidget):
    """Synchronise the retained snapshot window without losing local clear watermarks."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._session_id = ""
        self._seen = 0
        self._cleared = 0
        self._latest: WebSocketSnapshot | None = None
        self.status = BodyLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label = BodyLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.messages = MessagesPane(self, export_available=False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(self.status)
        layout.addWidget(self.error_label)
        layout.addWidget(self.messages, 1)
        self.messages.clearRequested.connect(self._on_clear)
        self.reset()

    def reset(self) -> None:
        self._session_id = ""
        self._seen = self._cleared = 0
        self._latest = None
        self.messages.show_websocket([], WsClose())
        self.status.setText(self.tr("尚未连接"))
        self.error_label.clear()
        self.error_label.hide()

    @Slot()
    def _on_clear(self) -> None:
        self._cleared = max(
            self._seen, self._latest.message_count if self._latest else 0
        )

    def set_snapshot(self, snapshot: WebSocketSnapshot) -> None:
        if snapshot.session_id != self._session_id:
            self.reset()
            self._session_id = snapshot.session_id
        self._latest = snapshot
        first = snapshot.frames[0].index if snapshot.frames else snapshot.message_count
        self.messages.discard_before(max(first, self._cleared))
        for frame in snapshot.frames:
            if frame.index >= max(self._seen, self._cleared):
                self.messages.append_frame(frame)
        self._seen = max(self._seen, snapshot.message_count)
        self.messages.set_count(snapshot.message_count)
        self.messages.set_close(snapshot.close)
        if snapshot.state == WebSocketState.CONNECTING:
            text = self.tr("正在连接…")
        elif snapshot.state == WebSocketState.OPEN:
            text = (
                self.tr("已连接 · 子协议：{}").format(snapshot.subprotocol)
                if snapshot.subprotocol
                else self.tr("已连接")
            )
        elif snapshot.state == WebSocketState.CLOSING:
            text = self.tr("正在断开…")
        elif snapshot.state == WebSocketState.FAILED:
            text = self.tr("连接失败")
        else:
            text = self.tr("连接已关闭")
        self.status.setText(text)
        self.error_label.setText(snapshot.error)
        self.error_label.setVisible(bool(snapshot.error))
