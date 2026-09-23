import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.filter import MultiFilterManager


class ExpressionPanelTests(unittest.TestCase):
    """单一 flowfilter 表达式模型：编辑器是唯一事实源。"""

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

    def test_raw_expression_round_trips(self) -> None:
        self.manager.expression_input.setText('~u "api/.*"')
        self.assertEqual(self.manager.get_raw_expression(), '~u "api/.*"')

    def test_empty_expression_means_no_active_filter(self) -> None:
        self.assertFalse(self.manager.has_active_filter())
        self.assertEqual(self.manager.active_condition_count(), 0)
        self.assertEqual(self.manager.get_conditions(), [])

    def test_a_typed_expression_activates_the_filter(self) -> None:
        self.manager.expression_input.setText("~m GET")
        self.assertTrue(self.manager.has_active_filter())
        self.assertEqual(self.manager.active_condition_count(), 1)

    def test_clear_empties_the_expression(self) -> None:
        self.manager.expression_input.setText("~m GET")
        self.manager.expression_input.clear()
        self.assertEqual(self.manager.get_raw_expression(), "")
        self.assertFalse(self.manager.has_active_filter())

    def test_change_notifies_after_debounce(self) -> None:
        changed = Mock()
        self.manager.conditionsChanged.connect(changed)
        self.manager.expression_input.setText("~m GET")
        changed.assert_not_called()  # debounce 还没走完
        self.manager._debounce.timeout.emit()
        changed.assert_called_once_with()

    def test_highlight_mode_reflects_the_checkbox(self) -> None:
        self.assertFalse(self.manager.is_highlight_mode())
        self.manager.highlight_check.setChecked(True)
        self.assertTrue(self.manager.is_highlight_mode())

    def test_toggling_highlight_notifies_immediately_without_the_debounce(self) -> None:
        """过滤↔高亮是两条不同下发路径，切换立即重算 —— 不等 200ms debounce。"""
        changed = Mock()
        self.manager.conditionsChanged.connect(changed)
        self.manager.highlight_check.setChecked(True)
        changed.assert_called_once_with()

    def test_clearing_the_expression_leaves_highlight_mode_untouched(self) -> None:
        """清表达式不复位开关：用户攒好的「高亮而非过滤」意图不该被一次清空吞掉。"""
        self.manager.highlight_check.setChecked(True)
        self.manager.expression_input.setText("~m GET")
        self.manager.expression_input.clear()
        self.assertEqual(self.manager.get_raw_expression(), "")
        self.assertTrue(self.manager.is_highlight_mode())

    def test_error_state_round_trips(self) -> None:
        self.manager.set_raw_error("Expected & or |, found 'x'")
        self.assertIn("Expected", self.manager.expression_input.toolTip())
        self.assertTrue(self.manager.error_label.isVisible())
        self.manager.set_raw_error("")
        self.assertEqual(self.manager.expression_input.toolTip(), "")
        self.assertFalse(self.manager.error_label.isVisible())

    def test_collapse_does_not_clear_the_expression(self) -> None:
        closed = Mock()
        self.manager.panelCloseRequested.connect(closed)
        self.manager.expression_input.setText("keep-me")
        self.manager.close_btn.click()
        closed.assert_called_once_with()
        self.assertEqual(self.manager.get_raw_expression(), "keep-me")


class TokenInsertionTests(unittest.TestCase):
    """下拉菜单与快捷 chip 只在光标处单向插入 token。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.manager = MultiFilterManager()
        self.manager.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.manager.close()
        self.manager.deleteLater()
        self.app.processEvents()

    def test_insert_into_empty_editor_has_no_leading_connector(self) -> None:
        self.manager._insert_token("~websocket")
        self.assertEqual(self.manager.get_raw_expression(), "~websocket")

    def test_insert_into_nonempty_editor_adds_an_and_connector(self) -> None:
        self.manager.expression_input.setText("~m GET")
        self.manager.expression_input.setCursorPosition(6)
        self.manager._insert_token("~websocket")
        self.assertEqual(self.manager.get_raw_expression(), "~m GET & ~websocket")

    def test_value_token_parks_cursor_inside_quotes(self) -> None:
        self.manager._insert_token('~u ""', -1)
        editor = self.manager.expression_input
        self.assertEqual(editor.text(), '~u ""')
        # 光标停在两个引号中间，续打即写进引号内。
        editor.insert("api")
        self.assertEqual(editor.text(), '~u "api"')

    def test_operator_insert_pads_with_spaces(self) -> None:
        self.manager.expression_input.setText("~m GET")
        self.manager.expression_input.setCursorPosition(6)
        self.manager._insert_operator("|")
        self.assertEqual(self.manager.get_raw_expression(), "~m GET |")


if __name__ == "__main__":
    unittest.main()
