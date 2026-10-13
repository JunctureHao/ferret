"""Compose body drafts: typed editors, lossless loading and explicit conversion."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode

from PySide6.QtCore import QCoreApplication, QSignalBlocker, Slot
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox

from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.compose.forms import BodyFilePicker, FormDataTable, UrlencodedTable
from ferret.core.mitm import (
    FormField,
    encode_multipart,
    multipart_boundary,
    parse_multipart,
    read_body_file,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker


class BodyKind(StrEnum):
    NONE = "none"
    JSON = "json"
    TEXT = "text"
    XML = "xml"
    RAW = "raw"
    URLENCODED = "urlencoded"
    MULTIPART = "multipart"
    FILE = "file"


BODY_KINDS = (
    (BodyKind.NONE, QT_TRANSLATE_NOOP("ComposeBodyEditor", "无请求体")),
    (BodyKind.JSON, QT_TRANSLATE_NOOP("ComposeBodyEditor", "JSON")),
    (BodyKind.TEXT, QT_TRANSLATE_NOOP("ComposeBodyEditor", "Text")),
    (BodyKind.XML, QT_TRANSLATE_NOOP("ComposeBodyEditor", "XML")),
    (BodyKind.RAW, QT_TRANSLATE_NOOP("ComposeBodyEditor", "Raw")),
    (BodyKind.URLENCODED, QT_TRANSLATE_NOOP("ComposeBodyEditor", "Urlencoded")),
    (BodyKind.MULTIPART, QT_TRANSLATE_NOOP("ComposeBodyEditor", "Form-data")),
    (BodyKind.FILE, QT_TRANSLATE_NOOP("ComposeBodyEditor", "文件")),
)
_TEXT_KINDS = {BodyKind.JSON, BodyKind.TEXT, BodyKind.XML, BodyKind.RAW}
_CONTENT_TYPES = {
    BodyKind.JSON: "application/json",
    BodyKind.TEXT: "text/plain",
    BodyKind.XML: "application/xml",
    BodyKind.URLENCODED: "application/x-www-form-urlencoded",
    BodyKind.FILE: "application/octet-stream",
}


def _media_matches(kind: BodyKind, media: str) -> bool:
    if kind is BodyKind.JSON:
        return media.endswith(("/json", "+json"))
    if kind is BodyKind.XML:
        return media.endswith(("/xml", "+xml"))
    return media == _CONTENT_TYPES.get(kind)


@dataclass(frozen=True)
class BodyDraft:
    """No Qt objects: file reads and multipart assembly may run in FunctionTask."""

    content: bytes = b""
    fields: tuple[FormField, ...] | None = None
    file: FormField | None = None
    boundary: str | None = None

    @property
    def needs_worker(self) -> bool:
        return self.fields is not None or self.file is not None

    def build(self) -> bytes:
        if self.fields is not None:
            return encode_multipart(self.fields, self.boundary)[0]
        if self.file is not None:
            if self.file.file_path:
                return read_body_file(self.file.file_path)
            if self.file.payload is None:
                raise ValueError(
                    QCoreApplication.translate(
                        "ComposeBodyEditor", "请先选择请求体文件"
                    )
                )
            return self.file.payload
        return self.content


class ComposeBodyEditor(QWidget):
    """The active kind alone determines what is sent; hidden drafts never leak."""

    def __init__(self, headers: HeaderDualPanel, parent: QWidget | None = None):
        super().__init__(parent)
        self.headers = headers
        self._kind = BodyKind.NONE
        self._suspended_kind = BodyKind.NONE
        self._loading = False
        self._text_raw = b""
        self._text_initial = ""
        self._text_dirty = False
        self._binary = False
        self._form_raw: bytes | None = None
        self._form_dirty = False
        self._multipart_raw: bytes | None = None
        self._multipart_dirty = False
        self._multipart_type = ""
        self._file_initialized = False
        self._notice = ""

        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems(
            [
                resolve_marker(dict(BODY_KINDS), kind, "ComposeBodyEditor")
                for kind, _ in BODY_KINDS
            ]
        )
        self.kind_combo.setMinimumWidth(150)
        toolbar = QHBoxLayout()
        toolbar.addWidget(BodyLabel(self.tr("数据类型"), self))
        toolbar.addWidget(self.kind_combo)
        toolbar.addStretch(1)

        self.text = ToolPlainTextEdit(self)
        self.form = UrlencodedTable(self)
        self.multipart = FormDataTable(self)
        self.file_picker = BodyFilePicker(self)
        self.empty = QWidget(self)
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.addStretch(1)
        empty_layout.addWidget(CaptionLabel(self.tr("该请求不带请求体"), self.empty))
        empty_layout.addStretch(1)
        self.stack = QStackedWidget(self)
        for page in (
            self.empty,
            self.text,
            self.form,
            self.multipart,
            self.file_picker,
        ):
            self.stack.addWidget(page)
        self.hint = CaptionLabel(self)
        self.hint.setWordWrap(True)
        self.hint.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(toolbar)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.hint)

        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.text.changed.connect(self._on_text_edited)
        self.form.changed.connect(self._on_form_edited)
        self.multipart.changed.connect(self._on_multipart_edited)
        self.file_picker.changed.connect(self._on_file_edited)
        self.headers.changed.connect(self._update_hint)
        self._render()

    @property
    def kind(self) -> BodyKind:
        return self._kind

    @property
    def binary(self) -> bool:
        return self._binary

    def set_kind(self, kind: BodyKind) -> None:
        self.kind_combo.setCurrentIndex([entry[0] for entry in BODY_KINDS].index(kind))

    @Slot()
    def _on_text_edited(self) -> None:
        if not self._loading:
            # Qt also emits changed when a theme rehighlights the document.
            # Compare the displayed baseline, not raw CRLF bytes, so formatting
            # and undo back to the original text keep the exact captured body.
            self._text_dirty = self.text.text() != self._text_initial

    @Slot()
    def _on_form_edited(self) -> None:
        if not self._loading:
            self._form_dirty = True

    @Slot()
    def _on_multipart_edited(self) -> None:
        if not self._loading:
            self._multipart_dirty = True

    @Slot()
    def _on_file_edited(self) -> None:
        if not self._loading:
            self._file_initialized = True

    def _set_text(self, content: bytes) -> None:
        self._text_raw = content
        self._text_dirty = False
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            self._binary = True
            text = ""
        else:
            self._binary = False
        self.text.set_text(text, Language.TEXT)
        self._text_initial = self.text.text()
        self.text.set_read_only(self._binary)

    def _text_content(self) -> bytes:
        if self._binary or not self._text_dirty:
            return self._text_raw
        return self.text.text().encode("utf-8")

    def _form_content(self) -> bytes:
        # Committing an open delegate can emit changed. Read first, test second.
        pairs = self.form.items()
        if self._form_raw is not None and not self._form_dirty:
            return self._form_raw
        return urlencode(pairs).encode("utf-8")

    @Slot(int)
    def _on_kind_changed(self, index: int) -> None:
        if self._loading:
            return
        new = BODY_KINDS[index][0]
        old = self._suspended_kind if self._kind is BodyKind.NONE else self._kind
        if new is BodyKind.NONE:
            self._suspended_kind = old
        elif old is BodyKind.URLENCODED and new in _TEXT_KINDS:
            self._set_text(self._form_content())
        elif old in _TEXT_KINDS and new is BodyKind.URLENCODED:
            raw = self._text_content()
            try:
                pairs = parse_qsl(
                    raw.decode("utf-8"), keep_blank_values=True, errors="strict"
                )
            except UnicodeError:
                pairs = []
            self.form.set_items(pairs)
            self._form_raw = None
            self._form_dirty = True
            if self._binary:
                self._set_text(b"")
        elif (
            old in _TEXT_KINDS
            and new is BodyKind.FILE
            and self._binary
            and not self._file_initialized
        ):
            self.file_picker.set_file(filename="body.bin", payload=self._text_raw)
        elif new in _TEXT_KINDS - {BodyKind.RAW} and self._binary:
            self._set_text(b"")
        self._kind = new
        if new is BodyKind.FILE:
            self._file_initialized = True
        if new is not BodyKind.NONE:
            self._suspended_kind = new
        self._notice = ""
        default = _CONTENT_TYPES.get(new)
        if new is BodyKind.MULTIPART:
            default = "multipart/form-data; boundary=" + secrets.token_hex(16)
        self.headers.set_managed_header("Content-Type", default)
        self._render()

    def _render(self) -> None:
        kind = self._kind
        if kind in _TEXT_KINDS:
            language = {BodyKind.JSON: Language.JSON, BodyKind.XML: Language.XML}.get(
                kind, Language.TEXT
            )
            # Rehighlighting emits textChanged even though the body did not change.
            with QSignalBlocker(self.text):
                self.text.code_widget.set_language(language)
            self.text.set_read_only(self._binary)
            page = self.text
        else:
            page = {
                BodyKind.NONE: self.empty,
                BodyKind.URLENCODED: self.form,
                BodyKind.MULTIPART: self.multipart,
                BodyKind.FILE: self.file_picker,
            }[kind]
        self.stack.setCurrentWidget(page)
        self._update_hint()

    def _content_types(self) -> list[str]:
        return [
            v for k, v in self.headers.items() if k.strip().lower() == "content-type"
        ]

    @Slot()
    def _update_hint(self) -> None:
        if self._loading:
            return
        messages = []
        if self._kind in _TEXT_KINDS and self._binary:
            messages.append(
                self.tr("这段内容不是合法的 UTF-8，已锁定为只读；发送时按原样发出。")
            )
        elif self._notice:
            messages.append(self._notice)
        expected = _CONTENT_TYPES.get(self._kind)
        types = self._content_types()
        if (
            expected
            and types
            and any(
                not _media_matches(self._kind, value.split(";", 1)[0].strip().lower())
                for value in types
            )
        ):
            messages.append(
                self.tr(
                    "请求头中的 Content-Type 与所选类型不同，将保留你填写的请求头。"
                )
            )
        self.hint.setText("\n".join(messages))
        self.hint.setVisible(bool(messages))

    def load(self, content: bytes | None, headers: list[tuple[str, str]]) -> None:
        """Load a complete new request without invoking user-switch side effects."""
        content = content or b""
        values = [v for k, v in headers if k.strip().lower() == "content-type"]
        content_type = values[0] if len(values) == 1 else ""
        media = content_type.partition(";")[0].strip().lower()
        self._loading = True
        try:
            self._set_text(content)
            self.form.set_items([])
            self.multipart.set_fields([])
            self.file_picker.set_file()
            self._file_initialized = False
            self._form_raw = self._multipart_raw = None
            self._form_dirty = self._multipart_dirty = False
            self._multipart_type = ""
            self._notice = ""
            fields = (
                parse_multipart(content, content_type)
                if media == "multipart/form-data"
                else None
            )
            if fields is not None:
                kind = BodyKind.MULTIPART
                self.multipart.set_fields(fields)
                self._multipart_raw = content
                self._multipart_type = content_type
            elif media == "multipart/form-data":
                kind = BodyKind.RAW
                self._notice = self.tr(
                    "此 multipart 正文无法完整转换为字段表，已保留原始正文。"
                )
            elif self._binary:
                kind = BodyKind.RAW
            elif _media_matches(BodyKind.JSON, media):
                kind = BodyKind.JSON
            elif _media_matches(BodyKind.XML, media):
                kind = BodyKind.XML
            elif media == "text/plain":
                kind = BodyKind.TEXT
            elif media == "application/x-www-form-urlencoded":
                try:
                    pairs = parse_qsl(
                        content.decode("utf-8"), keep_blank_values=True, errors="strict"
                    )
                except UnicodeError:
                    kind = BodyKind.RAW
                    self._notice = self.tr("此表单包含非 UTF-8 字段，已保留原始正文。")
                else:
                    kind = BodyKind.URLENCODED
                    self.form.set_items(pairs)
                    self._form_raw = content
            elif not content:
                kind = BodyKind.NONE
            elif values:
                kind = BodyKind.RAW
            elif content.lstrip().startswith((b"{", b"[")):
                kind = BodyKind.JSON
            elif content.lstrip().startswith(b"<"):
                kind = BodyKind.XML
            else:
                kind = BodyKind.TEXT
            self._kind = self._suspended_kind = kind
            self.set_kind(kind)
        finally:
            self._loading = False
        self._render()

    def draft(self) -> BodyDraft:
        """Capture the current editor and the boundary before collecting headers."""
        if self._kind is BodyKind.NONE:
            return BodyDraft()
        if self._kind in _TEXT_KINDS:
            return BodyDraft(content=self._text_content())
        if self._kind is BodyKind.URLENCODED:
            return BodyDraft(content=self._form_content())
        if self._kind is BodyKind.FILE:
            return BodyDraft(file=self.file_picker.file())
        fields = tuple(self.multipart.fields())
        values = self._content_types()
        if len(values) != 1:
            raise ValueError(
                self.tr(
                    "Form-data 需要唯一的 Content-Type 请求头，请重新选择类型或修正请求头。"
                )
            )
        content_type = values[0]
        if (
            self._multipart_raw is not None
            and not self._multipart_dirty
            and content_type == self._multipart_type
        ):
            return BodyDraft(content=self._multipart_raw)
        if self.headers.is_managed_header("Content-Type"):
            content_type = "multipart/form-data; boundary=" + secrets.token_hex(16)
            self.headers.set_managed_header("Content-Type", content_type)
        boundary = (
            multipart_boundary(content_type)
            if content_type.partition(";")[0].strip().lower() == "multipart/form-data"
            else None
        )
        if boundary is None:
            raise ValueError(
                self.tr(
                    "Form-data 的 Content-Type 必须是 multipart/form-data 并包含有效的 boundary。"
                )
            )
        return BodyDraft(fields=fields, boundary=boundary)
