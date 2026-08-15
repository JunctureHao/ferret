import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.filter import MultiFilterManager


class MultiFilterManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.manager = MultiFilterManager()
        self.manager.resize(720, 240)
        self.manager.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def test_only_last_row_exposes_add_button(self) -> None:
        self.manager.add_new_row()
        rows = self.manager._rows()
        self.assertTrue(rows[0].add_btn.isHidden())
        self.assertFalse(rows[1].add_btn.isHidden())

        while len(self.manager._rows()) < self.manager.MAX_ROWS:
            self.manager.add_new_row()
        rows = self.manager._rows()
        self.assertFalse(rows[-1].add_btn.isEnabled())

    def test_active_count_tracks_value_and_enabled_state(self) -> None:
        row = self.manager._rows()[0]
        row.value_input.setText("application/json")
        self.assertEqual(self.manager.active_condition_count(), 1)
        self.assertEqual(self.manager.summary_label.text(), "1 个有效条件")

        row.check_box.setChecked(False)
        self.assertEqual(self.manager.active_condition_count(), 0)

    def test_clear_restores_one_empty_row(self) -> None:
        self.manager.add_new_row()
        rows = self.manager._rows()
        rows[0].value_input.setText("one")
        rows[1].value_input.setText("two")

        self.manager.clear_conditions()
        self.assertEqual(self.manager.v_layout.count(), 1)
        self.assertEqual(self.manager.active_condition_count(), 0)
        self.assertEqual(self.manager._rows()[0].value_input.text(), "")

    def test_collapse_does_not_clear_conditions(self) -> None:
        closed = Mock()
        self.manager.panelCloseRequested.connect(closed)
        self.manager._rows()[0].value_input.setText("keep-me")

        self.manager.close_btn.click()
        closed.assert_called_once_with()
        self.assertEqual(self.manager.active_condition_count(), 1)

    def test_input_keeps_minimum_width(self) -> None:
        self.assertGreaterEqual(self.manager._rows()[0].value_input.minimumWidth(), 160)


if __name__ == "__main__":
    unittest.main()
