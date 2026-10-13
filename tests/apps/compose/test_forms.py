"""Regression coverage for lossless fields and pending delegate edits."""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication, QLineEdit
from qfluentwidgets import CheckBox

from ferret.apps.compose.forms import (
    BodyFilePicker,
    FormDataTable,
    UrlencodedTable,
    _FieldTextDialog,
)
from ferret.core import resources_rc  # noqa: F401
from ferret.core.mitm import FormField

app = QApplication.instance() or QApplication([])


class FormWidgetTests(unittest.TestCase):
    def make_widget(self, cls):
        widget = cls()
        self.addCleanup(app.processEvents)
        self.addCleanup(widget.deleteLater)
        return widget

    def open_cell_editor(self, table, row: int, column: int) -> QLineEdit:
        table.window().show()
        table.setCurrentCell(row, column)
        item = table.item(row, column)
        self.assertIsNotNone(item)
        table.editItem(item)
        app.processEvents()
        editor = table.viewport().focusWidget()
        self.assertIsInstance(editor, QLineEdit)
        assert isinstance(editor, QLineEdit)
        return editor


class UrlencodedTableTests(FormWidgetTests):
    def test_loaded_fields_keep_empty_names_spaces_breaks_and_duplicates(self):
        table = self.make_widget(UrlencodedTable)
        changes = []
        table.changed.connect(lambda: changes.append(table.items()))
        pairs = [("", ""), (" x: ", " a\r\nb "), ("x", "1"), ("x", "2")]
        table.set_items(pairs)
        self.assertEqual(table.items(), pairs)
        self.assertEqual(changes, [])
        table._table_widget.item(2, 1).setText("new")
        self.assertEqual(table.items(), pairs[:2] + [("x", "new"), ("x", "2")])

    def test_new_placeholder_is_ignored_but_cleared_edited_field_is_kept(self):
        table = self.make_widget(UrlencodedTable)
        table.set_items([("", "")])
        table.add_row()
        self.assertEqual(table.items(), [("", "")])
        table._table_widget.item(1, 1).setText("x")
        table._table_widget.item(1, 1).setText("")
        self.assertEqual(table.items(), [("", ""), ("", "")])

    def test_items_commit_the_active_editor_before_reading(self):
        table = self.make_widget(UrlencodedTable)
        table.set_items([("key", "old")])
        editor = self.open_cell_editor(table._table_widget, 0, 1)
        editor.setText(" new ")
        # Read from a change handler too: commit must be reentrancy-safe.
        changes = []
        table.changed.connect(lambda: changes.append(table.items()))
        self.assertEqual(table.items(), [("key", " new ")])
        self.assertEqual(changes, [[("key", " new ")]])

    def test_removing_a_loaded_empty_field_does_not_restore_it(self):
        table = self.make_widget(UrlencodedTable)
        table.set_items([("", ""), ("x", "y")])
        table._table_widget.selectRow(0)
        table.remove_selected_rows()
        self.assertEqual(table.items(), [("x", "y")])


