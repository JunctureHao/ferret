"""``edit/widgets.py`` 的编辑能力测试。

这一层原本是**只读**的：表格收 dict、读回时优先吃一份 `_items` 缓存，文本框既没有
``text()`` 也不发 ``changed``。断点页要在同一套控件上改头改体，于是补了三件事，每件
都有一个很容易回归的坑，这里逐条锁住：

* 表格是唯一真相 —— 用户改过的单元格必须能读回来（旧缓存连"复制"都复制旧值）；
* 键值对内部一律用**有序列表** —— 重名的 ``Set-Cookie`` 用 dict 存会被挤掉一条；
* ``set_text`` / ``set_rows`` 是程序化换内容，**不得**发 ``changed`` ——
  否则控制器一填表就以为用户动过手。
"""

from __future__ import annotations

import json
import os
import typing
import unittest
import unittest.mock
from array import array

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication, QLineEdit, QTableWidget

from ferret.apps.common.edit.syntax import Language
from ferret.apps.common.edit.widgets import (
    JSON_TREE_BATCH_SIZE,
    JSON_TREE_DEPTH_LIMIT,
    JSON_TREE_NODE_LIMIT,
    SEARCH_HIGHLIGHT_LIMIT,
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
from tests.core.mitm._qt import wait_until

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
        self.assertFalse(self.edit._search_results)
        self.assertEqual(self.edit._search_index, -1)

    def test_many_search_hits_use_compact_positions_and_bounded_highlights(
        self,
    ) -> None:
        self.edit.resize(600, 400)
        self.edit.show()
        self.edit.set_text("a " * 20000)
        self.edit.open_search()
        self.edit.do_search("a")
        self.assertIsInstance(self.edit._search_results, array)
        self.assertEqual(len(self.edit._search_results), 20000)
        self.assertLessEqual(
            len(self.edit.code_widget.extraSelections()), SEARCH_HIGHLIGHT_LIMIT
        )
        self.edit.search_prev()
        self.assertEqual(self.edit._search_index, 19999)
        self.assertEqual(self.edit.code_widget.textCursor().selectionStart(), 39998)
        self.assertLessEqual(
            len(self.edit.code_widget.extraSelections()), SEARCH_HIGHLIGHT_LIMIT
        )
        self.edit.set_text("short")
        self.assertFalse(self.edit._search_results)
        self.assertFalse(self.edit.code_widget.extraSelections())

    def test_search_navigation_uses_qt_utf16_positions(self) -> None:
        self.edit.set_text("😀 a 😀 A")
        self.edit.open_search()
        self.edit.do_search("a")
        self.assertEqual(list(self.edit._search_results), [3, 8])
        self.edit.search_next()
        self.assertEqual(self.edit.code_widget.textCursor().selectedText(), "A")
        self.edit.close_search()
        self.assertFalse(self.edit._search_results)
        self.assertEqual(len(self.edit.code_widget.extraSelections()), 1)

    def test_search_input_is_debounced_and_document_replacement_cancels_it(
        self,
    ) -> None:
        self.edit.set_text("abc")
        self.edit.open_search()
        with unittest.mock.patch.object(
            self.edit, "do_search", wraps=self.edit.do_search
        ) as search:
            self.edit._search_bar.setText("a")
            self.edit._search_bar.setText("ab")
            self.edit._search_bar.setText("abc")
            search.assert_not_called()
            self.assertTrue(wait_until(lambda: search.call_count == 1, timeout_ms=2000))
            search.assert_called_once_with("abc")
            self.edit._search_bar.setText("x")
            self.edit.set_text("new document")
            self.assertFalse(self.edit._search_timer.isActive())
            self.assertFalse(self.edit.code_widget.extraSelections())


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

    def test_a_small_wide_json_is_materialized_in_bounded_batches(self) -> None:
        self.panel.set_text("[" + ",".join("0" for _ in range(100000)) + "]")
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        tree = self.panel.tree.tree
        self.assertEqual(tree.topLevelItemCount(), JSON_TREE_BATCH_SIZE + 1)
        more = tree.topLevelItem(tree.topLevelItemCount() - 1)
        assert more is not None
        tree.itemClicked.emit(more, 0)
        self.assertEqual(tree.topLevelItemCount(), JSON_TREE_BATCH_SIZE * 2 + 1)
        last = tree.topLevelItem(JSON_TREE_BATCH_SIZE * 2 - 1)
        assert last is not None
        self.assertEqual(last.text(0), "[511]")
        while tree.topLevelItemCount() < JSON_TREE_NODE_LIMIT:
            more = tree.topLevelItem(tree.topLevelItemCount() - 1)
            assert more is not None
            tree.itemClicked.emit(more, 0)
        self.assertEqual(tree.topLevelItemCount(), JSON_TREE_NODE_LIMIT)
        self.assertIn("树节点上限", self.panel.tree.notice.text())

    def test_children_are_only_created_when_the_container_expands(self) -> None:
        self.panel.set_text('{"items": [[1, 2], [3, 4]]}')
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        item = self.panel.tree.tree.topLevelItem(0)
        assert item is not None
        self.assertEqual(item.childCount(), 0)
        item.setExpanded(True)
        self.assertEqual(item.childCount(), 2)
        child = item.child(0)
        assert child is not None
        self.assertEqual(child.childCount(), 0)
        child.setExpanded(True)
        self.assertEqual(child.childCount(), 2)

    def test_depth_is_bounded_and_parser_recursion_errors_are_reported(self) -> None:
        self.panel.set_text("[" * 100 + "0" + "]" * 100)
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        tree = self.panel.tree.tree
        item = tree.topLevelItem(0)
        count = 0
        while item is not None:
            count += 1
            item.setExpanded(True)
            item = item.child(0)
        self.assertEqual(count, JSON_TREE_DEPTH_LIMIT)
        self.assertIn("嵌套层级过深", self.panel.tree.notice.text())
        self.panel.set_text("[" * 2000 + "0" + "]" * 2000)
        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.json.loads", side_effect=RecursionError
        ):
            self.panel._show_tree_page()
        self.assertEqual(tree.topLevelItemCount(), 0)
        self.assertIn("嵌套层级过深", self.panel.tree.notice.text())

    def test_replacing_text_releases_hidden_tree_nodes_immediately(self) -> None:
        self.panel.set_text('{"old": [1, 2]}')
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        self.panel.stack.setCurrentWidget(self.panel.text)
        self.panel.set_text("new", Language.HTTP)
        self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)

    def test_text_only_use_never_creates_or_parses_the_tree(self) -> None:
        with unittest.mock.patch("ferret.apps.common.edit.widgets.json.loads") as parse:
            self.panel.set_text('{"a": 1}')
            self.panel.set_text('{"b": 2}')
            self.panel.text.code_widget.setPlainText('{"c": 3}')
        parse.assert_not_called()
        self.assertIsNone(self.panel.tree)
        self.assertEqual(self.panel.stack.count(), 1)
        self.assertEqual(self.panel.plain_text(), '{"c": 3}')

    def test_the_tree_is_rebuilt_from_the_edited_text(self) -> None:
        """树是文本的派生视图；不重建就会显示上一份结构。"""
        self.panel.set_text('{"a": 1}')
        self.panel.text.code_widget.setPlainText('{"b": 2, "c": 3}')
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        tree = self.panel.tree.tree
        self.assertEqual(tree.topLevelItemCount(), 2)
        first = tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "b")

    def test_switching_pages_reuses_the_tree_until_the_text_changes(self) -> None:
        self.panel.set_text('{"a": {"nested": 1}}')
        self.panel._btn_tree.click()
        assert self.panel.tree is not None
        tree_panel = self.panel.tree
        first = tree_panel.tree.topLevelItem(0)
        assert first is not None
        first.setExpanded(True)

        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.json.loads", wraps=json.loads
        ) as parse:
            self.panel._btn_text.click()
            self.panel._btn_tree.click()
            parse.assert_not_called()
            self.assertTrue(first.isExpanded())

            self.panel._btn_text.click()
            self.panel.set_text('{"b": 2}')
            parse.assert_not_called()
            self.panel._btn_tree.click()
            parse.assert_called_once_with('{"b": 2}')

        self.assertIs(self.panel.tree, tree_panel)
        self.assertEqual(self.panel.stack.count(), 2)
        first = tree_panel.tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "b")

    def test_a_visible_tree_tracks_replacement_text(self) -> None:
        self.panel.show()
        self.panel.set_text('{"a": 1}')
        self.panel._btn_tree.click()
        assert self.panel.tree is not None
        self.assertTrue(self.panel.tree.isVisible())

        seen: list[None] = []
        self.panel.changed.connect(lambda: seen.append(None))
        self.panel.set_text('{"b": 2}')

        first = self.panel.tree.tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "b")
        self.assertIs(self.panel.stack.currentWidget(), self.panel.tree)
        self.assertEqual(seen, [])

    def test_loading_the_same_text_and_language_keeps_the_cached_tree(self) -> None:
        text = '{"a": {"nested": 1}}'
        self.panel.show()
        self.panel.set_text(text)
        self.panel._btn_tree.click()
        assert self.panel.tree is not None
        first = self.panel.tree.tree.topLevelItem(0)
        assert first is not None
        first.setExpanded(True)

        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.json.loads", wraps=json.loads
        ) as parse:
            self.panel.set_text(text, "json")
            self.panel._btn_text.click()
            self.panel._btn_tree.click()
        parse.assert_not_called()
        self.assertTrue(first.isExpanded())
        self.assertEqual(self.panel.plain_text(), text)

    def test_changing_only_the_language_invalidates_the_tree(self) -> None:
        text = '{"a": 1}'
        self.panel.show()
        self.panel.set_text(text)
        self.panel._btn_tree.click()
        assert self.panel.tree is not None

        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.json.loads", wraps=json.loads
        ) as parse:
            self.panel.set_text(text, Language.HTTP)
            parse.assert_not_called()
            self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)
            self.panel.set_text(text, Language.JSON)
            parse.assert_called_once_with(text)

        first = self.panel.tree.tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "a")

    def test_a_hidden_tree_waits_until_the_panel_is_shown_again(self) -> None:
        self.panel.show()
        self.panel.set_text('{"a": 1}')
        self.panel._btn_tree.click()
        assert self.panel.tree is not None
        self.panel.hide()

        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.json.loads", wraps=json.loads
        ) as parse:
            self.panel.set_text('{"b": 2}')
            self.panel.set_text('{"c": 3}')
            parse.assert_not_called()
            self.panel.show()
            parse.assert_called_once_with('{"c": 3}')

        first = self.panel.tree.tree.topLevelItem(0)
        assert first is not None
        self.assertEqual(first.text(0), "c")

    def test_broken_json_leaves_an_empty_tree(self) -> None:
        """body 常常是被截断的 JSON，不该因此炸掉。"""
        self.panel.set_text('{"old": 1}')
        self.panel._show_tree_page()
        self.panel.set_text('{"a": 1')
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)
        self.assertEqual(self.panel.plain_text(), '{"a": 1')

    def test_a_non_json_language_has_no_tree(self) -> None:
        self.panel.set_text('{"old": 1}')
        self.panel._show_tree_page()
        self.panel.set_text("plain body", Language.HTTP)
        self.panel._show_tree_page()
        assert self.panel.tree is not None
        self.assertEqual(self.panel.tree.tree.topLevelItemCount(), 0)

    def test_typing_is_announced(self) -> None:
        seen: list[None] = []
        self.panel.changed.connect(lambda: seen.append(None))
        self.panel.set_text('{"a": 1}')
        self.assertEqual(seen, [])
        self.panel.text.code_widget.setPlainText('{"a": 2}')
        self.assertTrue(seen)


