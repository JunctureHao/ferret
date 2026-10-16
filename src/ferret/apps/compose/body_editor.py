"""Compose 正文编辑：公共 BodyEditor 的薄适配层。

编辑能力已上移 `apps.common.edit.body`（断点窗口共用同一实现）；这里只固定
REQUEST profile，并保留 `load(content, headers)` / `draft()` 的既有签名与
``BodyKind`` / ``BodyDraft`` 再导出 —— `views.py` 自己收集 headers，只调 draft()。
"""

from __future__ import annotations

import base64

from PySide6.QtWidgets import QWidget

from ferret.apps.common.edit.body import (
    BodyDraft,
    BodyEditor,
    BodyEditorState,
    BodyKind,
    BodyProfile,
)
from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.compose.models import SavedBody, SavedFormField
from ferret.core.mitm import FormField

__all__ = ["BodyDraft", "BodyKind", "ComposeBodyEditor"]


class ComposeBodyEditor(BodyEditor):
    """The active kind alone determines what is sent; hidden drafts never leak."""

    def __init__(self, headers: HeaderDualPanel, parent: QWidget | None = None):
        super().__init__(headers, parent, profile=BodyProfile.REQUEST)

    def saved_body(self) -> SavedBody:
        """Save editable values and original bytes without preparing a send."""
        state = self.editor_state()
        try:
            text = state.content.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
            raw_b64 = base64.b64encode(state.content).decode("ascii")
        else:
            raw_b64 = ""
        return SavedBody(
            kind=state.kind.value,
            text=text,
            raw_b64=raw_b64,
            urlencoded=state.form,
            multipart=tuple(_save_field(field) for field in state.fields),
            file=_save_field(state.file) if state.file is not None else None,
            suspended_kind=state.suspended_kind.value,
            form_raw=state.form_raw,
            multipart_raw=state.multipart_raw,
            multipart_content_type=state.multipart_content_type,
        )

    def load_saved(self, body: SavedBody) -> None:
        self.load_editor_state(
            BodyEditorState(
                kind=BodyKind(body.kind),
                suspended_kind=BodyKind(body.suspended_kind),
                content=body.text_bytes(),
                form=body.urlencoded,
                form_raw=body.form_raw,
                fields=tuple(_load_field(field) for field in body.multipart),
                multipart_raw=body.multipart_raw,
                multipart_content_type=body.multipart_content_type,
                file=_load_field(body.file) if body.file is not None else None,
            )
        )


def _save_field(field: FormField) -> SavedFormField:
    return SavedFormField(
        enabled=field.enabled,
        kind=field.kind,
        name=field.name,
        value=field.value,
        file_path=field.file_path,
        filename=field.filename,
        content_type=field.content_type,
        payload=field.payload,
    )


def _load_field(field: SavedFormField) -> FormField:
    return FormField(
        enabled=field.enabled,
        kind="file" if field.kind == "file" else "text",
        name=field.name,
        value=field.value,
        file_path=field.file_path,
        filename=field.filename,
        content_type=field.content_type,
        payload=field.payload,
    )
