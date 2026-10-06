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