class FormDataTableTests(FormWidgetTests):
    def test_narrow_english_table_keeps_values_readable_and_manual_widths(self):
        translator = QTranslator()
        self.assertTrue(translator.load(":/i18n/en_GB.qm"))
        app.installTranslator(translator)
        try:
            table = self.make_widget(FormDataTable)
        finally:
            app.removeTranslator(translator)
        table.resize(412, 300)
        table.set_fields([FormField(name="attachment", kind="file", payload=b"")])
        table.show()
        app.processEvents()
        header = table.table.horizontalHeaderItem(0)
        self.assertEqual(header.text(), "Enabled")
        self.assertGreaterEqual(
            table.table.columnWidth(0),
            table.table.fontMetrics().horizontalAdvance(header.text()) + 16,
        )
        self.assertGreaterEqual(table.table.columnWidth(2), 140)
        self.assertGreater(table.table.horizontalScrollBar().maximum(), 0)
        table.table.setColumnWidth(1, 150)
        table.table.setColumnWidth(2, 200)
        table.resize(700, 300)
        app.processEvents()
        self.assertEqual(table.table.columnWidth(1), 150)
        self.assertEqual(table.table.columnWidth(2), 200)

    def test_loading_keeps_original_fields_and_emits_no_change(self):
        table = self.make_widget(FormDataTable)
        changes = []
        table.changed.connect(lambda: changes.append(table.fields()))
        fields = [
            FormField(name="", value=" a\r\nb ", enabled=False),
            FormField(
                name="upload",
                kind="file",
                filename="zero.bin",
                content_type="application/octet-stream",
                payload=b"",
            ),
        ]
        table.set_fields(fields)
        self.assertEqual(table.fields(), fields)
        self.assertEqual(changes, [])
        self.assertIsNot(table.fields(), fields)

    def test_read_commits_pending_name_without_changing_payload(self):
        table = self.make_widget(FormDataTable)
        field = FormField(kind="file", name="before", filename="a", payload=b"\xff")
        table.set_fields([field])
        editor = self.open_cell_editor(table.table, 0, 1)
        editor.setText(" after ")
        changes = []
        table.changed.connect(lambda: changes.append(table.fields()))
        expected = [replace(field, name=" after ")]
        self.assertEqual(table.fields(), expected)
        self.assertEqual(changes, [expected])

    def test_read_commits_pending_text_value(self):
        table = self.make_widget(FormDataTable)
        table.set_fields([FormField(name="a", value="before")])
        editor = self.open_cell_editor(table.table, 0, 2)
        editor.setText(" after ")
        self.assertEqual(table.fields(), [FormField(name="a", value=" after ")])

    def test_moving_a_row_commits_its_pending_value_first(self):
        table = self.make_widget(FormDataTable)
        first = FormField(name="same", value="first")
        second = FormField(name="same", value="second")
        table.set_fields([first, second])
        editor = self.open_cell_editor(table.table, 0, 2)
        editor.setText("edited")
        table._move_field(0, 1)
        self.assertEqual(table.fields(), [second, replace(first, value="edited")])

    def test_enable_reorder_and_remove_keep_field_identity(self):
        table = self.make_widget(FormDataTable)
        fields = [FormField(name="x", value="1"), FormField(name="x", value="2")]
        table.set_fields(fields)
        check = table.table.cellWidget(0, 0).findChild(CheckBox)
        check.setChecked(False)
        table._move_field(0, 1)
        self.assertEqual(table.fields(), [fields[1], replace(fields[0], enabled=False)])
        table._remove_field(0)
        self.assertEqual(table.fields(), [replace(fields[0], enabled=False)])

    def test_switching_field_type_preserves_previous_values_in_memory(self):
        table = self.make_widget(FormDataTable)
        original = FormField(name="field", value="keep\r\nthis")
        table.set_fields([original])
        table._change_kind(0, "file")
        picker = table.table.cellWidget(0, 2)
        self.assertIsInstance(picker, BodyFilePicker)
        picker.set_file(filename="captured.bin", payload=b"\xff")
        picker.changed.emit()
        table._change_kind(0, "text")
        self.assertEqual(table.fields()[0].value, original.value)
        table._change_kind(0, "file")
        self.assertEqual(table.fields()[0].payload, b"\xff")

    def test_choosing_file_replaces_imported_memory_and_keeps_other_metadata(self):
        table = self.make_widget(FormDataTable)
        field = FormField(
            name="upload",
            kind="file",
            filename="old.bin",
            payload=b"old",
            enabled=False,
        )
        table.set_fields([field])
        picker = table.table.cellWidget(0, 2)
        with patch(
            "ferret.apps.compose.forms.QFileDialog.getOpenFileName",
            return_value=("C:/not-read/new.bin", ""),
        ):
            picker.choose_button.click()
        self.assertEqual(
            table.fields(),
            [
                replace(
                    field,
                    file_path="C:/not-read/new.bin",
                    filename="new.bin",
                    payload=None,
                )
            ],
        )

    def test_read_only_locks_checkboxes_file_picker_and_row_actions(self):
        table = self.make_widget(FormDataTable)
        fields = [FormField(kind="file", filename="captured", payload=b"")]
        table.set_fields(fields)
        table.set_read_only(True)
        self.assertFalse(table.add_button.isEnabled())
        self.assertFalse(table.table.cellWidget(0, 0).findChild(CheckBox).isEnabled())
        self.assertFalse(table.table.cellWidget(0, 2).choose_button.isEnabled())
        self.assertFalse(table.table.cellWidget(0, 4).isEnabled())
        self.assertEqual(table.fields(), fields)


class BodyFilePickerTests(FormWidgetTests):
    def test_empty_memory_file_is_distinct_from_no_file(self):
        picker = self.make_widget(BodyFilePicker)
        changes = []
        picker.changed.connect(lambda: changes.append(picker.file()))
        no_file = picker.filename_label.text()
        picker.set_file(filename="empty.bin", payload=b"")
        self.assertEqual(picker.file().payload, b"")
        self.assertNotEqual(picker.filename_label.text(), no_file)
        self.assertEqual(changes, [])
        picker.clear_button.click()
        self.assertIsNone(picker.file().payload)
        self.assertEqual(picker.filename_label.text(), no_file)
        self.assertEqual(len(changes), 1)

    def test_cancel_keeps_the_existing_file(self):
        picker = self.make_widget(BodyFilePicker)
        picker.set_file(filename="memory", payload=b"body")
        field = picker.file()
        changes = []
        picker.changed.connect(lambda: changes.append(picker.file()))
        with patch(
            "ferret.apps.compose.forms.QFileDialog.getOpenFileName",
            return_value=("", ""),
        ):
            picker.choose_button.click()
        self.assertEqual(picker.file(), field)
        self.assertEqual(changes, [])


class FieldTextDialogTests(FormWidgetTests):
    def test_saving_without_edits_keeps_crlf_in_the_original_field(self):
        parent = self.make_widget(UrlencodedTable)
        dialog = _FieldTextDialog("line 1\r\nline 2", parent)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.text(), "line 1\r\nline 2")
        dialog.edit.code_widget.setPlainText("changed\nvalue")
        self.assertEqual(dialog.text(), "changed\nvalue")
