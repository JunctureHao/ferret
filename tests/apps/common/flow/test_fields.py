"""字段声明表的守卫：规格表是数据，渲染只剩一层循环 —— 两边都得钉住。

原来「哪些行会出现」藏在 `OverviewTree.set_data` 那 290 行里，搬进 `fields.SECTIONS`
之后它是纯数据了，所以这里按语义分两层测：

* `field_value` / `section_title` / `format_time` 是纯函数 —— 空值、`fmt`、缺键容错；
* `OverviewTree` 只剩「一组标签和值怎么变成树节点」—— 空组不留标题、`when` 说不显示就
  整组不显示、次级小节的 ``- `` 前缀。

翻译器**故意不装**，和 `tests/core/test_i18n.py` 同一个理由：unittest 一个进程跑完所有
用例，装上去会污染别处断言英文文案的用例。`translate()` 查不到就原样返回源文本，正好用来
验标记本身有没有被求值成别的东西。译文是否补齐由 `tests/core/test_i18n.py` 双向盯着。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTreeWidgetItem, QWidget

from ferret.apps.common.flow.fields import (
    SECTIONS,
    Field,
    Section,
    field_label,
    field_value,
    format_time,
    section_title,
)
from ferret.apps.common.flow.views import OverviewTree


def find_field(label: str) -> Field:
    """按标签在 `SECTIONS` 里找字段 —— 测的是真规格表，不是就地造的假表。"""

    def walk(entries):
        for entry in entries:
            if isinstance(entry, Section):
                yield from walk(entry.fields)
            elif entry.label == label:
                yield entry

    found = next(walk(SECTIONS), None)
    assert found is not None, f"SECTIONS 里没有标签为 {label!r} 的字段"
    return found


def find_section(title: str) -> Section:
    """按标题在 `SECTIONS` 里找分组（含次级小节）。"""

    def walk(entries):
        for entry in entries:
            if not isinstance(entry, Section):
                continue
            if entry.title == title:
                yield entry
            yield from walk(entry.fields)

    found = next(walk(SECTIONS), None)
    assert found is not None, f"SECTIONS 里没有标题为 {title!r} 的分组"
    return found


class FieldValueTests(unittest.TestCase):
    """取值与格式化，`data` 一律当成可能残缺的字典对待。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_missing_and_blank_values_skip_the_row(self) -> None:
        field = Field("Label", "key")
        self.assertIsNone(field_value(field, {}))
        for blank in (None, "", "N/A", "-", [], ()):
            with self.subTest(blank=blank):
                self.assertIsNone(field_value(field, {"key": blank}))

    def test_zero_is_a_value_not_a_blank(self) -> None:
        """时序与大小两组靠这个：`0 ms`、`0b` 是结论，不是「没数据」。"""
        self.assertEqual(field_value(Field("Label", "key"), {"key": 0}), "0")

    def test_always_renders_a_dash_instead_of_skipping(self) -> None:
        field = Field("Label", "key", always=True)
        self.assertEqual(field_value(field, {}), "-")
        self.assertEqual(field_value(field, {"key": "x"}), "x")

    def test_fmt_runs_on_the_raw_value(self) -> None:
        field = Field("Label", "key", fmt=lambda value: f"<{value}>")
        self.assertEqual(field_value(field, {"key": 7}), "<7>")

    def test_callable_source_reads_the_whole_dict(self) -> None:
        field = Field("Label", lambda data: len(data))
        self.assertEqual(field_value(field, {"a": 1, "b": 2}), "2")

    def test_list_values_render_as_one_comma_joined_line(self) -> None:
        """ALPN / cipher list 原来是「N 项」加一层子节点，现在一行逗号分隔。"""
        alpn = find_field("ALPN")
        self.assertEqual(
            field_value(alpn, {"TLS ALPN Offers": ["h2", "http/1.1"]}), "h2, http/1.1"
        )
        self.assertIsNone(field_value(alpn, {"TLS ALPN Offers": []}))

    def test_size_fields_count_a_missing_key_as_zero(self) -> None:
        """只抓到请求的流量照旧显示 ``0b`` 的响应大小，不是整行消失。"""
        total = find_field("Total")
        self.assertEqual(field_value(total, {}), "0b")
        self.assertEqual(field_value(find_field("Request"), {"req_size": 384}), "384b")


