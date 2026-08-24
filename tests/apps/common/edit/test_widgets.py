"""``edit/widgets.py`` 的编辑能力测试。

这一层原本是**只读**的：表格收 dict、读回时优先吃一份 `_items` 缓存，文本框既没有
``text()`` 也不发 ``changed``。断点页要在同一套控件上改头改体，于是补了三件事，每件
都有一个很容易回归的坑，这里逐条锁住：

* 表格是唯一真相 —— 用户改过的单元格必须能读回来（旧缓存连"复制"都复制旧值）；
* 键值对内部一律用**有序列表** —— 重名的 ``Set-Cookie`` 用 dict 存会被挤掉一条；
* ``set_text`` / ``set_rows`` 是程序化换内容，**不得**发 ``changed`` ——
  否则控制器一填表就以为用户动过手。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidget

from ferret.apps.common.edit.syntax import Language
from ferret.apps.common.edit.widgets import (
    ItemDualPanel,
    ItemTableToolWidget,
    ItemTableWidget,
    JsonDualPanel,
    SortState,
    ToolPlainTextEdit,
    items_from_text,
    items_to_text,
    normalize_items,
)

app = QApplication.instance() or QApplication([])

# 一对重名头：整个"有序列表而不是 dict"的取舍就是为它们做的。
DUPLICATE = [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]


class NormalizeItemsTests(unittest.TestCase):
    def test_a_mapping_keeps_its_insertion_order(self) -> None:
        self.assertEqual(
            normalize_items({"B": "2", "A": "1"}), [("B", "2"), ("A", "1")]
        )

    def test_a_pair_sequence_keeps_duplicate_keys(self) -> None:
        """dict 会把这两条压成一条，这正是不用 dict 的原因。"""
        self.assertEqual(normalize_items(DUPLICATE), DUPLICATE)

    def test_nothing_becomes_an_empty_list(self) -> None:
        self.assertEqual(normalize_items(None), [])
        self.assertEqual(normalize_items({}), [])
        self.assertEqual(normalize_items([]), [])

    def test_bytes_are_decoded_and_others_stringified(self) -> None:
        self.assertEqual(
            normalize_items([(b"X-Id", b"7"), ("X-Len", 12)]),
            [("X-Id", "7"), ("X-Len", "12")],
        )

    def test_undecodable_bytes_do_not_raise(self) -> None:
        """头值可以是任意字节；这里只负责显示，不许因为一个字节就炸掉。"""
        self.assertEqual(normalize_items([("X", b"\xff")]), [("X", "�")])

    def test_a_none_value_becomes_an_empty_cell(self) -> None:
        self.assertEqual(normalize_items([("X", None)]), [("X", "")])


class ItemTextRoundTripTests(unittest.TestCase):
    def test_text_and_pairs_round_trip(self) -> None:
        self.assertEqual(items_from_text(items_to_text(DUPLICATE)), DUPLICATE)

    def test_only_the_first_colon_splits(self) -> None:
        """``Date`` 的值里就带冒号，切到第二个冒号会把日期切断。"""
        self.assertEqual(
            items_from_text("Date: Mon, 01 Jan 2024 00:00:00 GMT"),
            [("Date", "Mon, 01 Jan 2024 00:00:00 GMT")],
        )

    def test_blank_lines_are_dropped(self) -> None:
        self.assertEqual(items_from_text("A: 1\n\n   \nB: 2"), [("A", "1"), ("B", "2")])

    def test_a_line_without_a_colon_is_all_key(self) -> None:
        """用户还在打字的那半行：留着键，值给空，别整行吃掉。"""
        self.assertEqual(items_from_text("X-Token"), [("X-Token", "")])

    def test_surrounding_spaces_are_trimmed(self) -> None:
        self.assertEqual(items_from_text("  A :  1  "), [("A", "1")])

    def test_an_empty_value_survives_the_round_trip(self) -> None:
        """空头值不是"没这个头"：重写页拿它表达"只删不加"。"""
        self.assertEqual(items_from_text(items_to_text([("A", "")])), [("A", "")])


class ItemTableWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = ItemTableWidget(True)
        self.addCleanup(self.table.deleteLater)
        self.seen: list[None] = []
        self.table.items_changed.connect(lambda: self.seen.append(None))

    def test_set_rows_fills_the_grid_without_claiming_a_user_edit(self) -> None:
        self.table.set_rows(DUPLICATE)
        self.assertEqual(self.table.rowCount(), 2)
        self.assertEqual(self.table.rows(), DUPLICATE)
        # 程序化填表期间每建一个单元格都会发 itemChanged，_loading 就是为它挡的。
        self.assertEqual(self.seen, [])

    def test_editing_a_cell_is_read_back_and_announced(self) -> None:
        self.table.set_rows([("A", "1")])
        item = self.table.item(0, 1)
        assert item is not None
        item.setText("2")
        self.assertEqual(self.table.rows(), [("A", "2")])
        self.assertEqual(len(self.seen), 1)

    def test_a_blank_key_row_is_not_read_back(self) -> None:
        """用户刚点"新增一行"、还没填键 —— 这行不该被当成一个真的头下发。"""
        self.table.set_rows([("", "1"), ("A", "2")])
        self.assertEqual(self.table.rows(), [("A", "2")])

    def test_add_row_appends_and_announces(self) -> None:
        self.table.set_rows([("A", "1")])
        self.assertEqual(self.table.add_row("B", "2"), 1)
        self.assertEqual(self.table.rows(), [("A", "1"), ("B", "2")])
        self.assertEqual(len(self.seen), 1)

    def test_remove_selected_rows_deletes_from_the_bottom_up(self) -> None:
        """倒序删除：先删第 0 行会把后面的行号整体挪上来。"""
        self.table.set_rows([("A", "1"), ("B", "2"), ("C", "3")])
        self.table.selectRow(0)
        self.table.selectionModel().select(
            self.table.model().index(2, 0),
            self.table.selectionModel().SelectionFlag.Select
            | self.table.selectionModel().SelectionFlag.Rows,
        )
        self.assertEqual(self.table.remove_selected_rows(), 2)
        self.assertEqual(self.table.rows(), [("B", "2")])

    def test_removing_nothing_changes_nothing(self) -> None:
        self.table.set_rows([("A", "1")])
        self.table.clearSelection()
        self.assertEqual(self.table.remove_selected_rows(), 0)
        self.assertEqual(self.seen, [])

    def test_read_only_takes_the_edit_triggers_away(self) -> None:
        self.assertTrue(self.table.editable)
        self.table.set_read_only(True)
        self.assertFalse(self.table.editable)
        self.assertEqual(
            self.table.editTriggers(), QTableWidget.EditTrigger.NoEditTriggers
        )
        self.table.set_read_only(False)
        self.assertTrue(
            self.table.editTriggers() & QTableWidget.EditTrigger.DoubleClicked
        )

    def test_a_long_cell_gets_a_tooltip(self) -> None:
        long_value = "x" * 40
        self.table.set_rows([("A", long_value)])
        item = self.table.item(0, 1)
        assert item is not None
        self.assertEqual(item.toolTip(), long_value)


class ItemTableToolWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = ItemTableToolWidget(True)
        self.addCleanup(self.panel.deleteLater)

    def test_items_come_from_the_grid_not_a_cache(self) -> None:
        """旧实现优先读 `_items` 缓存，用户改过的单元格对外完全不可见。"""
        self.panel.set_items([("A", "1")])
        item = self.panel._table_widget.item(0, 1)
        assert item is not None
        item.setText("2")
        self.assertEqual(self.panel.items(), [("A", "2")])

    def test_duplicate_keys_survive(self) -> None:
        self.panel.set_items(DUPLICATE)
        self.assertEqual(self.panel.items(), DUPLICATE)

    def test_edits_are_announced_upwards(self) -> None:
        seen: list[None] = []
        self.panel.items_changed.connect(lambda: seen.append(None))
        self.panel.set_items([("A", "1")])
        self.assertEqual(seen, [])
        self.panel.add_row()
        self.assertEqual(len(seen), 1)

    def test_add_and_remove_buttons_only_show_when_editable(self) -> None:
        """排序按钮反之：它的"原始顺序"档要重填表格，会吃掉改了一半的内容。"""
        self.assertTrue(self.panel.add_row_button.isVisibleTo(self.panel))
        self.assertFalse(self.panel.sort_order_button.isVisibleTo(self.panel))
        self.panel.set_read_only(True)
        self.assertFalse(self.panel.add_row_button.isVisibleTo(self.panel))
        self.assertTrue(self.panel.sort_order_button.isVisibleTo(self.panel))

    def test_sorting_can_get_back_to_the_original_order(self) -> None:
        self.panel.set_items([("B", "2"), ("A", "1")])
        self.panel.handle_sort_order_button_clicked()
        self.assertEqual(self.panel.items(), [("A", "1"), ("B", "2")])
        self.panel.handle_sort_order_button_clicked()
        self.assertEqual(self.panel.items(), [("B", "2"), ("A", "1")])
        self.panel.handle_sort_order_button_clicked()
        self.assertEqual(self.panel.items(), [("B", "2"), ("A", "1")])
        self.assertIs(self.panel._sort_state, SortState.ORIGINAL)

    def test_set_items_resets_the_sort_state(self) -> None:
        self.panel.set_items([("B", "2"), ("A", "1")])
        self.panel.handle_sort_order_button_clicked()
        self.panel.set_items([("D", "4"), ("C", "3")])
        self.assertIs(self.panel._sort_state, SortState.ORIGINAL)
        self.assertEqual(self.panel.items(), [("D", "4"), ("C", "3")])

    def test_json_copy_collapses_duplicate_keys_into_a_list(self) -> None:
        """JSON 对象不许重名键，而 HTTP 头许 —— 只能收敛成数组。"""
        self.panel.set_items([*DUPLICATE, ("Set-Cookie", "c=3"), ("A", "1")])
        self.panel.handle_copy_json_button_clicked()
        clipboard = QApplication.clipboard()
        assert clipboard is not None
        self.assertIn(
            '"Set-Cookie": [\n    "a=1",\n    "b=2",\n    "c=3"\n  ]', clipboard.text()
        )
        self.assertIn('"A": "1"', clipboard.text())

    def test_plain_copy_is_the_same_text_the_text_page_shows(self) -> None:
        self.panel.set_items(DUPLICATE)
        self.panel.handle_copy_plain_button_clicked()
        clipboard = QApplication.clipboard()
        assert clipboard is not None
        self.assertEqual(clipboard.text(), items_to_text(DUPLICATE))


class ToolPlainTextEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.edit = ToolPlainTextEdit()
        self.addCleanup(self.edit.deleteLater)
        self.seen: list[None] = []
        self.edit.changed.connect(lambda: self.seen.append(None))

    def test_set_text_is_not_a_user_edit(self) -> None:
        """控制器一填内容就发 changed，界面会立刻显示成"有未保存改动"。"""
        self.edit.set_text('{"a": 1}', Language.JSON)
        self.assertEqual(self.edit.text(), '{"a": 1}')
        self.assertEqual(self.seen, [])

    def test_typing_is_announced(self) -> None:
        """只断言"发了"，不断言发几次：上色本身还会再发一轮 textChanged。"""
        self.edit.set_text("body")
        self.edit.code_widget.setPlainText("changed")
        self.assertTrue(self.seen)
        self.assertEqual(self.edit.text(), "changed")

    def test_read_only_round_trips(self) -> None:
        self.assertFalse(self.edit.is_read_only())
        self.edit.set_read_only(True)
        self.assertTrue(self.edit.is_read_only())
        self.edit.set_read_only(False)
        self.assertFalse(self.edit.is_read_only())

    def test_set_text_clears_stale_search_hits(self) -> None:
        """查找结果记的是上一份文本里的游标，换文本后必须清掉。"""
        self.edit.set_text("aaa")
        self.edit.do_search("a")
        self.assertTrue(self.edit._search_results)
        self.edit.set_text("bbb")
        self.assertEqual(self.edit._search_results, [])
        self.assertEqual(self.edit._search_index, -1)


class ItemDualPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = ItemDualPanel(True)
        self.addCleanup(self.panel.deleteLater)

    def test_set_items_fills_both_pages(self) -> None:
        self.panel.set_items(DUPLICATE)
        self.assertEqual(self.panel.table.items(), DUPLICATE)
        self.assertEqual(self.panel.text.text(), items_to_text(DUPLICATE))

    def test_items_follow_the_page_on_show(self) -> None:
        """文本页手敲的行还没同步到表格，固定读表格就会读到旧值。"""
        self.panel.set_items([("A", "1")])
        self.panel.stack.setCurrentWidget(self.panel.text)
        self.panel.text.code_widget.setPlainText("B: 2")
        self.assertEqual(self.panel.items(), [("B", "2")])
        self.panel.stack.setCurrentWidget(self.panel.table)
        self.assertEqual(self.panel.items(), [("A", "1")])

    def test_switching_to_the_table_carries_the_typed_text(self) -> None:
        self.panel.set_items([("A", "1")])
        self.panel.stack.setCurrentWidget(self.panel.text)
        self.panel.text.code_widget.setPlainText("B: 2\nC: 3")
        self.panel._show_table_page()
        self.assertEqual(self.panel.items(), [("B", "2"), ("C", "3")])

    def test_switching_to_the_text_carries_the_edited_cells(self) -> None:
        self.panel.set_items([("A", "1")])
        self.panel._show_table_page()
        item = self.panel.table._table_widget.item(0, 1)
        assert item is not None
        item.setText("9")
        self.panel._show_text_page()
        self.assertEqual(self.panel.text.text(), "A: 9")
        self.assertEqual(self.panel.items(), [("A", "9")])

    def test_a_read_only_panel_does_not_carry_content_across(self) -> None:
        """只读页面切来切去不该改内容 —— 搬运只在可编辑时发生。"""
        panel = ItemDualPanel(False)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("A", "1")])
        panel._show_table_page()
        panel._show_text_page()
        self.assertEqual(panel.text.text(), "A: 1")

    def test_read_only_locks_both_pages(self) -> None:
        """只锁文本页会留下一个能改却读不出来的表格。"""
        self.panel.set_read_only(True)
        self.assertTrue(self.panel.text.is_read_only())
        self.assertFalse(self.panel.table.editable)
        self.panel.set_read_only(False)
        self.assertFalse(self.panel.text.is_read_only())
        self.assertTrue(self.panel.table.editable)

    def test_a_panel_starts_locked_when_not_editable(self) -> None:
        panel = ItemDualPanel()
        self.addCleanup(panel.deleteLater)
        self.assertTrue(panel.text.is_read_only())
        self.assertFalse(panel.table.editable)

    def test_changed_comes_from_either_page(self) -> None:
        seen: list[None] = []
        self.panel.changed.connect(lambda: seen.append(None))
        self.panel.set_items([("A", "1")])
        self.assertEqual(seen, [])
        self.panel.text.code_widget.setPlainText("A: 2")
        typed = len(seen)
        self.assertTrue(typed)
        self.panel.table.add_row()
        self.assertGreater(len(seen), typed)


class JsonDualPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = JsonDualPanel()
        self.addCleanup(self.panel.deleteLater)

    def test_plain_text_round_trips(self) -> None:
        self.panel.set_text('{"a": 1}')
        self.assertEqual(self.panel.plain_text(), '{"a": 1}')

    def test_the_tree_is_rebuilt_from_the_edited_text(self) -> None:
        """树是文本的派生视图；不重建就会显示上一份结构。"""
        self.panel.set_text('{"a": 1}')
        self.panel.text.code_widget.setPlainText('{"b": 2, "c": 3}')
        self.panel._show_tree_page()
        tree = self.panel.tree.tree
        self.assertEqual(tree.topLevelItemCount(), 2)
        first = tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "b")

    def test_broken_json_leaves_an_empty_tree(self) -> None:
        """body 常常是被截断的 JSON，不该因此炸掉。"""
        self.panel.set_text('{"a": 1')
        self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)
        self.assertEqual(self.panel.plain_text(), '{"a": 1')

    def test_a_non_json_language_has_no_tree(self) -> None:
        self.panel.set_text("plain body", Language.HTTP)
        self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)

    def test_typing_is_announced(self) -> None:
        seen: list[None] = []
        self.panel.changed.connect(lambda: seen.append(None))
        self.panel.set_text('{"a": 1}')
        self.assertEqual(seen, [])
        self.panel.text.code_widget.setPlainText('{"a": 2}')
        self.assertTrue(seen)


if __name__ == "__main__":
    unittest.main()
