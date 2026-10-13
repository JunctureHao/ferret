"""Typed message bodies with separate create and incremental-edit snapshots."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import Message
from enum import StrEnum
from urllib.parse import parse_qsl, urlencode

from PySide6.QtCore import QCoreApplication, QSignalBlocker, Slot
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox

from ferret.core.mitm import (
    FormField,
    encode_multipart,
    multipart_boundary,
    parse_multipart,
    read_body_file,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

from .forms import BodyFilePicker, FormDataTable, UrlencodedTable
from .headers import HeaderDualPanel
from .syntax import Language
from .widgets import ToolPlainTextEdit


class BodyProfile(StrEnum):
    REQUEST = "request"
    RESPONSE = "response"


class BodyMode(StrEnum):
    CREATE = "create"
    PATCH = "patch"


class BodyKind(StrEnum):
    NONE = "none"
    JSON = "json"
    TEXT = "text"
    HTML = "html"
    XML = "xml"
    RAW = "raw"
    URLENCODED = "urlencoded"
    MULTIPART = "multipart"
    FILE = "file"


BODY_KINDS = (
    (BodyKind.NONE, QT_TRANSLATE_NOOP("BodyEditor", "无请求体")),
    (BodyKind.JSON, QT_TRANSLATE_NOOP("BodyEditor", "JSON")),
    (BodyKind.TEXT, QT_TRANSLATE_NOOP("BodyEditor", "Text")),
    (BodyKind.XML, QT_TRANSLATE_NOOP("BodyEditor", "XML")),
    (BodyKind.RAW, QT_TRANSLATE_NOOP("BodyEditor", "Raw")),
    (BodyKind.URLENCODED, QT_TRANSLATE_NOOP("BodyEditor", "Urlencoded")),
    (BodyKind.MULTIPART, QT_TRANSLATE_NOOP("BodyEditor", "Form-data")),
    (BodyKind.FILE, QT_TRANSLATE_NOOP("BodyEditor", "文件")),
)
RESPONSE_BODY_KINDS = (
    (BodyKind.NONE, QT_TRANSLATE_NOOP("BodyEditor", "无响应体")),
    (BodyKind.JSON, QT_TRANSLATE_NOOP("BodyEditor", "JSON")),
    (BodyKind.TEXT, QT_TRANSLATE_NOOP("BodyEditor", "Text")),
    (BodyKind.HTML, QT_TRANSLATE_NOOP("BodyEditor", "HTML")),
    (BodyKind.XML, QT_TRANSLATE_NOOP("BodyEditor", "XML")),
    (BodyKind.RAW, QT_TRANSLATE_NOOP("BodyEditor", "Raw")),
    (BodyKind.FILE, QT_TRANSLATE_NOOP("BodyEditor", "文件")),
)
_TEXT_KINDS = {BodyKind.JSON, BodyKind.TEXT, BodyKind.HTML, BodyKind.XML, BodyKind.RAW}
_CONTENT_TYPES = {
    BodyKind.JSON: "application/json",
    BodyKind.TEXT: "text/plain",
    BodyKind.HTML: "text/html",
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
                    QCoreApplication.translate("BodyEditor", "请先选择正文文件")
                )
            return self.file.payload
        return self.content


@dataclass(frozen=True)
class BodySnapshot:
    headers: tuple[tuple[str, str], ...]
    body: BodyDraft | None


class BodyEditor(QWidget):
    """The active kind alone determines what is sent; hidden drafts never leak."""

    def __init__(
        self,
        headers: HeaderDualPanel,
        parent: QWidget | None = None,
        *,
        profile: BodyProfile = BodyProfile.REQUEST,
        enabled_kinds: Sequence[BodyKind] | None = None,
    ):
        super().__init__(parent)
        self.headers = headers
        self.profile = BodyProfile(profile)
        choices = (
            BODY_KINDS if self.profile is BodyProfile.REQUEST else RESPONSE_BODY_KINDS
        )
        self._choices = tuple(
            (kind, label)
            for kind, label in choices
            if enabled_kinds is None or kind in enabled_kinds
        )
        self.kinds = tuple(kind for kind, _ in self._choices)
        if BodyKind.NONE not in self.kinds or BodyKind.RAW not in self.kinds:
            raise ValueError("Body profiles must include none and raw")
        self._kind = BodyKind.NONE
        self._suspended_kind = BodyKind.NONE
        self._loading = False
        self._text_raw = b""
        self._text_initial = ""
        self._text_dirty = False
        self._binary = False
        self._source_content = b""
        self._available = True
        self._decode_ok = True
        self._strict_charset = False
        self._text_guarded = False
        self._text_replaced = False
        self._body_allowed = True
        self._protocol_hint = ""
        self._updating_hint = False
        self._form_raw: bytes | None = None
        self._form_dirty = False
        self._form_initial: tuple[tuple[str, str], ...] | None = None
        self._multipart_raw: bytes | None = None
        self._multipart_dirty = False
        self._multipart_initial: tuple[FormField, ...] | None = None
        self._multipart_type = ""
        self._file_initialized = False
        self._notice = ""

        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems(
            [
                resolve_marker(dict(self._choices), kind, "BodyEditor")
                for kind, _ in self._choices
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
        self.empty_label = CaptionLabel(self.empty)
        empty_layout.addWidget(self.empty_label)
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
        self.kind_combo.setCurrentIndex(self.kinds.index(kind))

    def set_body_allowed(self, allowed: bool, hint: str = "") -> None:
        """Temporarily hide drafts without changing their kind or header ownership."""
        if self._body_allowed and not allowed:
            self.form.commit_active_editor()
            self.multipart.commit_active_editor()
        self._body_allowed = allowed
        self._protocol_hint = hint
        self._render()

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

    def _set_text(self, content: bytes, *, replaced: bool = False) -> None:
        self._text_raw = content
        self._text_dirty = False
        self._text_guarded = False
        self._text_replaced = replaced
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
        if self._form_initial is not None:
            self._form_dirty = tuple(pairs) != self._form_initial
        if self._form_raw is not None and not self._form_dirty:
            return self._form_raw
        return urlencode(pairs).encode("utf-8")

    @Slot(int)
    def _on_kind_changed(self, index: int) -> None:
        if self._loading:
            return
        new = self.kinds[index]
        if not self._body_allowed:
            with QSignalBlocker(self.kind_combo):
                self.kind_combo.setCurrentIndex(self.kinds.index(self._kind))
            return
        if (
            new in _TEXT_KINDS - {BodyKind.RAW} or new is BodyKind.URLENCODED
        ) and self._charset_blocked():
            with QSignalBlocker(self.kind_combo):
                self.kind_combo.setCurrentIndex(self.kinds.index(self._kind))
            self._notice = self.tr(
                "请先将 Content-Type 的 charset 改为 UTF-8，再选择文本类型。"
            )
            self._render()
            return
        old = self._suspended_kind if self._kind is BodyKind.NONE else self._kind
        if new is BodyKind.NONE:
            self._suspended_kind = old
        elif old is BodyKind.URLENCODED and new in _TEXT_KINDS:
            self._set_text(self._form_content())
        elif old in _TEXT_KINDS and new is BodyKind.URLENCODED:
            raw = b"" if self._text_guarded else self._text_content()
            try:
                pairs = parse_qsl(
                    raw.decode("utf-8"), keep_blank_values=True, errors="strict"
                )
            except UnicodeError:
                pairs = []
            self.form.set_items(pairs)
            self._form_raw = None
            self._form_initial = None
            self._form_dirty = True
            if self._binary or self._text_guarded:
                self._set_text(b"", replaced=True)
        elif (
            old in _TEXT_KINDS
            and new is BodyKind.FILE
            and self._binary
            and not self._file_initialized
            and self._available
            and self._decode_ok
        ):
            self.file_picker.set_file(filename="body.bin", payload=self._text_raw)
        elif new in _TEXT_KINDS - {BodyKind.RAW} and (
            self._binary or self._text_guarded
        ):
            self._set_text(b"", replaced=True)
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
        self.kind_combo.setEnabled(self._body_allowed)
        self.empty_label.setText(
            self._protocol_hint
            if not self._body_allowed and self._protocol_hint
            else self.tr("该请求不带请求体")
            if self.profile is BodyProfile.REQUEST
            else self.tr("该响应不带响应体")
        )
        if not self._body_allowed:
            page = self.empty
        elif kind in _TEXT_KINDS:
            language = {
                BodyKind.JSON: Language.JSON,
                BodyKind.XML: Language.XML,
                BodyKind.HTML: Language.XML,
            }.get(kind, Language.TEXT)
            # Rehighlighting emits textChanged even though the body did not change.
            with QSignalBlocker(self.text):
                self.text.code_widget.set_language(language)
            self.text.set_read_only(
                self._binary or self._text_guarded or self._charset_blocked()
            )
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

    def _charset_blocked(self, content_types: Sequence[str] | None = None) -> bool:
        if not self._strict_charset:
            return False
        for value in self._content_types() if content_types is None else content_types:
            message = Message()
            message["Content-Type"] = value
            for name, charset in (
                message.get_params(header="content-type", failobj=()) or ()
            ):
                if name.lower() == "charset" and str(charset).strip().lower() not in (
                    "utf-8",
                    "utf8",
                ):
                    return True
        return False

    def _check_text_charset(self) -> None:
        if (
            self._kind in _TEXT_KINDS | {BodyKind.URLENCODED}
            and self._charset_blocked()
        ):
            raise ValueError(
                self.tr("请先将 Content-Type 的 charset 改为 UTF-8，再替换文本正文。")
            )

    @Slot()
    def _update_hint(self) -> None:
        if self._loading or self._updating_hint:
            return
        self._updating_hint = True
        try:
            self._refresh_hint()
        finally:
            self._updating_hint = False

    def _refresh_hint(self) -> None:
        messages = []
        charset_blocked = self._charset_blocked()
        self.text.set_read_only(self._binary or self._text_guarded or charset_blocked)
        self.form.set_read_only(charset_blocked)
        if not self._body_allowed:
            messages.append(self._protocol_hint)
        elif self._kind in _TEXT_KINDS and self._text_guarded and not self._available:
            messages.append(
                self.tr(
                    "原正文尚未取得，不能将缺失内容当作空正文；可明确选择替换内容。"
                )
            )
        elif self._kind in _TEXT_KINDS and self._text_guarded and not self._decode_ok:
            messages.append(
                self.tr("原正文解码失败，已锁定为只读；未修改时保留原始报文。")
            )
        elif charset_blocked and self._kind in _TEXT_KINDS | {BodyKind.URLENCODED}:
            messages.append(
                self.tr(
                    "正文声明的 charset 不是 UTF-8，已锁定为只读；修改声明后可选择文本替换。"
                )
            )
        elif self._kind in _TEXT_KINDS and self._binary:
            messages.append(
                self.tr(
                    "这段内容不是合法的 UTF-8，已锁定为只读；未修改时保留原始字节。"
                )
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
                if self.profile is BodyProfile.REQUEST
                else self.tr(
                    "响应头中的 Content-Type 与所选类型不同，将保留你填写的响应头。"
                )
            )
        self.hint.setText("\n".join(messages))
        self.hint.setVisible(bool(messages))

    def load(
        self,
        content: bytes | None,
        headers: Sequence[tuple[str, str]],
        *,
        available: bool = True,
        decode_ok: bool = True,
        strict_charset: bool = False,
    ) -> None:
        """Load message data; guards belong to the caller, never to live flows."""
        content = content or b""
        values = [v for k, v in headers if k.strip().lower() == "content-type"]
        content_type = values[0] if len(values) == 1 else ""
        media = content_type.partition(";")[0].strip().lower()
        self._loading = True
        try:
            self._source_content = content
            self._available = available
            self._decode_ok = decode_ok
            self._strict_charset = strict_charset
            self._body_allowed = True
            self._protocol_hint = ""
            self._set_text(content)
            self._text_guarded = (
                not available or not decode_ok or self._charset_blocked(values)
            )
            if not available or not decode_ok:
                self.text.set_text("", Language.TEXT)
                self._text_initial = ""
            self.form.set_items([])
            self.multipart.set_fields([])
            self.file_picker.set_file()
            self._file_initialized = False
            self._form_raw = self._multipart_raw = None
            self._form_dirty = self._multipart_dirty = False
            self._form_initial = self._multipart_initial = None
            self._multipart_type = ""
            self._notice = ""
            fields = (
                parse_multipart(content, content_type)
                if media == "multipart/form-data"
                and BodyKind.MULTIPART in self.kinds
                and not self._text_guarded
                else None
            )
            if self._text_guarded:
                kind = BodyKind.RAW
            elif fields is not None:
                kind = BodyKind.MULTIPART
                self.multipart.set_fields(fields)
                self._multipart_raw = content
                self._multipart_type = content_type
                self._multipart_initial = tuple(fields)
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
            elif media == "text/html" and BodyKind.HTML in self.kinds:
                kind = BodyKind.HTML
            elif (
                media == "application/x-www-form-urlencoded"
                and BodyKind.URLENCODED in self.kinds
            ):
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
                    self._form_initial = tuple(pairs)
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
            if kind not in self.kinds:
                kind = BodyKind.RAW
            self._kind = self._suspended_kind = kind
            self.set_kind(kind)
        finally:
            self._loading = False
        self._render()

    def snapshot(self, *, mode: BodyMode) -> BodySnapshot:
        """Freeze headers and body together; read headers only after this.

        draft 的 multipart 分支可能替换仍受管理的 Content-Type（boundary 过期
        重发），所以必须先跑完 body 判定、再读 `headers.items()` —— 调用方把两步
        拆开就会拿到旧头。CREATE 恒给草稿（None 只属于 PATCH）；PATCH 对「没改
        过」一律返回 body=None，调用方跳过正文写回（未编辑保真）。
        """
        draft = self._patched_draft(mode) if mode is BodyMode.PATCH else None
        return BodySnapshot(tuple(self.headers.items()), draft)

    def _patched_draft(self, mode: BodyMode) -> BodyDraft | None:
        """PATCH 语义：没改返回 None；显式清空/替换返回 BodyDraft。"""
        if not self._body_allowed:
            # 协议禁止正文：只有源里有实内容才需要明确清空；源为空/缺失时
            # 不动，避免无端重写 Content-Length。
            if self._source_content and self._available:
                return BodyDraft(content=b"")
            return None
        if self._kind is BodyKind.NONE:
            # 源有正文 → 明确清空；源为空 → 没改。
            return BodyDraft(content=b"") if self._source_content else None
        if self._kind in _TEXT_KINDS:
            self._check_text_charset()
            if not self._text_dirty and not self._text_replaced:
                return None
            content = self._text_content()
            if not self._text_replaced and content == self._source_content:
                # 改回原文等价于没改：保留原始 CRLF 字节，不重写长度。
                return None
            return BodyDraft(content=content)
        if self._kind is BodyKind.URLENCODED:
            self._check_text_charset()
            # _form_content 先提交活动单元格并刷新 _form_dirty。
            content = self._form_content()
            if self._form_raw is not None and not self._form_dirty:
                return None
            return BodyDraft(content=content)
        if self._kind is BodyKind.FILE:
            if not self._file_initialized:
                return None
            file = self.file_picker.file()
            if (
                file.payload == self._source_content
                and not file.file_path
                and self._source_content
            ):
                # 预填的内存文件不算改（装载时二进制 → 文件类型；合成名
                # body.bin 与空 file_path 都是预填痕迹，不是用户选择）。
                return None
            return BodyDraft(file=file)
        # MULTIPART：draft() 内部已按需重发 boundary（可能改 managed 头）。
        draft = self.draft()
        if (
            self._multipart_raw is not None
            and not self._multipart_dirty
            and self._multipart_type
            and self._content_types() == [self._multipart_type]
        ):
            return None
        return draft

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
