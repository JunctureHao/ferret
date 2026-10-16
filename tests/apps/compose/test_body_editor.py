"""Compose sends typed drafts without changing unedited requests or UI snapshots."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QLineEdit
from qfluentwidgets import CheckBox

from ferret.apps.common.edit import Language
from ferret.apps.compose.body_editor import BodyKind
from ferret.apps.compose.curl_import import parse_curl
from ferret.core.mitm import (
    FormField,
    RequestEdit,
    encode_multipart,
    multipart_boundary,
    parse_multipart,
    read_body_file,
)
from tests.apps.compose.test_views import ComposeInterfaceTestCase, app
from tests.core.mitm._qt import wait_until


class ComposeBodyTestCase(ComposeInterfaceTestCase):
    def setUp(self) -> None:
        super().setUp()
        # These codec tests intentionally replace each prior draft. The actual
        # save/discard/cancel interaction is exercised in test_saved_views.
        discard = patch("ferret.apps.compose.views._UnsavedDialog")
        discard.start().return_value.choice = "discard"
        self.addCleanup(discard.stop)
        self.editor = self.page.body_editor
        self.page.url_edit.setText("https://example.test/upload")
        self.errors: list[str] = []
        error_patch = patch(
            "ferret.apps.compose.views.show_error",
            side_effect=lambda _title, message, _parent: self.errors.append(message),
        )
        error_patch.start()
        self.addCleanup(error_patch.stop)
        self.addCleanup(
            lambda: wait_until(lambda: self.page._prepare_task is None, timeout_ms=5000)
        )

    def load(
        self, content: bytes, headers: list[tuple[str, str]] | None = None
    ) -> None:
        self.page.prefill(
            RequestEdit(
                method="POST",
                url="https://example.test/upload",
                headers=[] if headers is None else headers,
                content=content,
            )
        )

    def send(self) -> tuple:
        previous = len(self.controller.calls)
        errors = len(self.errors)
        self.page.send_btn.click()
        self.assertTrue(
            wait_until(
                lambda: (
                    (len(self.controller.calls) > previous or len(self.errors) > errors)
                    and self.page._prepare_task is None
                ),
                timeout_ms=5000,
            ),
            "Body preparation never completed",
        )
        self.assertEqual(self.errors[errors:], [])
        self.assertEqual(len(self.controller.calls), previous + 1)
        self.controller.sending_changed.emit(False)
        return self.controller.calls[-1]

    def open_cell_editor(self, table, row: int, column: int) -> QLineEdit:
        self.page.request_panel.setCurrentTab("Body")
        self.page.show()
        table.setCurrentCell(row, column)
        table.editItem(table.item(row, column))
        app.processEvents()
        editor = table.viewport().focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        assert isinstance(editor, QLineEdit)
        return editor


class BodyTypeTests(ComposeBodyTestCase):
    def test_all_eight_choices_keep_the_selector_visible_and_none_sends_no_body(self):
        self.assertEqual(self.editor.kind_combo.count(), 8)
        self.assertEqual(self.editor.kind, BodyKind.NONE)
        for kind in self.editor.kinds:
            self.editor.set_kind(kind)
            self.assertEqual(self.editor.kind, kind)
            self.assertTrue(self.editor.kind_combo.isVisibleTo(self.editor))
        self.editor.set_kind(BodyKind.NONE)
        self.editor.text.code_widget.setPlainText("hidden text must not leak")
        self.assertEqual(self.send()[4], b"")

    def test_text_kinds_change_highlight_and_owned_header_without_changing_crlf(self):
        payload = b'{\r\n  "original": true\r\n}\r\n'
        self.load(payload)
        for kind, language, content_type in (
            (BodyKind.TEXT, Language.TEXT, "text/plain"),
            (BodyKind.JSON, Language.JSON, "application/json"),
            (BodyKind.XML, Language.XML, "application/xml"),
            (BodyKind.RAW, Language.TEXT, None),
        ):
            with self.subTest(kind=kind):
                self.editor.set_kind(kind)
                app.processEvents()
                self.assertEqual(self.editor.text.code_widget.language, language)
                call = self.send()
                self.assertEqual(call[4], payload)
                self.assertEqual(dict(call[3]).get("Content-Type"), content_type)
        self.editor.text.code_widget.setPlainText("edited\ntext")
        self.assertEqual(self.send()[4], b"edited\ntext")

    def test_prefill_type_inference_preserves_headers_including_known_empty_bodies(
        self,
    ):
        cases = (
            (b"", "application/json", BodyKind.JSON),
            (b"", "text/plain", BodyKind.TEXT),
            (b"", "application/xml", BodyKind.XML),
            (b"", "application/x-www-form-urlencoded", BodyKind.URLENCODED),
            (b'{"a":1}', "application/problem+json; charset=utf-8", BodyKind.JSON),
            (b"<root />", "application/example+xml", BodyKind.XML),
            (b"opaque", "application/custom", BodyKind.RAW),
            (b"", None, BodyKind.NONE),
            (b"<root />", None, BodyKind.XML),
            (b"plain", None, BodyKind.TEXT),
            (b"\xff", "application/json", BodyKind.RAW),
        )
        for content, content_type, kind in cases:
            with self.subTest(content=content, content_type=content_type):
                headers = (
                    [] if content_type is None else [("cOnTeNt-TyPe", content_type)]
                )
                self.load(content, headers)
                self.assertEqual(self.editor.kind, kind)
                self.assertEqual(self.page.headers_card.items(), headers)
                self.assertFalse(
                    self.page.headers_card.is_managed_header("Content-Type")
                )
                call = self.send()
                self.assertEqual(call[3], tuple(headers))
                self.assertEqual(call[4], content)

    def test_theme_refresh_and_undo_restore_original_crlf_bytes(self):
        payload = b'{\r\n  "keep": true\r\n}\r\n'
        self.load(payload)
        code = self.editor.text.code_widget
        code.highlighter.refresh_style()
        app.processEvents()
        self.assertEqual(self.send()[4], payload)
        code.moveCursor(QTextCursor.MoveOperation.End)
        code.insertPlainText("edited")
        self.assertEqual(self.send()[4], payload.replace(b"\r\n", b"\n") + b"edited")
        code.undo()
        app.processEvents()
        self.assertEqual(self.send()[4], payload)

    def test_manual_header_priority_and_deleted_automatic_header_stays_deleted(self):
        self.load(b"x", [("content-type", "application/json")])
        self.editor.set_kind(BodyKind.XML)
        self.assertEqual(
            self.page.headers_card.items(), [("content-type", "application/json")]
        )
        self.assertFalse(self.editor.hint.isHidden())
        self.editor.set_kind(BodyKind.NONE)
        self.assertEqual(
            self.page.headers_card.items(), [("content-type", "application/json")]
        )

        self.load(b"")
        self.editor.set_kind(BodyKind.JSON)
        cursor = self.page.headers_card.text.code_widget.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        self.editor.text.code_widget.setPlainText("{}")
        self.assertEqual(self.send()[3], ())
        self.editor.set_kind(BodyKind.XML)
        self.assertEqual(
            self.page.headers_card.items(), [("Content-Type", "application/xml")]
        )

    def test_none_temporarily_suppresses_text_and_form_drafts(self):
        self.load(b"original\r\ntext")
        self.editor.set_kind(BodyKind.NONE)
        self.assertEqual(self.send()[4], b"")
        self.editor.set_kind(BodyKind.TEXT)
        self.assertEqual(self.send()[4], b"original\r\ntext")

        self.load(b"q=old", [("Content-Type", "application/x-www-form-urlencoded")])
        value_item = self.editor.form._table_widget.item(0, 1)
        assert value_item is not None
        value_item.setText("new value")
        self.editor.set_kind(BodyKind.NONE)
        self.assertEqual(self.send()[4], b"")
        self.editor.set_kind(BodyKind.URLENCODED)
        self.assertEqual(self.send()[4], b"q=new+value")
        self.editor.set_kind(BodyKind.NONE)
        self.editor.set_kind(BodyKind.RAW)
        self.assertEqual(self.send()[4], b"q=new+value")

    def test_binary_body_can_be_sent_raw_or_as_a_file_and_explicit_text_switch_unlocks(
        self,
    ):
        payload = b"\x00\xff\r\n\xfe"
        self.load(payload)
        self.assertTrue(self.editor.binary)
        self.assertTrue(self.editor.text.is_read_only())
        self.assertEqual(self.send()[4], payload)
        self.editor.set_kind(BodyKind.FILE)
        self.assertEqual(self.send()[4], payload)
        self.load(payload)
        self.editor.set_kind(BodyKind.TEXT)
        self.assertFalse(self.editor.binary)
        self.assertFalse(self.editor.text.is_read_only())
        self.editor.text.code_widget.setPlainText("new text")
        self.assertEqual(self.send()[4], b"new text")

    def test_binary_lock_does_not_follow_file_or_form_drafts_into_text_kinds(self):
        payload = b"\xff\x00original"
        for intermediate in (BodyKind.FILE, BodyKind.MULTIPART):
            for target in (BodyKind.JSON, BodyKind.TEXT, BodyKind.XML):
                with self.subTest(intermediate=intermediate, target=target):
                    self.load(payload)
                    self.editor.set_kind(intermediate)
                    self.editor.set_kind(target)
                    self.assertFalse(self.editor.binary)
                    self.assertFalse(self.editor.text.is_read_only())
                    self.assertEqual(self.send()[4], b"")
        self.load(payload)
        self.editor.set_kind(BodyKind.NONE)
        self.assertEqual(self.send()[4], b"")
        self.editor.set_kind(BodyKind.RAW)
        self.assertTrue(self.editor.binary)
        self.assertEqual(self.send()[4], payload)


class UrlencodedComposeTests(ComposeBodyTestCase):
    def test_untouched_encoding_survives_send_and_text_roundtrip(self):
        payload = b"q=a%20b&flag&escaped=%2f&same=1&same=2"
        self.load(payload, [("Content-Type", "application/x-www-form-urlencoded")])
        self.assertEqual(self.send()[4], payload)
        self.editor.set_kind(BodyKind.RAW)
        self.assertEqual(self.send()[4], payload)
        self.editor.set_kind(BodyKind.URLENCODED)
        self.assertEqual(
            parse_qsl(self.send()[4].decode(), keep_blank_values=True),
            parse_qsl(payload.decode(), keep_blank_values=True),
        )

    def test_editing_one_field_preserves_empty_names_spaces_multiline_and_duplicates(
        self,
    ):
        pairs = [("", ""), (" a: ", " x\r\ny "), ("a", "1"), ("a", "2")]
        self.load(
            urlencode(pairs).encode(),
            [("Content-Type", "application/x-www-form-urlencoded")],
        )
        value_item = self.editor.form._table_widget.item(2, 1)
        assert value_item is not None
        value_item.setText("changed")
        expected = pairs[:2] + [("a", "changed"), ("a", "2")]
        self.assertEqual(self.send()[4], urlencode(expected).encode())
        self.editor.set_kind(BodyKind.RAW)
        self.assertEqual(self.editor.text.text(), urlencode(expected))
        self.assertEqual(self.send()[4], urlencode(expected).encode())

    def test_sending_commits_an_open_form_cell_before_testing_raw_body_eligibility(
        self,
    ):
        self.load(
            b"a=old%20value", [("Content-Type", "application/x-www-form-urlencoded")]
        )
        edit = self.open_cell_editor(self.editor.form._table_widget, 0, 1)
        edit.setText("fresh value")
        self.assertEqual(self.send()[4], b"a=fresh+value")

    def test_curl_import_to_send_preserves_default_suppressed_and_empty_headers(self):
        cases = (
            "-d 'q=a%20b&flag'",
            "--data-urlencode 'name=中文 x'",
            "-d ''",
            "-d 'a=1' -H 'Content-Type:'",
            "-d '{}' -H 'Content-Type:'",
            "-d 'a=1' -H 'Content-Type;'",
            "-d 'a=1' -H 'content-type: text/plain'",
        )
        for flags in cases:
            with self.subTest(flags=flags):
                edit = parse_curl("curl https://example.test/upload " + flags)
                self.page.prefill(edit)
                call = self.send()
                self.assertEqual(call[3], tuple(edit.headers))
                self.assertEqual(call[4], edit.content)


class MultipartComposeTests(ComposeBodyTestCase):
    def test_prefill_binary_file_and_odd_header_spacing_sends_original_bytes(self):
        fields = [
            FormField(name="message", value="original\r\ntext"),
            FormField(
                kind="file", name="file", filename="binary.bin", payload=b"\xff\x00\r\n"
            ),
        ]
        content, content_type = encode_multipart(fields, "captured")
        content = content.replace(b"Content-Disposition:", b"content-disposition: ")
        self.load(content, [("Content-Type", content_type)])
        self.assertEqual(self.editor.kind, BodyKind.MULTIPART)
        self.assertEqual(self.editor.multipart.fields()[1].payload, b"\xff\x00\r\n")
        call = self.send()
        self.assertEqual(call[4], content)
        self.assertEqual(dict(call[3])["Content-Type"], content_type)

    def test_unrepresentable_multipart_prefill_keeps_raw_bytes_and_header(self):
        content = b'--fixed\r\nContent-Disposition: form-data; name="a"\r\nX-Extra: keep\r\n\r\ntext\r\n--fixed--\r\n'
        content_type = "multipart/form-data; boundary=fixed"
        self.load(content, [("Content-Type", content_type)])
        self.assertEqual(self.editor.kind, BodyKind.RAW)
        self.assertFalse(self.editor.hint.isHidden())
        self.assertEqual(self.send()[4], content)

    def test_new_form_uses_a_fresh_matching_boundary_and_skips_disabled_file_rows(self):
        self.editor.set_kind(BodyKind.MULTIPART)
        self.editor.multipart.add_field(FormField(name="text", value="value"))
        self.editor.multipart.add_field(
            FormField(kind="file", name="skip", file_path="missing")
        )
        holder = self.editor.multipart.table.cellWidget(1, 0)
        assert holder is not None
        checkbox = holder.findChild(CheckBox)
        assert checkbox is not None
        checkbox.setChecked(False)
        first, second = self.send(), self.send()
        for call in (first, second):
            content_type = dict(call[3])["Content-Type"]
            self.assertEqual(
                parse_multipart(call[4], content_type),
                [FormField(name="text", value="value")],
            )
        self.assertNotEqual(
            dict(first[3])["Content-Type"], dict(second[3])["Content-Type"]
        )
        self.assertEqual(self.page.headers_card.items(), list(second[3]))

    def test_manual_boundary_is_honoured_and_pending_cell_edit_rebuilds_imported_body(
        self,
    ):
        content, content_type = encode_multipart(
            [FormField(name="a", value="old")], "manual"
        )
        self.load(content, [("Content-Type", content_type)])
        edit = self.open_cell_editor(self.editor.multipart.table, 0, 2)
        edit.setText("new value")
        call = self.send()
        self.assertEqual(dict(call[3])["Content-Type"], content_type)
        self.assertEqual(multipart_boundary(content_type), "manual")
        self.assertEqual(
            parse_multipart(call[4], content_type),
            [FormField(name="a", value="new value")],
        )

    def test_incompatible_duplicate_missing_and_colliding_headers_block_send(self):
        header_sets = (
            [("Content-Type", "application/json")],
            [("Content-Type", "multipart/mixed; boundary=fixed")],
            [("Content-Type", "multipart/form-data")],
            [
                ("Content-Type", "multipart/form-data; boundary=fixed"),
                ("content-type", "multipart/form-data; boundary=other"),
            ],
            [],
            [("Content-Type", "multipart/form-data; boundary=collision")],
        )
        for headers in header_sets:
            with self.subTest(headers=headers):
                self.load(b"")
                self.editor.set_kind(BodyKind.MULTIPART)
                self.editor.multipart.add_field(
                    FormField(name="a", value="value\r\n--collision\r\nend")
                )
                self.page.headers_card.set_items(headers)
                previous = len(self.controller.calls)
                errors = len(self.errors)
                self.page.send_btn.click()
                self.assertTrue(
                    wait_until(
                        lambda errors=errors: (
                            len(self.errors) > errors
                            and self.page._prepare_task is None
                        ),
                        timeout_ms=5000,
                    )
                )
                self.assertEqual(len(self.controller.calls), previous)
                self.assertTrue(self.page.send_btn.isEnabled())


class FilePreparationTests(ComposeBodyTestCase):
    def test_file_selection_and_clear_survive_returning_through_binary_raw(self):
        self.load(b"\xffcaptured")
        self.editor.set_kind(BodyKind.FILE)
        self.assertEqual(self.send()[4], b"\xffcaptured")
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "selected.bin"
            selected.write_bytes(b"selected by the user")
            with patch(
                "ferret.apps.common.edit.forms.QFileDialog.getOpenFileName",
                return_value=(str(selected), ""),
            ):
                self.editor.file_picker.choose_button.click()
            self.editor.set_kind(BodyKind.RAW)
            self.editor.set_kind(BodyKind.FILE)
            self.assertEqual(self.send()[4], selected.read_bytes())
        self.editor.file_picker.clear_button.click()
        self.editor.set_kind(BodyKind.RAW)
        self.editor.set_kind(BodyKind.FILE)
        self.assertIsNone(self.editor.file_picker.file().payload)
        self.assertEqual(self.editor.file_picker.file().file_path, "")

    def test_file_reads_run_off_the_gui_thread_and_send_uses_the_original_ui_snapshot(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.bin"
            second = Path(directory) / "second.bin"
            first.write_bytes(b"first\x00\xff\r\n")
            second.write_bytes(b"second")
            self.editor.set_kind(BodyKind.FILE)
            with patch(
                "ferret.apps.common.edit.forms.QFileDialog.getOpenFileName",
                return_value=(str(first), ""),
            ):
                self.editor.file_picker.choose_button.click()
            self.page.headers_card.set_items(
                [("Content-Type", "application/octet-stream"), ("X-Snapshot", "old")]
            )
            entered, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            threads: list[int] = []

            def read(path: str) -> bytes:
                threads.append(threading.get_ident())
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release body preparation")
                return read_body_file(path)

            with patch("ferret.apps.common.edit.body.read_body_file", side_effect=read):
                self.page.send_btn.click()
                self.assertTrue(wait_until(entered.is_set, timeout_ms=5000))
                self.assertFalse(self.page.send_btn.isEnabled())
                self.page.url_edit.setText("https://example.test/changed")
                self.page.headers_card.set_items([("X-Snapshot", "new")])
                self.editor.file_picker.set_file(str(second))
                self.page.record_btn.setChecked(False)
                release.set()
                self.assertTrue(
                    wait_until(
                        lambda: (
                            bool(self.controller.calls)
                            and self.page._prepare_task is None
                        ),
                        timeout_ms=5000,
                    )
                )
            call = self.controller.calls[-1]
            self.assertNotEqual(threads, [threading.get_ident()])
            self.assertEqual(call[2], "https://example.test/upload")
            self.assertEqual(
                call[3],
                (("Content-Type", "application/octet-stream"), ("X-Snapshot", "old")),
            )
            self.assertEqual(call[4], first.read_bytes())
            self.assertTrue(call[5])
            self.controller.sending_changed.emit(False)

    def test_missing_file_error_restores_send_and_the_next_request_can_succeed(self):
        self.editor.set_kind(BodyKind.FILE)
        self.editor.file_picker.set_file("Z:/ferret-test-does-not-exist/body.bin")
        self.page.send_btn.click()
        self.assertTrue(
            wait_until(
                lambda: bool(self.errors) and self.page._prepare_task is None,
                timeout_ms=5000,
            )
        )
        self.assertEqual(self.controller.calls, [])
        self.assertTrue(self.page.send_btn.isEnabled())
        self.editor.file_picker.set_file(filename="empty.bin", payload=b"")
        self.assertEqual(self.send()[4], b"")
