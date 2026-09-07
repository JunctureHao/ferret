import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.filter import (
    FILTER_FLAG_LOGICS,
    FILTER_LOGICS,
    MultiFilterManager,
)


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


class FlagFieldTests(unittest.TestCase):
    """`WebSocket` 这类字段在 flowfilter 里没有可比的值（原生 `~websocket` 不带参数）。

    界面得跟着变：逻辑换成 是 / 不是、输入框禁掉。留一个能打字但下游根本不读的框，
    是那种「筛选没生效但看不出为什么」的坑。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.manager = MultiFilterManager()
        self.row = self.manager._rows()[0]
        self.ws_index = self.row.field_box.findData("WebSocket")

    def tearDown(self) -> None:
        self.manager.deleteLater()
        self.app.processEvents()

    def logics(self) -> tuple:
        return tuple(item.userData for item in self.row.logic_box.items)

    def select_websocket(self) -> None:
        self.row.field_box.setCurrentIndex(self.ws_index)

    def test_the_field_is_offered_at_all(self) -> None:
        """少了这一项，用户根本没法把 WS 流量单独筛出来。"""
        self.assertGreaterEqual(self.ws_index, 0)

    def test_value_fields_keep_the_regular_logics(self) -> None:
        self.assertEqual(self.logics(), FILTER_LOGICS)
        self.assertTrue(self.row.value_input.isEnabled())

    def test_selecting_it_swaps_the_logic_set(self) -> None:
        self.select_websocket()
        self.assertEqual(self.logics(), FILTER_FLAG_LOGICS)
        self.assertEqual(self.row.logic_box.currentData(), "is")

    def test_selecting_it_disables_the_value_input(self) -> None:
        self.select_websocket()
        self.assertFalse(self.row.value_input.isEnabled())
        self.assertNotEqual(self.row.value_input.placeholderText(), "")

    def test_switching_back_restores_the_logics_and_the_input(self) -> None:
        self.select_websocket()
        self.row.field_box.setCurrentIndex(self.row.field_box.findData("URL"))
        self.assertEqual(self.logics(), FILTER_LOGICS)
        self.assertEqual(self.row.logic_box.currentData(), "contains")
        self.assertTrue(self.row.value_input.isEnabled())

    def test_switching_within_the_value_fields_keeps_the_chosen_logic(self) -> None:
        """URL → Body 不该把用户选好的「排除」偷偷打回「包含」。"""
        self.row.logic_box.setCurrentIndex(FILTER_LOGICS.index("excludes"))
        self.row.field_box.setCurrentIndex(self.row.field_box.findData("Body"))
        self.assertEqual(self.row.logic_box.currentData(), "excludes")

    def test_it_counts_as_active_without_any_text(self) -> None:
        self.select_websocket()
        self.assertEqual(self.manager.active_condition_count(), 1)
        self.assertEqual(
            self.manager.get_conditions(),
            [{"field": "WebSocket", "logic": "is", "value": ""}],
        )

    def test_unchecking_still_disables_it(self) -> None:
        self.select_websocket()
        self.row.check_box.setChecked(False)
        self.assertEqual(self.manager.active_condition_count(), 0)

    def test_leftover_text_is_still_reported_as_the_flag_field(self) -> None:
        """字段换过来之前框里可能留着字；条件的 field/logic 才是下游的依据。"""
        self.row.value_input.setText("stale")
        self.select_websocket()
        condition = self.manager.get_conditions()[0]
        self.assertEqual(condition["field"], "WebSocket")
        self.assertEqual(condition["logic"], "is")

    def test_changing_the_field_still_notifies(self) -> None:
        """换字段等于换了筛选条件，表格得跟着重算。"""
        changed = Mock()
        self.manager.conditionsChanged.connect(changed)
        self.select_websocket()
        changed.assert_called_once_with()

    def test_clear_all_resets_the_row_to_a_value_field(self) -> None:
        """清空之后那一行必须能正常打字 —— 输入框还禁着就等于面板坏了。"""
        self.select_websocket()
        self.manager.clear_conditions()
        row = self.manager._rows()[0]
        self.assertEqual(row.field_box.currentData(), "all")
        self.assertEqual(tuple(i.userData for i in row.logic_box.items), FILTER_LOGICS)
        self.assertTrue(row.value_input.isEnabled())
        self.assertEqual(self.manager.active_condition_count(), 0)


if __name__ == "__main__":
    unittest.main()
