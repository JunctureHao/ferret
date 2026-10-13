"""Compose 正文编辑：公共 BodyEditor 的薄适配层。

编辑能力已上移 `apps.common.edit.body`（断点窗口共用同一实现）；这里只固定
REQUEST profile，并保留 `load(content, headers)` / `draft()` 的既有签名与
``BodyKind`` / ``BodyDraft`` 再导出 —— `views.py` 自己收集 headers，只调 draft()。
"""

from __future__ import annotations

from PySide6.QtWidgets import QWidget

from ferret.apps.common.edit.body import (
    BodyDraft,
    BodyEditor,
    BodyKind,
    BodyProfile,
)
from ferret.apps.common.edit.headers import HeaderDualPanel

__all__ = ["BodyDraft", "BodyKind", "ComposeBodyEditor"]


class ComposeBodyEditor(BodyEditor):
    """The active kind alone determines what is sent; hidden drafts never leak."""

    def __init__(self, headers: HeaderDualPanel, parent: QWidget | None = None):
        super().__init__(headers, parent, profile=BodyProfile.REQUEST)
