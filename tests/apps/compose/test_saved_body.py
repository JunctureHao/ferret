"""Persistence keeps editable bodies and the exact bytes of untouched requests."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.compose.body_editor import BodyKind, ComposeBodyEditor
from ferret.apps.compose.models import SavedBody, SavedRequest
from ferret.core.mitm import FormField, encode_multipart, parse_multipart

app = QApplication.instance() or QApplication([])


class SavedBodyTests(unittest.TestCase):
    def setUp(self):
        self.widgets = []
        self.headers, self.editor = self.make_editor()

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def make_editor(self):
        headers = HeaderDualPanel(True)
        editor = ComposeBodyEditor(headers)
        self.widgets.extend([headers, editor])
        return headers, editor

    def restored(self):
        before = self.headers.items()
        saved = SavedRequest.create(
            name="sample",
            method="POST",
            url="https://example.test/",
            body=self.editor.saved_body(),
            headers=tuple(before),
            managed_headers=self.headers.saved_managed_rows(),
        )
        self.assertEqual(self.headers.items(), before)
        saved = SavedRequest.from_dict(json.loads(json.dumps(saved.to_dict())))
        headers, editor = self.make_editor()
        headers.set_items(saved.headers)
        headers.restore_managed_rows(saved.managed_headers)
        editor.load_saved(saved.body)
        self.assertEqual(headers.items(), before)
        return headers, editor

    def test_text_and_binary_bytes_survive_without_reencoding(self):
        for content, media in (
            (b'{\r\n  "value": 1\r\n}\r\n', "application/json"),
            (b"one\r\ntwo\r\n", "text/plain"),
            (b"\x00\xff\xfe", "application/octet-stream"),
        ):
            with self.subTest(media=media):
                self.headers.set_items([("Content-Type", media)])
                self.editor.load(content, self.headers.items())
                _, restored = self.restored()
                self.assertEqual(restored.draft().build(), content)

    def test_urlencoded_original_and_later_edits(self):
        original = b"q=a%20b&flag&k=1&k=2"
        self.headers.set_items([("Content-Type", "application/x-www-form-urlencoded")])
        self.editor.load(original, self.headers.items())
        _, restored = self.restored()
        self.assertEqual(restored.draft().build(), original)
        restored.form.set_items([("q", "new"), ("k", "1"), ("k", "2")])
        self.assertEqual(restored.draft().build(), b"q=new&k=1&k=2")

    def test_imported_multipart_keeps_boundary_bytes_and_file_payload(self):
        fields = [
            FormField(name="text", value="hello"),
            FormField(
                kind="file",
                name="upload",
                filename="x.bin",
                content_type="application/octet-stream",
                payload=b"\x00\xff",
            ),
        ]
        content, content_type = encode_multipart(fields, "saved-boundary")
        self.headers.set_items([("Content-Type", content_type)])
        self.editor.load(content, self.headers.items())
        headers, restored = self.restored()
        self.assertEqual(restored.draft().build(), content)
        self.assertEqual(restored.multipart.fields(), fields)
        table = restored.multipart.table
        table.item(0, 1).setText("renamed")
        rebuilt = restored.draft().build()
        self.assertIn(b"saved-boundary", rebuilt)
        parsed = parse_multipart(rebuilt, headers.items()[0][1])
        assert parsed is not None
        self.assertEqual(parsed[1].payload, b"\x00\xff")

    def test_local_file_is_read_when_sending_and_disabled_missing_file_stays_disabled(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload.bin"
            path.write_bytes(b"first")
            self.editor.set_kind(BodyKind.MULTIPART)
            fields = [
                FormField(kind="file", name="upload", file_path=str(path)),
                FormField(
                    enabled=False,
                    kind="file",
                    name="ignored",
                    file_path=str(path) + ".missing",
                ),
            ]
            self.editor.multipart.set_fields(fields)
            with patch(
                "ferret.core.mitm.body.read_body_file",
                side_effect=AssertionError("save read a file"),
            ):
                headers, restored = self.restored()
            self.assertEqual(restored.multipart.fields(), fields)
            path.write_bytes(b"second")
            content = restored.draft().build()
            content_type = dict(headers.items())["Content-Type"]
            parsed = parse_multipart(content, content_type)
            assert parsed is not None
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].payload, b"second")

    def test_empty_memory_file_differs_from_unselected_file(self):
        for payload in (None, b"", b"\xffdata"):
            with self.subTest(payload=payload):
                self.editor.set_kind(BodyKind.FILE)
                self.editor.file_picker.set_file(filename="body.bin", payload=payload)
                _, restored = self.restored()
                self.assertEqual(restored.kind, BodyKind.FILE)
                if payload is None:
                    with self.assertRaises(ValueError):
                        restored.draft().build()
                else:
                    self.assertEqual(restored.draft().build(), payload)

    def test_auto_header_remains_owned_and_deleted_header_stays_absent(self):
        self.editor.set_kind(BodyKind.JSON)
        headers, restored = self.restored()
        self.assertTrue(headers.is_managed_header("Content-Type"))
        restored.set_kind(BodyKind.TEXT)
        self.assertEqual(headers.items(), [("Content-Type", "text/plain")])
        self.headers.set_items([])
        headers, restored = self.restored()
        self.assertEqual(headers.items(), [])
        self.assertEqual(restored.kind, BodyKind.JSON)

    def test_none_keeps_suspended_text_document_without_sending_it(self):
        self.headers.set_items([("Content-Type", "text/plain")])
        self.editor.load(b"hello\r\n", self.headers.items())
        self.editor.set_kind(BodyKind.NONE)
        _, restored = self.restored()
        self.assertEqual(restored.draft().build(), b"")
        restored.set_kind(BodyKind.TEXT)
        self.assertEqual(restored.draft().build(), b"hello\r\n")

    def test_malformed_kind_and_nonfinite_time_are_rejected(self):
        for kind in ([], {}):
            with self.assertRaises(ValueError):
                SavedBody.from_dict({"kind": kind})
        request = SavedRequest.create(name="sample", method="GET", url="").to_dict()
        for timestamp in (float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                SavedRequest.from_dict({**request, "updated_at": timestamp})