class _MenuStub(QObject):
    closedSignal = Signal()

    """替身菜单：真 `RoundMenu.exec` 会弹出非阻塞菜单，离线测试没人点它。

    必须是 `QObject`：菜单动作把菜单当 parent 构造，QAction 拒绝 MagicMock。
    """

    created: typing.ClassVar[list] = []

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.actions: list = []
        _MenuStub.created.append(self)

    def addAction(self, action) -> None:
        self.actions.append(action)

    def exec(self, pos) -> None:
        pass


class ActiveEditorCommitTests(unittest.TestCase):
    """读数据前先提交打开着的单元格编辑器（issues #86）。

    工具栏按钮只有 TabFocus、点击不抢焦点，delegate 编辑器会一直开着 ——
    之前逐格读回的是旧值，切文本页把旧值搬过去，切回表格页再覆盖掉新值。
    """

    def _open_editor(self, table: ItemTableWidget):
        item = table.item(0, 1)
        assert item is not None
        table.editItem(item)
        editor = table.viewport().focusWidget()
        assert isinstance(editor, QLineEdit)  # delegate 给文本格的默认编辑器
        editor.setText("new")
        return editor

    def test_items_commit_an_open_editor(self) -> None:
        panel = ItemTableToolWidget(True)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("X-Key", "old")])
        self._open_editor(panel._table_widget)
        self.assertEqual(panel.items(), [("X-Key", "new")])

    def test_switching_pages_round_trips_the_open_edit(self) -> None:
        """编辑到一半点「文本模式」：新值跟过去；切回表格页不被旧值覆盖。"""
        panel = ItemDualPanel(True)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("X-Key", "old")])
        panel.stack.setCurrentWidget(panel.table)
        self._open_editor(panel.table._table_widget)

        panel._show_text_page()
        self.assertIn("new", panel.text.text())
        panel._show_table_page()
        self.assertEqual(panel.items(), [("X-Key", "new")])

    def test_committing_without_an_editor_is_a_no_op(self) -> None:
        panel = ItemTableToolWidget(True)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("A", "1")])
        panel._table_widget.commit_active_editor()
        self.assertEqual(panel.items(), [("A", "1")])


class ContextMenuTests(unittest.TestCase):
    """右键菜单构造（issues #51）：BaseAction 的第三位置参数是 parent（QObject），
    把槽方法塞进去构造即 TypeError —— 可编辑键值表的右键从未成功弹出过。"""

    def test_the_menu_constructs_with_action_and_remove(self) -> None:
        panel = ItemTableToolWidget(True)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("A", "1")])
        panel._table_widget.selectRow(0)
        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.RoundMenu", _MenuStub
        ):
            panel._show_context_menu(QPoint(5, 5))
        self.assertEqual(len(_MenuStub.created), 1)
        self.assertEqual(len(_MenuStub.created[0].actions), 2)

    def test_a_read_only_table_shows_no_menu(self) -> None:
        panel = ItemTableToolWidget(False)
        self.addCleanup(panel.deleteLater)
        panel.set_items([("A", "1")])
        with unittest.mock.patch(
            "ferret.apps.common.edit.widgets.RoundMenu", _MenuStub
        ):
            panel._show_context_menu(QPoint(5, 5))
        self.assertEqual(_MenuStub.created, [])


if __name__ == "__main__":
    unittest.main()
