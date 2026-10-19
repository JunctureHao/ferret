"""Saved Compose drafts keep their exact request and guard destructive switches."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit

from ferret.apps.compose.models import SavedBody, SavedFormField, SavedRequest
from ferret.apps.compose.store import ComposeRequestStore
from ferret.apps.compose.views import ComposeInterface
from ferret.core.mitm import RequestEdit
from ferret.core.settings import CONFIG, Layout

app = QApplication.instance() or QApplication([])


class _Controller(QObject):
    sending_changed = Signal(bool)
    result_ready = Signal(object)
    send_failed = Signal(str, str)

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def send(self, method, url, headers, body, *, record):
        self.calls.append((method, url, headers, body, record))
        self.sending_changed.emit(True)


class SavedComposeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.old_width = CONFIG.compose_library_width.value
        self.old_layout = CONFIG.layout.value
        CONFIG.compose_library_width.value = 240
        self.config_save = patch.object(CONFIG, "defer_save")
        self.config_save.start()
        self.store = ComposeRequestStore(
            path=Path(self.directory.name) / "requests.json"
        )
        self.controller = _Controller()
        self.page = ComposeInterface(self.controller, store=self.store)  # ty: ignore[invalid-argument-type]

    def tearDown(self) -> None:
        self.store.flush()
        self.page.deleteLater()
        self.store.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        self.config_save.stop()
        CONFIG.compose_library_width.value = self.old_width
        CONFIG.layout.value = self.old_layout
        self.directory.cleanup()

    def _save_new(self, *, name: str = "示例", folder: str = "") -> SavedRequest:
        with patch("ferret.apps.compose.views.SaveRequestDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = True
            dialog.name.return_value = name
            dialog.folder.return_value = folder
            self.assertTrue(self.page._on_save())
            dialog.deleteLater.assert_called_once()
        request = self.store.get(self.page._saved_id or "")
        assert request is not None
        return request

    def _other_request(self) -> SavedRequest:
        request = SavedRequest.create(
            name="其他", method="GET", url="https://other.test/"
        )
        self.store.upsert(request)
        self.store.flush()
        return request

    def _click_saved(self, request_id: str) -> None:
        self.page.resize(1400, 800)
        self.page.library_btn.setChecked(True)
        self.page.show()
        app.processEvents()
        panel = self.page.library_panel
        item = panel._request_items[request_id]
        panel.tree.scrollToItem(item)
        QTest.mouseClick(
            panel.tree.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            panel.tree.visualItemRect(item).center(),
        )
        app.processEvents()

    def test_clicking_a_saved_request_fills_the_request_editor(self):
        saved = SavedRequest.create(
            name="请求",
            folder="文件夹",
            method="POST",
            url="https://example.test/api?value=42",
            headers=(("Content-Type", "application/json"), ("X-Test", "saved")),
            body=SavedBody(kind="json", text='{"value":42}'),
            record=False,
        )
        self.store.upsert(saved)
        self._click_saved(saved.id)
        self.assertEqual(self.page._saved_id, saved.id)
        self.assertEqual(self.page.method_combo.currentText(), saved.method)
        self.assertEqual(self.page.url_edit.text(), saved.url)
        self.assertEqual(self.page.params_card.items(), [("value", "42")])
        self.assertEqual(self.page.headers_card.items(), list(saved.headers))
        self.assertEqual(self.page.body_editor.text.text(), saved.body.text)
        self.assertFalse(self.page.record_btn.isChecked())
        self.assertFalse(self.page._form_dirty)
        other = self._other_request()
        self._click_saved(other.id)
        self.assertEqual(self.page._saved_id, other.id)
        self.assertEqual(self.page.url_edit.text(), other.url)

    def test_clicking_current_dirty_request_can_cancel_discard_or_save(self):
        saved = SavedRequest.create(
            name="请求",
            method="POST",
            url="https://example.test/saved",
            body=SavedBody(kind="json", text='{"saved":true}'),
        )
        self.store.upsert(saved)
        self._click_saved(saved.id)
        edited_url = "https://example.test/edited"
        edited_body = '{"edited":true}'

        for choice in ("cancel", "discard", "save"):
            with self.subTest(choice=choice):
                self.page.url_edit.setText(edited_url)
                self.page.body_editor.text.code_widget.setPlainText(edited_body)
                with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
                    dialog_type.return_value.choice = choice
                    self._click_saved(saved.id)
                    dialog_type.return_value.exec.assert_called_once()
                self.assertEqual(self.page._saved_id, saved.id)
                self.assertEqual(
                    self.page.url_edit.text(),
                    saved.url if choice == "discard" else edited_url,
                )
                self.assertEqual(
                    self.page.body_editor.text.text(),
                    saved.body.text if choice == "discard" else edited_body,
                )
                self.assertEqual(self.page._form_dirty, choice == "cancel")
                persisted = self.store.get(saved.id)
                assert persisted is not None
                self.assertEqual(
                    persisted.url, edited_url if choice == "save" else saved.url
                )
                self.assertEqual(
                    persisted.body.text,
                    edited_body if choice == "save" else saved.body.text,
                )

    def test_library_wraps_the_entire_compose_panel_and_keeps_inner_orientation(self):
        self.page.resize(1400, 800)
        self.page.show()
        app.processEvents()
        self.assertTrue(self.page.library_panel.isHidden())
        outer = self.page.library_splitter
        self.assertIs(outer.widget(0), self.page.library_panel)
        self.assertIs(outer.widget(1), self.page._content)
        self.assertTrue(self.page._content.isAncestorOf(self.page.method_combo))
        self.assertTrue(self.page._content.isAncestorOf(self.page.url_edit))
        self.assertTrue(self.page._content.isAncestorOf(self.page.send_btn))
        self.assertIs(self.page.splitter.parentWidget(), self.page._content)
        CONFIG.layout.value = Layout.VERTICAL
        self.assertEqual(self.page.splitter.orientation(), Qt.Orientation.Vertical)
        self.assertEqual(outer.orientation(), Qt.Orientation.Horizontal)
        self.page.library_btn.setChecked(True)
        app.processEvents()
        self.assertFalse(self.page.library_panel.isHidden())
        self.assertEqual(outer.sizes()[0], 240)
        outer.setSizes([290, 1000])
        self.page._on_library_resized(290, 1)
        self.assertEqual(CONFIG.compose_library_width.value, outer.sizes()[0])
        self.page.library_btn.setChecked(False)
        self.page.library_btn.setChecked(True)
        self.assertEqual(outer.sizes()[0], CONFIG.compose_library_width.value)
        fresh_page = ComposeInterface(self.controller, store=self.store)  # ty: ignore[invalid-argument-type]
        try:
            self.assertFalse(fresh_page.library_btn.isChecked())
            self.assertTrue(fresh_page.library_panel.isHidden())
        finally:
            fresh_page.deleteLater()

    def test_save_reload_send_preserves_signed_url_duplicate_headers_and_body(self):
        url = "https://example.test:443/api?q=a%20b&flag&k=1&k=2&x=%2f#tail"
        headers = [("X-Repeat", " one "), ("X-Repeat", "two"), ("X-Bytes", "\udcff")]
        content = b"\x00\xffbinary"
        self.page.prefill(
            RequestEdit(method="PROPFIND", url=url, headers=headers, content=content)
        )
        self.page.record_btn.setChecked(False)
        saved = self._save_new(folder="签名")
        self.assertEqual(saved.url, url)
        self.page._on_create_draft()
        self.page._on_open_saved(saved.id)
        self.assertEqual(self.page.url_edit.text(), url)
        self.assertEqual(
            self.page.params_card.items()[:2], [("q", "a b"), ("flag", "")]
        )
        self.page._on_send()
        self.assertEqual(
            self.controller.calls, [("PROPFIND", url, headers, content, False)]
        )
        reloaded = ComposeRequestStore(path=self.store.path)
        self.assertEqual(reloaded.get(saved.id), saved)
        reloaded.deleteLater()

    def test_unfinished_url_can_be_saved_and_send_reports_validation(self):
        self.page.url_edit.setText("https://[")
        saved = self._save_new()
        self.assertEqual(saved.url, "https://[")
        with patch("ferret.apps.compose.views.show_error") as error:
            self.page._on_send()
            error.assert_called_once()
        self.assertEqual(self.controller.calls, [])

    def test_rehighlight_without_text_edit_does_not_dirty_loaded_request(self):
        url = "https://example.test/?q=a%20b&flag"
        saved = SavedRequest.create(
            name="saved",
            method="POST",
            url=url,
            headers=(("Content-Type", "application/json"),),
            body=SavedBody(kind="json", text='{"value": 1}'),
        )
        self.page._load_saved(saved)
        self.page.body_editor.text.changed.emit()
        self.page.headers_card.changed.emit()
        self.page.params_card.changed.emit()
        self.page.show()
        app.processEvents()
        self.assertFalse(self.page._form_dirty)
        self.assertEqual(self.page._collect_url(), url)
        self.page.body_editor.text.code_widget.insertPlainText(" ")
        self.assertTrue(self.page._form_dirty)

    def test_save_update_save_as_and_ctrl_s_have_distinct_id_semantics(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        self.page.url_edit.setText("https://example.test/b")
        self.assertTrue(self.page._form_dirty)
        self.page.show()
        self.page.activateWindow()
        self.page.url_edit.setFocus()
        app.processEvents()
        QTest.keyClick(
            self.page.url_edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier
        )
        # Offscreen Qt retains synthetic modifiers until an explicit release;
        # otherwise later table selection tests behave as if Ctrl were held.
        QTest.keyRelease(self.page.url_edit, Qt.Key.Key_Control)
        app.processEvents()
        self.assertFalse(self.page._form_dirty)
        self.assertEqual(self.page._saved_id, saved.id)
        self.assertEqual(len(self.store.requests()), 1)
        with patch("ferret.apps.compose.views.SaveRequestDialog") as dialog_type:
            dialog = dialog_type.return_value
            dialog.exec.return_value = True
            dialog.name.return_value = "副本"
            dialog.folder.return_value = ""
            self.assertTrue(self.page._on_save_as())
        self.assertNotEqual(self.page._saved_id, saved.id)
        self.assertEqual(len(self.store.requests()), 2)

    def test_failed_save_stays_dirty_and_retry_uses_the_same_id(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        self.page.url_edit.setText("https://example.test/edited")
        with (
            patch.object(self.store, "_write", side_effect=OSError("disk full")),
            patch("ferret.apps.compose.views.show_error") as error,
        ):
            self.assertFalse(self.page._on_save())
            error.assert_called_once()
        self.assertTrue(self.page._form_dirty)
        self.assertEqual(self.page._saved_id, saved.id)
        # A successful delayed retry alone does not erase the explicit-save obligation.
        self.assertTrue(self.store.flush())
        self.assertTrue(self.page._form_dirty)
        self.assertTrue(self.page._on_save())
        self.assertFalse(self.page._form_dirty)
        self.assertEqual(len(self.store.requests()), 1)

    def test_save_commits_active_parameter_cell_before_reading_the_url(self):
        self.page.prefill(
            RequestEdit(
                method="GET", url="https://example.test/?q=old", headers=[], content=b""
            )
        )
        saved = self._save_new()
        self.page.params_card._show_table_page()
        self.page.show()
        app.processEvents()
        table = self.page.params_card.table._table_widget
        cell = table.item(0, 1)
        assert cell is not None
        table.editItem(cell)
        editor = table.viewport().findChild(QLineEdit)
        assert editor is not None
        editor.setText("new value")
        self.assertEqual(cell.text(), "old")
        self.assertTrue(self.page._on_save())
        updated = self.store.get(saved.id)
        assert updated is not None
        self.assertEqual(updated.url, "https://example.test/?q=new+value")
        self.assertEqual(updated.params, (("q", "new value"),))

    def test_switch_cancel_keeps_dirty_form_and_selection_then_save_continues(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        other = self._other_request()
        self.page.url_edit.setText("https://example.test/edited")
        with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
            dialog_type.return_value.choice = "cancel"
            self.page._on_open_saved(other.id)
            dialog_type.return_value.deleteLater.assert_called_once()
        self.assertEqual(self.page._saved_id, saved.id)
        self.assertEqual(self.page.url_edit.text(), "https://example.test/edited")
        self.assertEqual(
            self.page.library_panel.tree.currentItem().data(
                0, Qt.ItemDataRole.UserRole
            ),
            ("request", saved.id),
        )
        with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
            dialog_type.return_value.choice = "save"
            self.page._on_open_saved(other.id)
        self.assertEqual(self.page._saved_id, other.id)
        updated = self.store.get(saved.id)
        assert updated is not None
        self.assertEqual(updated.url, "https://example.test/edited")

    def test_failed_save_blocks_switch_and_discard_can_open_new_draft(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        other = self._other_request()
        self.page.url_edit.setText("https://example.test/edited")
        with (
            patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type,
            patch.object(self.store, "_write", side_effect=OSError("disk full")),
            patch("ferret.apps.compose.views.show_error"),
        ):
            dialog_type.return_value.choice = "save"
            self.page._on_open_saved(other.id)
        self.assertEqual(self.page._saved_id, saved.id)
        self.assertTrue(self.page._form_dirty)
        with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
            dialog_type.return_value.choice = "discard"
            self.page._on_create_draft()
        self.assertIsNone(self.page._saved_id)
        self.assertEqual(self.page.url_edit.text(), "")
        self.assertFalse(self.page._form_dirty)

    def test_prefill_and_curl_confirm_dirty_replacement_and_report_only_success(self):
        self.page.url_edit.setText("https://example.test/edited")
        edit = RequestEdit(
            method="GET", url="https://other.test/", headers=[], content=b""
        )
        with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
            dialog_type.return_value.choice = "cancel"
            self.assertFalse(self.page.prefill(edit))
            QApplication.clipboard().setText("curl https://other.test/")
            with patch("ferret.apps.compose.views.show_success") as success:
                self.page._on_paste_curl()
                success.assert_not_called()
        self.assertEqual(self.page.url_edit.text(), "https://example.test/edited")
        with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
            dialog_type.return_value.choice = "discard"
            self.assertTrue(self.page.prefill(edit))
        self.assertIsNone(self.page._saved_id)

    def test_inflight_send_and_file_preparation_block_all_request_switches(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        other = self._other_request()
        self.page.body_editor.load_saved(
            SavedBody(
                kind="file", file=SavedFormField(kind="file", file_path="not-read.bin")
            )
        )
        with patch("ferret.apps.compose.views.QThreadPool.globalInstance") as pool:
            self.page._on_send()
            pool.return_value.start.assert_called_once()
        self.assertTrue(self.page._sending)
        task = self.page._prepare_task
        assert task is not None
        for before_response in (True, False):
            if not before_response:
                task.signals.succeeded.emit(b"file contents")
                task.signals.finished.emit()
            with patch("ferret.apps.compose.views._UnsavedDialog") as dialog_type:
                self.page._on_open_saved(other.id)
                self.page._on_create_draft()
                self.assertFalse(
                    self.page.prefill(
                        RequestEdit(
                            method="GET", url=other.url, headers=[], content=b""
                        )
                    )
                )
                dialog_type.assert_not_called()
            self.assertEqual(self.page._saved_id, saved.id)
            self.assertFalse(self.page.library_panel.isEnabled())
            self.assertFalse(self.page.paste_curl_btn.isEnabled())
            self.assertFalse(self.page.save_btn.isEnabled())
        self.assertEqual(self.controller.calls[-1][3], b"file contents")
        self.controller.sending_changed.emit(False)
        self.assertTrue(self.page.library_panel.isEnabled())

    def test_rename_move_and_delete_update_identity_without_overwriting_edits(self):
        self.page.url_edit.setText("https://example.test/a")
        saved = self._save_new()
        self.page.url_edit.setText("https://example.test/edited")
        self.store.upsert(saved.touched(name="新名称", folder="分组"))
        self.assertTrue(self.page._form_dirty)
        self.assertEqual(self.page.url_edit.text(), "https://example.test/edited")
        self.assertIn("新名称", self.page.save_btn.toolTip())
        self.assertIn("新名称 *", self.page.library_panel.tree.currentItem().text(0))
        self.assertTrue(self.page._on_save())
        updated = self.store.get(saved.id)
        assert updated is not None
        self.assertEqual(updated.folder, "分组")
        self.store.remove(saved.id)
        self.assertIsNone(self.page._saved_id)
        self.assertTrue(self.page._form_dirty)
        self.assertEqual(self.page.url_edit.text(), "https://example.test/edited")
