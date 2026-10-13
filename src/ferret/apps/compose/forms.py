"""Compose 的字段表已上移公共层（apps.common.edit.forms），这里只保留兼容入口。"""

from __future__ import annotations

from ferret.apps.common.edit.forms import (
    BodyFilePicker,
    FormDataTable,
    UrlencodedTable,
)

__all__ = ["BodyFilePicker", "FormDataTable", "UrlencodedTable"]
