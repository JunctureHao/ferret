"""Compose-only field editors; body encoding stays outside Qt widgets."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    FluentIcon,
    MessageBoxBase,
    PushButton,
    RoundMenu,
    SubtitleLabel,
    TableWidget,
)

from ferret.apps.common.button import TransparentTooltipButton
from ferret.apps.common.edit import (
    ItemSource,
    ItemTableToolWidget,
    Language,
    ToolPlainTextEdit,
    ToolWidget,
)
from ferret.apps.common.icon import BaseAction
from ferret.core.mitm import FormField

_FIELD_PRESENT = int(Qt.ItemDataRole.UserRole) + 131


class _FieldTextDialog(MessageBoxBase):
    """Use the shared editor for field values containing line breaks."""

    def __init__(self, text: str, parent: QWidget):
        super().__init__(parent)
        self.edit = ToolPlainTextEdit(self)
        self.edit.set_read_only(False)
        self.edit.set_text(text, Language.TEXT)
        self._original = text
        self._initial = self.edit.text()
        self.edit.setMinimumHeight(200)
        self.viewLayout.addWidget(SubtitleLabel(self.tr("编辑字段内容"), self))
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(520)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

    def text(self) -> str:
        text = self.edit.text()
        return self._original if text == self._initial else text


class UrlencodedTable(ItemTableToolWidget):
    """An ordered form table without header-style trimming or text conversion.

    A loaded empty-name/empty-value field is real data. Only newly inserted,
    untouched placeholder rows are omitted; once a row contains data it remains
    a field even if the user subsequently clears both cells.
    """

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(editable=True, parent=parent)
        self._table_widget.items_changed.disconnect(self.items_changed)
        self._table_widget.items_changed.connect(self._on_fields_changed)
        self.edit_value_button = TransparentTooltipButton(FluentIcon.EDIT, self)
        self.edit_value_button.setToolTip(self.tr("编辑字段内容"))
        self.edit_value_button.clicked.connect(self._edit_current_cell)
        self.tool_layout.addWidget(self.edit_value_button)

    def set_items(self, items: ItemSource):
        super().set_items(items)
        with QSignalBlocker(self._table_widget):
            for row in range(self._table_widget.rowCount()):
                item = self._table_widget.item(row, 0)
                assert item is not None
                item.setData(_FIELD_PRESENT, True)

    def commit_active_editor(self) -> None:
        self._table_widget.commit_active_editor()

    def items(self) -> list[tuple[str, str]]:
        self.commit_active_editor()
        pairs = []
        for row in range(self._table_widget.rowCount()):
            key = self._table_widget.item(row, 0)
            value = self._table_widget.item(row, 1)
            if key is None:
                continue
            pair = (key.text(), value.text() if value is not None else "")
            if pair != ("", "") or key.data(_FIELD_PRESENT):
                pairs.append(pair)
        return pairs

    def _on_fields_changed(self) -> None:
        with QSignalBlocker(self._table_widget):
            for row in range(self._table_widget.rowCount()):
                key = self._table_widget.item(row, 0)
                value = self._table_widget.item(row, 1)
                if key is not None and (key.text() or (value and value.text())):
                    key.setData(_FIELD_PRESENT, True)
        self.items_changed.emit()
        self.changed.emit()

    def _edit_current_cell(self) -> None:
        self.commit_active_editor()
        item = self._table_widget.currentItem()
        if item is None or not self.editable:
            return
        original = item.text()
        dialog = _FieldTextDialog(original, self.window())
        if dialog.exec() and dialog.text() != original:
            item.setText(dialog.text())
        dialog.deleteLater()

    def set_read_only(self, read_only: bool):
        super().set_read_only(read_only)
        self.edit_value_button.setEnabled(not read_only)


class BodyFilePicker(QWidget):
    """Choose a disk path or retain an imported in-memory file, without I/O."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None, *, compact: bool = False):
        super().__init__(parent)
        self._field = FormField(kind="file")
        self.filename_label = CaptionLabel(self)
        self.filename_label.setMinimumWidth(0)
        self.filename_label.setTextFormat(Qt.TextFormat.PlainText)
        self.filename_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        if compact:
            self.choose_button = TransparentTooltipButton(FluentIcon.FOLDER, self)
            self.choose_button.setToolTip(self.tr("选择文件"))
        else:
            self.choose_button = PushButton(
                FluentIcon.FOLDER, self.tr("选择文件"), self
            )
        self.clear_button = TransparentTooltipButton(FluentIcon.CLOSE, self)
        self.clear_button.setToolTip(self.tr("清除文件"))
        self.choose_button.clicked.connect(self._choose_file)
        self.clear_button.clicked.connect(self._clear_file)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)
        layout.addWidget(self.filename_label, 1)
        layout.addWidget(self.choose_button)
        layout.addWidget(self.clear_button)
        self._refresh_label()

    def set_file(
        self, path: str = "", *, filename: str = "", payload: bytes | None = None
    ) -> None:
        self._field = FormField(
            kind="file",
            file_path=path,
            filename=filename or (Path(path).name if path else ""),
            payload=None if path else payload,
        )
        self._refresh_label()

    def file(self) -> FormField:
        return self._field

    def _refresh_label(self) -> None:
        field = self._field
        if field.file_path:
            self.filename_label.setText(field.filename)
            self.filename_label.setToolTip(field.file_path)
        elif field.payload is not None:
            filename = field.filename or self.tr("内存文件")
            self.filename_label.setText(
                self.tr("{}（{} 字节）").format(filename, len(field.payload))
            )
            self.filename_label.setToolTip(filename)
        else:
            self.filename_label.setText(self.tr("未选择文件"))
            self.filename_label.setToolTip("")
        self.clear_button.setVisible(bool(field.file_path) or field.payload is not None)

    def _choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self.window(), self.tr("选择文件"), self._field.file_path
        )
        if path:
            self.set_file(path)
            self.changed.emit()

    def _clear_file(self) -> None:
        self.set_file()
        self.changed.emit()

    def set_read_only(self, read_only: bool) -> None:
        self.choose_button.setEnabled(not read_only)
        self.clear_button.setEnabled(not read_only)


