"""WebSocket drafts, single-connection guards and retained snapshot rendering."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QItemSelectionModel
from PySide6.QtWidgets import QApplication

from ferret.apps.compose.models import SavedBody, SavedRequest, SavedWebSocket
from ferret.apps.compose.store import ComposeRequestStore
from ferret.apps.compose.views import ComposeInterface
from ferret.apps.compose.websocket import WebSocketEditor, WebSocketHistory
from ferret.apps.compose.websocket_controller import ComposeWebSocketController
from ferret.core.mitm import (
    MitmRuntimeState,
    RequestEdit,
    WebSocketSnapshot,
    WebSocketSpec,
    WebSocketState,
    WsClose,
    WsFrame,
)
from tests.apps.compose._websocket import WebSocketFacade
from tests.apps.compose.test_views import FakeController

app = QApplication.instance() or QApplication([])


def frame(index: int) -> WsFrame:
    return WsFrame(index, False, 1, f"message {index}".encode(), 1.0, False, False)


class WebSocketControllerTests(unittest.TestCase):
    def setUp(self):
        self.facade = WebSocketFacade()
        self.controller = ComposeWebSocketController(mitm=self.facade)  # ty: ignore[invalid-argument-type]
        self.addCleanup(self.controller.deleteLater)

    def test_single_session_and_separate_send_close_lifecycle(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.controller.open_connection(WebSocketSpec("ws://other.test/"))
        self.assertEqual(len(self.facade.specs), 1)
        self.controller.send(b"before open", is_text=True)
        self.assertFalse(self.facade.sent)
        self.facade.update(state=WebSocketState.OPEN)
        self.controller.send(b"", is_text=False)
        self.controller.send(b" hello\r\n", is_text=True)
        self.assertEqual(
            self.facade.sent,
            [("session-1", b"", False), ("session-1", b" hello\r\n", True)],
        )
        self.controller.close()
        self.assertEqual(self.facade.closed, ["session-1"])
        self.assertTrue(self.controller.active)
        self.facade.update(state=WebSocketState.CLOSED)
        self.assertFalse(self.controller.active)

    def test_runtime_stop_is_terminal_even_with_late_open_notification(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.runtime.state_changed.emit(MitmRuntimeState.STOPPING)
        self.facade.update(state=WebSocketState.OPEN)
        self.controller.refresh()
        self.assertFalse(self.controller.active)
        assert self.controller.snapshot is not None
        self.assertEqual(self.controller.snapshot.state, WebSocketState.CLOSED)
        self.assertIn("内核已停止", self.controller.snapshot.error)

    def test_stopped_runtime_reads_final_frames_and_close_after_fallback(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.update(state=WebSocketState.OPEN)
        self.facade.runtime.state_changed.emit(MitmRuntimeState.STOPPING)
        final = replace(
            self.facade.snapshots["session-1"],
            state=WebSocketState.CLOSED,
            frames=(frame(0),),
            message_count=1,
            close=WsClose(close_code=1001, close_reason="shutdown"),
        )
        self.facade.snapshots["session-1"] = final
        self.facade.runtime.state_changed.emit(MitmRuntimeState.STOPPED)
        self.assertEqual(self.controller.snapshot, final)
        self.assertFalse(self.controller.active)
        self.facade.update(state=WebSocketState.OPEN)
        self.assertEqual(self.controller.snapshot, final)

    def test_terminal_notification_can_complete_stop_fallback(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.runtime.state_changed.emit(MitmRuntimeState.STOPPING)
        final = self.facade.update(
            state=WebSocketState.FAILED,
            error="last error",
            close=WsClose(close_code=1006),
        )
        self.assertEqual(self.controller.snapshot, final)

    def test_send_rejection_preserves_open_connection_and_allows_retry(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.update(state=WebSocketState.OPEN)
        failures: list[tuple[str, str]] = []
        self.controller.failed.connect(
            lambda title, error: failures.append((title, error))
        )
        with patch.object(
            self.facade, "send_websocket", side_effect=ValueError("queue full")
        ):
            self.controller.send(b"retry", is_text=True)
        self.assertEqual(failures, [("消息发送失败", "queue full")])
        self.assertTrue(self.controller.active)
        self.controller.send(b"retry", is_text=True)
        self.assertEqual(self.facade.sent, [("session-1", b"retry", True)])

    def test_close_exception_refreshes_the_actual_terminal_state(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.update(state=WebSocketState.OPEN)

        def failed_close(session_id: str):
            self.facade.snapshots[session_id] = replace(
                self.facade.snapshots[session_id], state=WebSocketState.CLOSED
            )
            raise OSError("transport closed")

        with patch.object(self.facade, "close_websocket", side_effect=failed_close):
            self.controller.close()
        self.assertFalse(self.controller.active)
        assert self.controller.snapshot is not None
        self.assertEqual(self.controller.snapshot.state, WebSocketState.CLOSED)

    def test_old_session_notification_does_not_replace_new_connection(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.update(state=WebSocketState.CLOSED)
        self.controller.open_connection(WebSocketSpec("ws://other.test/"))
        self.facade.update("session-1", state=WebSocketState.FAILED, error="late")
        assert self.controller.snapshot is not None
        self.assertEqual(self.controller.snapshot.session_id, "session-2")
        self.assertEqual(self.controller.snapshot.state, WebSocketState.CONNECTING)

    def test_running_capture_stop_does_not_close_websocket(self):
        self.controller.open_connection(WebSocketSpec("ws://example.test/"))
        self.facade.runtime.state_changed.emit(MitmRuntimeState.RUNNING)
        self.assertTrue(self.controller.active)


class WebSocketEditorTests(unittest.TestCase):
    def setUp(self):
        self.editor = WebSocketEditor()
        self.addCleanup(self.editor.deleteLater)

    def test_text_and_json_preserve_bytes_without_reformatting(self):
        value = ' { "a":  1 }\r\n'
        self.editor.load(SavedWebSocket(kind="json", text=value))
        self.assertEqual(self.editor.payload(), (value.encode(), True))
        self.editor.kind_combo.setCurrentIndex(0)
        self.assertEqual(self.editor.payload(), (value.encode(), True))
        self.editor.kind_combo.setCurrentIndex(1)
        self.assertEqual(self.editor.payload(), (value.encode(), True))

    def test_hex_requires_complete_byte_pairs_and_empty_is_valid(self):
        for value in ("f", "0x01", "f f", "01:02", "zz", "01\u00a002"):
            with self.subTest(value=value):
                self.editor.load(SavedWebSocket(kind="binary", binary_hex=value))
                with self.assertRaises(ValueError):
                    self.editor.payload()
        self.editor.load(SavedWebSocket(kind="binary", binary_hex="00 ff\nAB10"))
        self.assertEqual(self.editor.payload(), (b"\x00\xff\xab\x10", False))
        for kind in ("text", "json", "binary"):
            self.editor.load(SavedWebSocket(kind=kind))
            self.assertEqual(self.editor.payload(), (b"", kind != "binary"))

    def test_format_changes_keep_both_message_drafts(self):
        self.editor.load(SavedWebSocket(text="original", binary_hex="ff 00"))
        self.editor.kind_combo.setCurrentIndex(2)
        self.assertEqual(self.editor.text.text(), "ff 00")
        self.editor.text.code_widget.setPlainText("00 42")
        self.editor.kind_combo.setCurrentIndex(1)
        self.assertEqual(self.editor.text.text(), "original")
        self.assertEqual(
            self.editor.saved(),
            SavedWebSocket(kind="json", text="original", binary_hex="00 42"),
        )


class WebSocketHistoryTests(unittest.TestCase):
    def setUp(self):
        self.history = WebSocketHistory()
        self.addCleanup(self.history.deleteLater)

    def test_repeated_snapshots_clear_watermark_and_retained_window(self):
        initial = WebSocketSnapshot(
            "one",
            state=WebSocketState.OPEN,
            frames=tuple(frame(i) for i in range(3)),
            message_count=3,
        )
        self.history.set_snapshot(initial)
        self.history.set_snapshot(initial)
        self.assertEqual(
            [row.key for row in self.history.messages.message_list.rows()], [0, 1, 2]
        )
        self.history.messages.clear_btn.click()
        self.history.set_snapshot(initial)
        self.assertEqual(self.history.messages.message_list.message_count(), 0)
        later = replace(
            initial, frames=tuple(frame(i) for i in range(2, 5)), message_count=5
        )
        self.history.set_snapshot(later)
        self.assertEqual(
            [row.key for row in self.history.messages.message_list.rows()], [3, 4]
        )
        self.assertEqual(self.history.messages.count, 5)
        self.history.set_snapshot(replace(later, frames=(frame(4),), message_count=5))
        self.assertEqual(
            [row.key for row in self.history.messages.message_list.rows()], [4]
        )

    def test_filter_survives_incremental_snapshots_and_close_is_visible(self):
        initial = WebSocketSnapshot("one", frames=(frame(0),), message_count=1)
        self.history.set_snapshot(initial)
        self.history.messages.filter_input.setText("message 1")
        self.history.set_snapshot(
            replace(
                initial,
                frames=(frame(0), frame(1)),
                message_count=2,
                state=WebSocketState.CLOSED,
                close=WsClose(close_code=1000, close_reason="done"),
            )
        )
        self.assertEqual(self.history.messages.message_list.filter_text, "message 1")
        self.assertEqual(self.history.messages.message_list.visible_count(), 1)
        self.assertIn("1000", self.history.messages.close_label.text())
        self.assertEqual(self.history.status.text(), "连接已关闭")

    def test_incremental_refresh_preserves_selection_and_scroll(self):
        initial = WebSocketSnapshot(
            "one", frames=tuple(frame(i) for i in range(100)), message_count=100
        )
        self.history.resize(500, 450)
        self.history.show()
        self.history.set_snapshot(initial)
        app.processEvents()
        view = self.history.messages.message_list
        model = view.model()
        selection = view.selectionModel()
        assert model is not None and selection is not None
        selection.setCurrentIndex(
            model.index(25, 0), QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        view.verticalScrollBar().setValue(200)
        self.history.set_snapshot(
            replace(initial, frames=initial.frames + (frame(100),), message_count=101)
        )
        selected = view.selected_row()
        assert selected is not None
        self.assertEqual(selected.key, 25)
        self.assertEqual(view.verticalScrollBar().value(), 200)
        self.history.set_snapshot(
            replace(
                initial,
                frames=tuple(frame(i) for i in range(20, 102)),
                message_count=102,
            )
        )
        selected = view.selected_row()
        assert selected is not None
        self.assertEqual(selected.key, 25)


class WebSocketViewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ComposeRequestStore(
            path=Path(self.directory.name) / "requests.json"
        )
        self.controller = FakeController()
        self.facade = self.controller.websocket_facade
        self.page = ComposeInterface(self.controller, store=self.store)  # ty: ignore[invalid-argument-type]
        self.page.resize(1200, 700)

    def tearDown(self):
        self.store.flush()
        self.page.deleteLater()
        self.controller.deleteLater()
        self.store.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        self.directory.cleanup()

    def _prefill(self):
        self.assertTrue(
            self.page.prefill(
                WebSocketSpec(
                    "wss://example.test/socket?q=a%20b&flag",
                    (("X-Test", "one"),),
                    ("chat",),
                    record=False,
                )
            )
        )

    def _connect(self):
        self._prefill()
        self.page._on_send()

    def test_connect_freezes_configuration_but_keeps_save_and_message_draft(self):
        self._connect()
        self.assertFalse(self.page._sending)
        self.assertFalse(self.page.url_edit.isEnabled())
        self.assertFalse(self.page.headers_card.isEnabled())
        self.assertFalse(self.page.websocket_editor.subprotocols_edit.isEnabled())
        self.assertTrue(self.page.save_btn.isEnabled())
        self.assertTrue(self.page.websocket_editor.text.isEnabled())
        self.assertFalse(self.page.websocket_editor.send_btn.isEnabled())
        self.assertEqual(
            self.facade.specs[0].url, "wss://example.test/socket?q=a%20b&flag"
        )
        self.assertFalse(self.facade.specs[0].record)
        self.facade.update(state=WebSocketState.OPEN)
        self.assertTrue(self.page.websocket_editor.send_btn.isEnabled())
        self.page.websocket_editor.text.code_widget.setPlainText("message")
        self.page.websocket_editor.send_btn.click()
        self.assertEqual(self.facade.sent, [("session-1", b"message", True)])
        self.assertEqual(self.page.websocket_editor.text.text(), "message")

    def test_active_connection_guards_protocol_saved_request_prefill_and_curl(self):
        self._connect()
        original = self.page.url_edit.text()
        saved = SavedRequest.create(
            name="other", method="GET", url="https://other.test/"
        )
        self.store.upsert(saved)
        with patch("ferret.apps.compose.views.show_error") as error:
            self.page.protocol_combo.setCurrentIndex(0)
            self.assertFalse(
                self.page.prefill(
                    RequestEdit(
                        method="GET", url="https://other.test/", headers=(), content=b""
                    )
                )
            )
            self.page._on_open_saved(saved.id)
            self.page._on_paste_curl()
            self.assertEqual(error.call_count, 4)
        self.assertEqual(self.page._protocol, "websocket")
        self.assertEqual(self.page.url_edit.text(), original)

    def test_websocket_save_roundtrip_includes_messages_while_connected(self):
        self._connect()
        self.page.websocket_editor.load(
            SavedWebSocket(("chat",), "json", '{"raw": 1}\r\n', "00 ff")
        )
        with patch("ferret.apps.compose.views.SaveRequestDialog") as dialog:
            dialog.return_value.exec.return_value = True
            dialog.return_value.name.return_value = "socket"
            dialog.return_value.folder.return_value = ""
            self.assertTrue(self.page._on_save())
        saved = self.store.get(self.page._saved_id or "")
        assert saved is not None
        self.assertEqual(saved.protocol, "websocket")
        self.assertEqual(
            saved.websocket,
            SavedWebSocket(("chat",), "json", '{"raw": 1}\r\n', "00 ff"),
        )
        reopened = ComposeRequestStore(path=Path(self.directory.name) / "requests.json")
        reloaded = reopened.get(saved.id)
        self.assertEqual(reloaded, saved)
        assert reloaded is not None
        reopened.deleteLater()
        self.facade.update(state=WebSocketState.CLOSED)
        self.page._load_saved(reloaded)
        self.assertEqual(
            self.page.websocket_editor.payload(), (b'{"raw": 1}\r\n', True)
        )
        self.assertFalse(self.page._form_dirty)

    def test_switch_back_to_http_preserves_saved_body_and_ws_draft(self):
        self.page._load_saved(
            SavedRequest(
                method="POST",
                url="https://example.test/?signature=a%20b&flag",
                headers=(("Content-Type", "application/custom"),),
                body=SavedBody(kind="json", text='{ "original": true }\r\n'),
            )
        )
        self.page.protocol_combo.setCurrentIndex(1)
        self.page.websocket_editor.text.code_widget.setPlainText("socket draft")
        self.page._on_send()
        saved = self.page._snapshot_request("same draft", "")
        self.assertEqual(saved.protocol, "websocket")
        self.assertEqual(saved.method, "POST")
        self.assertEqual(saved.body.text, '{ "original": true }\r\n')
        self.facade.update(state=WebSocketState.CLOSED)
        self.page.protocol_combo.setCurrentIndex(0)
        self.page._on_send()
        self.assertEqual(
            self.controller.calls[-1],
            (
                "send",
                "POST",
                "https://example.test/?signature=a%20b&flag",
                (("Content-Type", "application/custom"),),
                b'{ "original": true }\r\n',
                True,
            ),
        )
        self.assertEqual(self.page.websocket_editor.saved().text, "socket draft")

    def test_unrecorded_truncation_does_not_suggest_export_after_record_toggle(self):
        self._connect()  # record=False
        self.page.show()
        app.processEvents()
        clipped = replace(frame(0), content=b"short", original_size=100)
        self.facade.update(
            state=WebSocketState.OPEN, frames=(clipped,), message_count=1
        )
        pane = self.page.websocket_history.messages
        pane.message_list.messageActivated.emit(pane.message_list.rows()[0])
        self.assertEqual(
            pane.detail_edit.notice.text(), "仅显示前 5 字节，共 100 字节。"
        )
        self.facade.update(state=WebSocketState.CLOSED)
        self.page.record_btn.setChecked(True)
        self.page._on_websocket_changed(self.controller.websocket.snapshot)
        self.assertNotIn("导出", pane.detail_edit.notice.text())

    def test_hidden_page_catches_up_and_failed_handshake_is_inspectable(self):
        self._connect()
        self.page.show()
        app.processEvents()
        self.facade.update(
            state=WebSocketState.OPEN, frames=(frame(0),), message_count=1
        )
        self.assertEqual(self.page.websocket_history.messages.count, 1)
        self.page.hide()
        self.facade.update(frames=(frame(0), frame(1)), message_count=2)
        self.assertEqual(self.page.websocket_history.messages.count, 1)
        self.page.show()
        app.processEvents()
        self.assertEqual(self.page.websocket_history.messages.count, 2)
        detail = {
            "Status Code": 403,
            "Reason": "Forbidden",
            "Response Headers": {"content-type": "text/plain"},
            "Response Body Text": "denied",
        }
        self.facade.update(state=WebSocketState.FAILED, error="403", detail=detail)
        self.assertIn("403", self.page.websocket_history.error_label.text())
        self.assertEqual(self.page._ws_detail, detail)
        self.assertTrue(self.page.url_edit.isEnabled())

    def test_switch_and_new_request_reset_history_without_touching_http_body_headers(
        self,
    ):
        self.page._load_saved(
            SavedRequest(
                method="POST",
                url="https://example.test/",
                headers=(("Content-Type", "application/custom"),),
                body=SavedBody(kind="json", text="{}"),
            )
        )
        self.page.protocol_combo.setCurrentIndex(1)
        self.page.websocket_editor.kind_combo.setCurrentIndex(1)
        self.assertEqual(
            self.page.headers_card.items(), [("Content-Type", "application/custom")]
        )
        self.assertTrue(self.page.method_combo.isHidden())
        self.assertEqual(self.page.url_edit.text(), "wss://example.test/")
        self.page._form_dirty = False
        self.page._on_create_draft()
        self.assertEqual(self.page._protocol, "http")
        self.assertEqual(self.page.websocket_editor.saved(), SavedWebSocket())
        self.assertEqual(self.page.websocket_history.messages.count, 0)