class LabelTests(unittest.TestCase):
    """标记求值。没装翻译器时 Qt 原样返回源文本，所以这里断言的是英文。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_markers_evaluate_to_their_source_text_without_a_translator(self) -> None:
        self.assertEqual(field_label(Field("Server address", "x")), "Server address")
        self.assertEqual(section_title(find_section("Connection")), "Connection")

    def test_a_section_without_a_title_evaluates_to_nothing(self) -> None:
        """最外层那批平级字段就是靠空标题挂上去的，不能求值成「查不到」的占位符。"""
        self.assertEqual(section_title(Section(title="", fields=())), "")

    def test_state_resolves_unknown_states_instead_of_echoing_them(self) -> None:
        state = find_field("State")
        self.assertEqual(field_value(state, {"state": "complete"}), "Completed")
        self.assertEqual(field_value(state, {"state": "nonsense"}), "Unknown")
        self.assertEqual(field_value(state, {}), "Unknown")


class FormatTimeTests(unittest.TestCase):
    def test_a_missing_timestamp_is_a_dash_not_the_current_time(self) -> None:
        """``time.localtime(None)`` 返回当前时间，空值必须自己挡掉。"""
        self.assertEqual(format_time(None), "-")
        self.assertEqual(format_time(0), "-")
        self.assertNotEqual(format_time(1756000000.0), "-")


class OverviewTreeTests(unittest.TestCase):
    """渲染这一侧：只验节点结构，字段内容归上面几个用例管。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.tree = OverviewTree(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def top(self, index: int) -> QTreeWidgetItem:
        """`topLevelItem` 的返回值是 Optional，取出来先钉住再断言。"""
        item = self.tree.topLevelItem(index)
        assert item is not None, f"没有第 {index} 个顶层节点"
        return item

    def child(self, item: QTreeWidgetItem, index: int) -> QTreeWidgetItem:
        """`child` 同上。"""
        found = item.child(index)
        assert found is not None, f"{item.text(0)!r} 没有第 {index} 个子节点"
        return found

    def rows(self) -> list[tuple[int, str, str]]:
        """整棵树拍平成 ``(缩进, 标签, 值)``。"""
        out: list[tuple[int, str, str]] = []

        def walk(item, depth: int) -> None:
            out.append((depth, item.text(0), item.text(1)))
            for index in range(item.childCount()):
                walk(item.child(index), depth + 1)

        for index in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(index), 0)
        return out

    def titles(self) -> list[str]:
        return [
            self.top(index).text(0) for index in range(self.tree.topLevelItemCount())
        ]

    def test_an_empty_dict_renders_only_what_it_can_answer(self) -> None:
        self.tree.set_data({})
        self.assertEqual(self.rows(), [(0, "State", "Unknown")])

    def test_set_data_clears_the_previous_flow(self) -> None:
        self.tree.set_data({"Method": "GET"})
        self.tree.set_data({"Method": "POST"})
        self.assertEqual(self.rows().count((0, "Method", "POST")), 1)
        self.assertNotIn((0, "Method", "GET"), self.rows())

    def test_top_level_fields_stay_flat(self) -> None:
        """空标题的分组把字段原样交回上一层，不能多出一个分组节点。"""
        self.tree.set_data({"Method": "GET", "Status Code": 200})
        self.assertIn((0, "Method", "GET"), self.rows())
        self.assertIn((0, "Code", "200"), self.rows())

    def test_a_group_with_nothing_to_show_leaves_no_title_behind(self) -> None:
        self.tree.set_data({"Method": "GET"})
        self.assertNotIn("TLS", self.titles())
        self.assertNotIn("Connection", self.titles())
        self.assertNotIn("Timing", self.titles())

    def test_a_group_appears_once_its_condition_holds(self) -> None:
        self.tree.set_data({"TLS Version": "TLSv1.3"})
        self.assertIn("TLS", self.titles())
        self.assertIn((1, "Version", "TLSv1.3"), self.rows())

    def test_a_groups_condition_outranks_its_own_fields(self) -> None:
        """连接组只看 ID/时间 —— 光有前后端地址时整组不露面，和搬过来之前一致。"""
        self.tree.set_data({"Front Client Address": "127.0.0.1"})
        self.assertNotIn("Connection", self.titles())

    def test_a_subgroup_nests_under_its_parent_with_a_dash_prefix(self) -> None:
        self.tree.set_data({"Not Before": "2026-01-01"})
        rows = self.rows()
        self.assertIn((0, "Server certificate", ""), rows)
        self.assertIn((1, "Subject", ""), rows)
        self.assertIn((2, "- Common Name", "-"), rows)
        self.assertIn((1, "Not before", "2026-01-01"), rows)

    def test_group_titles_are_bold_and_subgroup_titles_underlined(self) -> None:
        self.tree.set_data({"Not Before": "2026-01-01"})
        certificate = self.top(self.titles().index("Server certificate"))
        self.assertTrue(certificate.font(0).bold())
        self.assertTrue(self.child(certificate, 0).font(0).underline())


if __name__ == "__main__":
    unittest.main()