class _FormTable(TableWidget):
    """Commit the Fluent table delegate before a send or a row operation."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._committing = False
        self._auto_value_width = False
        self._resizing_value = False
        self._minimum_value_width = 140

    def stretch_value_column(self) -> None:
        """Fill spare space while keeping a readable value column when narrow.

        Qt's Stretch mode has only a header-wide minimum, which would also
        enlarge the checkbox and menu columns. Resize just the value column;
        an explicit drag of that column opts out of automatic sizing.
        """
        self._auto_value_width = True
        self.horizontalHeader().sectionResized.connect(self._on_section_resized)
        self._fit_value_column()

    def _on_section_resized(self, column: int, _old: int, width: int) -> None:
        if self._resizing_value:
            return
        if column == 2:
            self._auto_value_width = False
            self._set_value_width(max(self._minimum_value_width, width))
        else:
            self._fit_value_column()

    def _fit_value_column(self) -> None:
        if not self._auto_value_width:
            return
        other_width = sum(
            self.columnWidth(column)
            for column in range(self.columnCount())
            if column != 2
        )
        self._set_value_width(
            max(self._minimum_value_width, self.viewport().width() - other_width)
        )

    def _set_value_width(self, width: int) -> None:
        self._resizing_value = True
        try:
            self.setColumnWidth(2, width)
        finally:
            self._resizing_value = False

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._fit_value_column()

    def commit_active_editor(self) -> None:
        if self._committing:
            return
        editor = self.viewport().focusWidget()
        if editor is None or editor is self.viewport():
            return
        # Embedded checkboxes and file pickers are not delegate editors.
        if editor.parentWidget() is not self.viewport():
            return
        self._committing = True
        try:
            self.commitData(editor)
        finally:
            self._committing = False


class FormDataTable(QWidget):
    """Multipart fields with explicit row ordering, type and file ownership."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._fields: list[FormField] = []
        self._read_only = False
        self._loading = False
        self.tools = ToolWidget(self)
        self.add_button = TransparentTooltipButton(FluentIcon.ADD, self.tools)
        self.add_button.setToolTip(self.tr("新增字段"))
        self.add_button.clicked.connect(lambda: self.add_field())
        self.tools.right_layout.addWidget(self.add_button)
        self.table = _FormTable(self)
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            [self.tr("启用"), self.tr("名称"), self.tr("值"), self.tr("类型"), ""]
        )
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        enabled_width = max(
            64, self.table.fontMetrics().horizontalAdvance(self.tr("启用")) + 16
        )
        for column, width in ((0, enabled_width), (1, 120), (3, 64), (4, 36)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(column, width)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.stretch_value_column()
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._edit_multiline_cell)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.tools)
        layout.addWidget(self.table, 1)
        self._apply_read_only()

    def set_fields(self, fields: Sequence[FormField]) -> None:
        self._fields = list(fields)
        self._render()

    def fields(self) -> list[FormField]:
        self.commit_active_editor()
        return list(self._fields)

    def commit_active_editor(self) -> None:
        self.table.commit_active_editor()

    def add_field(self, field: FormField | None = None) -> None:
        self.commit_active_editor()
        self._fields.append(field if field is not None else FormField())
        self._render()
        self.table.setCurrentCell(len(self._fields) - 1, 1)
        self.changed.emit()
        if field is None and not self._read_only:
            item = self.table.item(len(self._fields) - 1, 1)
            if item is not None:
                self.table.editItem(item)

    def _render(self) -> None:
        self._loading = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(0)
            self.table.setRowCount(len(self._fields))
            for row, field in enumerate(self._fields):
                check = CheckBox(self.table)
                check.setChecked(field.enabled)
                check.setEnabled(not self._read_only)
                check.toggled.connect(
                    lambda checked, row=row: self._set_enabled(row, checked)
                )
                holder = QWidget(self.table)
                holder_layout = QHBoxLayout(holder)
                holder_layout.setContentsMargins(10, 0, 0, 0)
                holder_layout.addWidget(check)
                self.table.setCellWidget(row, 0, holder)
                self.table.setItem(row, 1, QTableWidgetItem(field.name))
                if field.kind == "file":
                    picker = BodyFilePicker(self.table, compact=True)
                    picker.set_file(
                        field.file_path,
                        filename=field.filename,
                        payload=field.payload,
                    )
                    picker.set_read_only(self._read_only)
                    picker.changed.connect(
                        lambda row=row, picker=picker: self._set_file(row, picker)
                    )
                    self.table.setCellWidget(row, 2, picker)
                else:
                    self.table.setItem(row, 2, QTableWidgetItem(field.value))
                kind = QTableWidgetItem(
                    self.tr("文件") if field.kind == "file" else self.tr("文本")
                )
                kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, 3, kind)
                more = TransparentTooltipButton(FluentIcon.MORE, self.table)
                more.setToolTip(self.tr("字段操作"))
                more.setEnabled(not self._read_only)
                more.clicked.connect(
                    lambda _checked=False, row=row, more=more: self._show_menu(
                        row, more
                    )
                )
                self.table.setCellWidget(row, 4, more)
        finally:
            self.table.setUpdatesEnabled(True)
            self._loading = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading or item.row() >= len(self._fields):
            return
        row, column = item.row(), item.column()
        field = self._fields[row]
        if column == 1:
            updated = replace(field, name=item.text())
        elif column == 2 and field.kind == "text":
            updated = replace(field, value=item.text())
        else:
            return
        if updated != field:
            self._fields[row] = updated
            self.changed.emit()

    def _set_enabled(self, row: int, enabled: bool) -> None:
        self._fields[row] = replace(self._fields[row], enabled=enabled)
        self.changed.emit()

    def _set_file(self, row: int, picker: BodyFilePicker) -> None:
        picked = picker.file()
        self._fields[row] = replace(
            self._fields[row],
            file_path=picked.file_path,
            filename=picked.filename,
            payload=picked.payload,
        )
        self.changed.emit()

    def _change_kind(self, row: int, kind: str) -> None:
        self.commit_active_editor()
        field = self._fields[row]
        if kind == field.kind:
            return
        # Retain the other representation in memory when switching back.
        self._fields[row] = replace(field, kind="file" if kind == "file" else "text")
        self._render()
        self.changed.emit()

    def _move_field(self, row: int, offset: int) -> None:
        self.commit_active_editor()
        target = row + offset
        if not 0 <= target < len(self._fields):
            return
        self._fields[row], self._fields[target] = (
            self._fields[target],
            self._fields[row],
        )
        self._render()
        self.table.setCurrentCell(target, 1)
        self.changed.emit()

    def _remove_field(self, row: int) -> None:
        self.commit_active_editor()
        del self._fields[row]
        self._render()
        self.changed.emit()

    def _edit_multiline_cell(self, row: int, column: int) -> None:
        field = self._fields[row]
        if column == 2 and field.kind == "text" and "\n" in field.value:
            self._edit_value(row)

    def _edit_value(self, row: int) -> None:
        if self._read_only or self._fields[row].kind != "text":
            return
        self.commit_active_editor()
        original = self._fields[row].value
        dialog = _FieldTextDialog(original, self.window())
        if dialog.exec() and dialog.text() != original:
            self._fields[row] = replace(self._fields[row], value=dialog.text())
            self._render()
            self.changed.emit()
        dialog.deleteLater()

    def _show_menu(self, row: int, button: QWidget) -> None:
        self.commit_active_editor()
        menu = RoundMenu(parent=self)
        menu.closedSignal.connect(menu.deleteLater)
        text = BaseAction(FluentIcon.EDIT, self.tr("文本"), parent=menu)
        text.triggered.connect(lambda: self._change_kind(row, "text"))
        menu.addAction(text)
        file = BaseAction(FluentIcon.FOLDER, self.tr("文件"), parent=menu)
        file.triggered.connect(lambda: self._change_kind(row, "file"))
        menu.addAction(file)
        edit = BaseAction(FluentIcon.EDIT, self.tr("编辑字段内容"), parent=menu)
        edit.setEnabled(self._fields[row].kind == "text")
        edit.triggered.connect(lambda: self._edit_value(row))
        menu.addAction(edit)
        menu.addSeparator()
        up = BaseAction(FluentIcon.UP, self.tr("上移"), parent=menu)
        up.setEnabled(row > 0)
        up.triggered.connect(lambda: self._move_field(row, -1))
        menu.addAction(up)
        down = BaseAction(FluentIcon.DOWN, self.tr("下移"), parent=menu)
        down.setEnabled(row + 1 < len(self._fields))
        down.triggered.connect(lambda: self._move_field(row, 1))
        menu.addAction(down)
        remove = BaseAction(FluentIcon.DELETE, self.tr("删除"), parent=menu)
        remove.triggered.connect(lambda: self._remove_field(row))
        menu.addAction(remove)
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def set_read_only(self, read_only: bool) -> None:
        self.commit_active_editor()
        self._read_only = read_only
        self._apply_read_only()
        self._render()

    def _apply_read_only(self) -> None:
        self.add_button.setEnabled(not self._read_only)
        self.table.setEditTriggers(
            TableWidget.EditTrigger.NoEditTriggers
            if self._read_only
            else (
                TableWidget.EditTrigger.DoubleClicked
                | TableWidget.EditTrigger.SelectedClicked
            )
        )


__all__ = ["BodyFilePicker", "FormDataTable", "UrlencodedTable"]
