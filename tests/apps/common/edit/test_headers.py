from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ferret.apps.common.edit.headers import HeaderDualPanel

app = QApplication.instance() or QApplication([])


class HeaderHistoryTests(unittest.TestCase):
    def make_panel(self, *, editable=True):
        panel = HeaderDualPanel(editable)
        self.addCleanup(panel.deleteLater)
        original = [
            ("X", value.decode("utf-8", "surrogateescape"))
            for value in (b"caf\xe9", b"caf\xff")
        ]
        panel.set_items(original)
        return panel, original

    def test_undo_and_redo_restore_the_deleted_duplicate_identity(self):
        panel, original = self.make_panel()
        editor = panel.text.code_widget
        panel.show()
        editor.setFocus()
        app.processEvents()
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        cursor.movePosition(
            QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor
        )
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        self.assertEqual(panel.items(), original[1:])
        QTest.keyClick(editor, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        QTest.keyRelease(editor, Qt.Key.Key_Control)
        self.assertEqual(panel.items(), original)
        editor.redo()
        self.assertEqual(panel.items(), original[1:])
        editor.undo()
        self.assertEqual(panel.items(), original)

    def test_loading_is_the_undo_baseline(self):
        panel, original = self.make_panel()
        editor = panel.text.code_widget
        self.assertFalse(editor.document().isUndoAvailable())
        editor.undo()
        self.assertEqual(panel.items(), original)

    def test_read_only_sort_reset_preserves_original_byte_identities(self):
        panel, original = self.make_panel(editable=False)
        panel._btn_table.click()
        self.assertFalse(panel.table.sort_order_button.isHidden())
        for _ in range(3):
            panel.table.sort_order_button.click()
        self.assertEqual(panel.items(), original)

    def test_undo_restores_a_value_ending_in_an_astral_character(self):
        panel, _original = self.make_panel()
        original = [
            ("X", ("value 😀".encode() + b"\xff").decode("utf-8", "surrogateescape"))
        ]
        panel.set_items(original)
        editor = panel.text.code_widget
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.deletePreviousChar()
        editor.undo()
        self.assertEqual(panel.items(), original)


class ManagedHeaderTests(unittest.TestCase):
    def make_panel(self, *, table=False, items=()):
        panel = HeaderDualPanel(True)
        self.addCleanup(panel.deleteLater)
        panel.set_items(items)
        if table:
            panel._show_table_page()
        return panel

    def append_user_header(self, panel, name, value):
        if panel.stack.currentWidget() is panel.table:
            panel.table._table_widget.add_row(name, value)
        else:
            cursor = panel.text.code_widget.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            if cursor.block().text():
                cursor.insertBlock()
            cursor.insertText(f"{name}: {value}")

    def test_owned_content_type_follows_the_selected_kind(self):
        for table in (False, True):
            with self.subTest(table=table):
                panel = self.make_panel(table=table)
                self.assertFalse(panel.is_managed_header("Content-Type"))
                self.assertTrue(
                    panel.set_managed_header("Content-Type", "application/json")
                )
                self.assertTrue(panel.is_managed_header("content-type"))
                self.assertTrue(panel.set_managed_header("content-type", "text/plain"))
                self.assertEqual(panel.items(), [("Content-Type", "text/plain")])
                self.assertTrue(panel.set_managed_header("Content-Type", None))
                self.assertEqual(panel.items(), [])
                self.assertFalse(panel.is_managed_header("Content-Type"))

    def test_existing_manual_header_takes_priority_even_at_the_default_value(self):
        for table in (False, True):
            with self.subTest(table=table):
                original = [("cOnTeNt-TyPe", "application/json")]
                panel = self.make_panel(table=table, items=original)
                self.assertFalse(panel.set_managed_header("Content-Type", "text/plain"))
                self.assertFalse(panel.set_managed_header("Content-Type", None))
                self.assertEqual(panel.items(), original)

    def test_replacing_a_deleted_automatic_row_with_identical_text_is_manual(self):
        for table in (False, True):
            with self.subTest(table=table):
                panel = self.make_panel(table=table)
                panel.set_managed_header("Content-Type", "application/json")
                if table:
                    widget = panel.table._table_widget
                    widget.selectRow(0)
                    widget.remove_selected_rows()
                else:
                    cursor = panel.text.code_widget.textCursor()
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.removeSelectedText()
                self.append_user_header(panel, "Content-Type", "application/json")
                self.assertFalse(panel.is_managed_header("Content-Type"))
                self.assertFalse(panel.set_managed_header("Content-Type", None))
                self.assertEqual(panel.items(), [("Content-Type", "application/json")])

    def test_editing_an_automatic_value_and_typing_it_back_relinquishes_ownership(self):
        for table in (False, True):
            with self.subTest(table=table):
                panel = self.make_panel(table=table)
                panel.set_managed_header("Content-Type", "application/json")
                if table:
                    item = panel.table._table_widget.item(0, 1)
                    item.setText("text/plain")
                    item.setText("application/json")
                else:
                    cursor = panel.text.code_widget.textCursor()
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertText("x")
                    cursor.deletePreviousChar()
                self.assertFalse(panel.is_managed_header("Content-Type"))
                self.assertFalse(panel.set_managed_header("Content-Type", None))
                self.assertEqual(panel.items(), [("Content-Type", "application/json")])

    def test_a_manual_duplicate_blocks_updates_but_only_the_owned_row_is_removed(self):
        for table in (False, True):
            with self.subTest(table=table):
                panel = self.make_panel(table=table)
                panel.set_managed_header("Content-Type", "application/json")
                self.append_user_header(panel, "content-type", "application/json")
                self.assertFalse(panel.is_managed_header("Content-Type"))
                self.assertFalse(panel.set_managed_header("Content-Type", "text/plain"))
                self.assertTrue(panel.set_managed_header("Content-Type", None))
                self.assertEqual(panel.items(), [("content-type", "application/json")])

    def test_editing_another_header_keeps_the_automatic_header_owned(self):
        for table in (False, True):
            with self.subTest(table=table):
                panel = self.make_panel(table=table, items=[("X-Other", "old")])
                panel.set_managed_header("Content-Type", "application/json")
                if table:
                    panel.table._table_widget.item(0, 1).setText("new")
                else:
                    cursor = panel.text.code_widget.textCursor()
                    cursor.movePosition(QTextCursor.MoveOperation.Start)
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                    cursor.insertText("-new")
                self.assertTrue(panel.is_managed_header("Content-Type"))
                self.assertTrue(panel.set_managed_header("Content-Type", "text/plain"))
                self.assertEqual(panel.items()[-1], ("Content-Type", "text/plain"))

    def test_dual_switches_preserve_ownership_and_external_load_clears_it(self):
        panel = self.make_panel()
        panel.set_managed_header("Content-Type", "application/json")
        for switch in (panel._show_table_page, panel._show_text_page):
            switch()
            self.assertTrue(panel.is_managed_header("Content-Type"))
        panel.set_items(panel.items())
        self.assertFalse(panel.is_managed_header("Content-Type"))
        self.assertFalse(panel.set_managed_header("Content-Type", None))

    def test_managed_changes_preserve_unrelated_non_utf8_header_bytes(self):
        for table in (False, True):
            with self.subTest(table=table):
                original = [
                    ("X", raw.decode("utf-8", "surrogateescape"))
                    for raw in (b"caf\xe9", b"caf\xff")
                ]
                panel = self.make_panel(table=table, items=original)
                panel.set_managed_header("Content-Type", "application/json")
                panel.set_managed_header("Content-Type", "text/plain")
                self.assertEqual(panel.items()[:-1], original)
                panel._show_text_page() if table else panel._show_table_page()
                self.assertTrue(panel.is_managed_header("Content-Type"))
                panel.set_managed_header("Content-Type", None)
                self.assertEqual(panel.items(), original)

    def test_text_automatic_changes_are_undoable_without_erasing_prior_edits(self):
        panel = self.make_panel(items=[("X-Other", "old")])
        editor = panel.text.code_widget
        cursor = editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("-new")
        panel.set_managed_header("Content-Type", "application/json")
        editor.undo()
        self.assertEqual(panel.items(), [("X-Other", "old-new")])
        editor.undo()
        self.assertEqual(panel.items(), [("X-Other", "old")])

    def test_text_removal_of_last_owned_header_keeps_surrounding_rows(self):
        panel = self.make_panel(items=[("X-Other", "keep")])
        panel.set_managed_header("Content-Type", "application/json")
        panel.set_managed_header("Content-Type", None)
        self.assertEqual(panel.items(), [("X-Other", "keep")])
        panel.text.code_widget.undo()
        self.assertEqual(
            panel.items(),
            [("X-Other", "keep"), ("Content-Type", "application/json")],
        )
