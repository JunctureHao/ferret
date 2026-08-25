"""字段声明表的守卫：规格表是数据，渲染只剩一层循环 —— 两边都得钉住。

原来「哪些行会出现」藏在 `OverviewTree.set_data` 那 290 行里，搬进 `fields.SECTIONS`
之后它是纯数据了，所以这里按语义分三层测：

* `field_value` / `section_title` / `format_time` 是纯函数 —— 空值、`fmt`、缺键容错；
* `section_rows` 是「一个分组 + 一份数据 → 已求值的行」，也是纯函数 —— 空组不留标题、
  `when` 说不显示就整组不显示、次级小节没内容连小标题一起不出现；
* `FieldCard` / `OverviewPane` 只剩「一串 Row 怎么摆进网格」与折叠。

翻译器**故意不装**，和 `tests/core/test_i18n.py` 同一个理由：unittest 一个进程跑完所有
用例，装上去会污染别处断言英文文案的用例。`translate()` 查不到就原样返回源文本，正好用来
验标记本身有没有被求值成别的东西。译文是否补齐由 `tests/core/test_i18n.py` 双向盯着。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.flow.fields import (
    SECTIONS,
    Field,
    FieldCard,
    OverviewPane,
    Section,
    field_label,
    field_value,
    format_time,
    section_rows,
    section_title,
)


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
        self.assertEqual(
            field_value(find_field("Request"), {"req_total_size": 384}), "384b"
        )

    def test_the_decoded_row_only_shows_up_when_it_differs_from_the_wire(self) -> None:
        """没压缩的报文两个口径一样大，并排列两行相同的值只是噪音。"""
        decoded = find_field("- Response body decoded")
        same = {"res_wire_size": 900, "res_decoded_size": 900}
        self.assertIsNone(field_value(decoded, same))
        gzipped = {"res_wire_size": 900, "res_decoded_size": 4096}
        self.assertEqual(field_value(decoded, gzipped), "4.0k")
        self.assertIsNone(field_value(decoded, {}))

    def test_wire_and_decoded_are_separate_rows(self) -> None:
        """两个口径各有各的行，谁也不冒充「大小」 —— 混成一个数才是原来的毛病。"""
        data = {"res_wire_size": 900, "res_decoded_size": 4096}
        self.assertEqual(
            field_value(find_field("- Response body on the wire"), data), "900b"
        )
        self.assertEqual(
            field_value(find_field("- Response body decoded"), data), "4.0k"
        )


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


class SectionRowsTests(unittest.TestCase):
    """规格 → 行。纯函数，不起窗口 —— 「哪些行会出现」的规则全在这一层。"""

    @classmethod
    def setUpClass(cls) -> None:
        # `section_title` / `field_label` 走 `QCoreApplication.translate`，静态方法
        # 本来就不依赖实例；起一个是为了和其它用例共享同一个进程状态。
        cls.app = QApplication.instance() or QApplication([])

    def flatten(self, section: Section, data: dict) -> list[tuple[str, str, bool]]:
        return [
            (row.label, row.value, row.heading) for row in section_rows(section, data)
        ]

    def test_an_empty_dict_only_yields_what_it_can_answer(self) -> None:
        summary = find_section("Summary")
        self.assertEqual(self.flatten(summary, {}), [("State", "Unknown", False)])

    def test_a_group_with_nothing_to_show_yields_no_rows_at_all(self) -> None:
        """整组空就返回空表 —— 卡片那侧据此整张隐藏，标题不会孤零零留着。"""
        for title in ("TLS · server", "Connection", "Timing"):
            with self.subTest(title=title):
                self.assertEqual(
                    section_rows(find_section(title), {"Method": "GET"}), []
                )

    def test_a_group_appears_once_its_condition_holds(self) -> None:
        rows = self.flatten(find_section("TLS · server"), {"TLS Version": "TLSv1.3"})
        self.assertIn(("Version", "TLSv1.3", False), rows)

    def test_a_groups_condition_outranks_its_own_fields(self) -> None:
        """连接组只看 ID/时间 —— 光有前后端地址时整组不露面，和搬过来之前一致。"""
        connection = find_section("Connection")
        self.assertEqual(
            section_rows(connection, {"Front Client Address": "127.0.0.1"}), []
        )

    def test_a_subgroup_becomes_a_heading_row_followed_by_its_own_rows(self) -> None:
        """次级小节不另开一张卡：一行跨两列的小标题，后面跟自己的行。"""
        rows = self.flatten(
            find_section("Server certificate"),
            {"Subject Common Name": "example.com", "Not Before": "2026-01-01"},
        )
        self.assertIn(("Subject", "", True), rows)
        self.assertIn(("Common Name", "example.com", False), rows)
        self.assertIn(("Not before", "2026-01-01", False), rows)
        # 小标题行永远没有值。
        self.assertEqual([row for row in rows if row[2] and row[1]], [])

    def test_a_subgroup_without_content_drops_its_heading_too(self) -> None:
        """证书组曾经渲染出 12 行字面 ``-``，看着像「读到证书但每项都空」。

        那是 `always=True` 加 models 不产出这六项凑出来的假象。现在缺哪项少哪行 ——
        只有有效期的证书就只显示有效期，不再凭空多出主体和签发者两个小节。
        """
        rows = self.flatten(
            find_section("Server certificate"), {"Not Before": "2026-01-01"}
        )
        self.assertIn(("Not before", "2026-01-01", False), rows)
        self.assertNotIn(("Subject", "", True), rows)
        self.assertNotIn(("Issuer", "", True), rows)
        self.assertEqual([row for row in rows if row[1] == "-"], [])

    def test_top_level_fields_keep_their_own_group(self) -> None:
        """空标题的分组照旧把字段原样交回 —— 卡片标题为空，行还在。"""
        summary = find_section("Summary")
        rows = self.flatten(summary, {"Method": "GET", "Status Code": 200})
        self.assertIn(("Method", "GET", False), rows)
        self.assertIn(("Code", "200", False), rows)


class FieldCardTests(unittest.TestCase):
    """渲染这一侧：网格里摆了几个控件、折叠状态、整组复制。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def card(self, title: str) -> FieldCard:
        return FieldCard(find_section(title), self.host)

    def test_a_card_with_nothing_to_show_hides_itself(self) -> None:
        card = self.card("TLS · server")
        card.set_data({"Method": "GET"})
        self.assertEqual(card.rows(), [])
        self.assertTrue(card.isHidden())

        card.set_data({"TLS Version": "TLSv1.3"})
        self.assertFalse(card.isHidden())

    def test_set_data_rebuilds_the_grid_instead_of_appending(self) -> None:
        card = self.card("Summary")
        card.set_data({"Method": "GET"})
        first = card.grid.count()
        card.set_data({"Method": "POST"})
        self.assertEqual(card.grid.count(), first)
        self.assertIn(("Method", "POST"), [(r.label, r.value) for r in card.rows()])

    def test_a_heading_row_spans_both_columns(self) -> None:
        card = self.card("Server certificate")
        card.set_data({"Subject Common Name": "example.com"})
        rows = card.rows()
        self.assertTrue([row for row in rows if row.heading])
        for index, row in enumerate(rows):
            with self.subTest(label=row.label):
                # 跨两列的控件占着两个格子，两边取回的是同一个；
                # 普通行左右两格是标签和值两个不同控件。
                left = card.grid.itemAtPosition(index, 0)
                right = card.grid.itemAtPosition(index, 1)
                assert left is not None and right is not None
                spans = left.widget() is right.widget()
                self.assertEqual(spans, row.heading)

    def test_every_card_can_collapse_not_just_the_ones_declared_collapsed(self) -> None:
        """只有部分卡能点等于让人去记哪几张能点 —— 折叠给每张卡，`collapsed` 只定初始态。"""
        card = self.card("Summary")
        self.assertTrue(card.is_expanded())
        card.toggle()
        self.assertFalse(card.is_expanded())
        self.assertFalse(card.view.isVisibleTo(card))
        card.toggle()
        self.assertTrue(card.is_expanded())

    def test_collapsed_state_does_not_depend_on_the_panel_being_shown(self) -> None:
        """`isVisible()` 连祖先一起算 —— 面板还没 show 时不能被当成「卡是收起的」。"""
        card = self.card("Summary")
        self.assertFalse(card.isVisible())
        self.assertTrue(card.is_expanded())

    def test_a_collapsed_section_starts_folded(self) -> None:
        card = FieldCard(Section(title="X", fields=(Field("L", "k"),), collapsed=True))
        self.assertFalse(card.is_expanded())
        card.deleteLater()

    def test_copying_a_group_writes_label_colon_value_per_line(self) -> None:
        card = self.card("Server certificate")
        card.set_data(
            {"Subject Common Name": "example.com", "Not Before": "2026-01-01"}
        )
        card.copy_to_clipboard()
        text = QApplication.clipboard().text()
        self.assertIn("Common Name: example.com", text)
        # 小标题行只出标题，不带冒号。
        self.assertIn("\nSubject\n", f"\n{text}\n")


class OverviewPaneTests(unittest.TestCase):
    """一整页：一个顶层分组一张卡，空组的卡不显示。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.pane = OverviewPane(self.host)

    def tearDown(self) -> None:
        self.host.deleteLater()
        self.app.processEvents()

    def test_one_card_per_top_level_section(self) -> None:
        self.assertEqual(len(self.pane.cards), len(SECTIONS))
        self.assertEqual(
            [card.section for card in self.pane.cards],
            list(SECTIONS),
        )

    def test_an_empty_dict_leaves_only_the_cards_that_can_answer(self) -> None:
        self.pane.set_data({})
        titles = [card.section.title for card in self.pane.visible_cards()]
        self.assertEqual(titles, ["Summary"])

    def test_set_data_replaces_the_previous_flow(self) -> None:
        self.pane.set_data({"TLS Version": "TLSv1.3"})
        self.assertIn(
            "TLS · server", [c.section.title for c in self.pane.visible_cards()]
        )
        self.pane.set_data({"Method": "GET"})
        self.assertNotIn(
            "TLS · server", [c.section.title for c in self.pane.visible_cards()]
        )


if __name__ == "__main__":
    unittest.main()
