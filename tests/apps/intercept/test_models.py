"""断点页两张表格模型的测试：规则列表与拦截队列。

两处刻意与网关页/重写页不同的地方在这里锁住：

* 规则**不给上移/下移** —— 所有启用的规则被 ``|`` 连成一条 flowfilter 表达式，
  命中任意一条就拦，行序没有语义；
* 队列每行是「请求方式 + URL + 操作」，操作列不放数据 —— 按钮由窗口经
  `setIndexWidget` 挂进那一格。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.intercept.models import (
    HeldFlowTableModel,
    InterceptRuleFilterProxyModel,
    InterceptRuleTableModel,
    field_hint,
    field_label,
    logic_label,
    phase_hint,
    phase_label,
    rule_summary,
)
from ferret.core.mitm import (
    InterceptField,
    InterceptLogic,
    InterceptPhase,
    InterceptRule,
)

app = QApplication.instance() or QApplication([])


def make_rule(value: str = "api.example.com", **kwargs) -> InterceptRule:
    return InterceptRule(value=value, **kwargs)


def flow_to(url: str = "http://api.example.com/v1", *, resp: bool = False):
    """一条 `pretty_url` 就是 ``url`` 的流量（`tflow` 默认是 ``http://address:22/path``）。"""
    flow = tflow.tflow(resp=resp)
    flow.request.url = url
    return flow


class LabelTests(unittest.TestCase):
    def test_every_enum_member_has_a_label_and_a_hint(self) -> None:
        """漏一个就会在界面上显示成 ``InterceptPhase.REQUEST`` 这种原始值。"""
        for phase in InterceptPhase:
            self.assertNotEqual(phase_label(phase), str(phase))
            self.assertTrue(phase_hint(phase))
        for field in InterceptField:
            self.assertNotEqual(field_label(field), str(field))
            self.assertTrue(field_hint(field))
        for logic in InterceptLogic:
            self.assertNotEqual(logic_label(logic), str(logic))

    def test_the_logic_labels_match_the_other_two_pages(self) -> None:
        """三页面同一个概念必须同一个词，否则用户以为是三件事。"""
        from ferret.apps.rewrite.models import logic_label as rewrite_logic_label
        from ferret.core.mitm import RewriteLogic

        for logic in InterceptLogic:
            self.assertEqual(
                logic_label(logic), rewrite_logic_label(RewriteLogic(str(logic)))
            )

    def test_rule_summary_shows_the_expression_that_gets_pushed_down(self) -> None:
        summary = rule_summary(make_rule())
        self.assertIn(phase_hint(InterceptPhase.BOTH), summary)
        self.assertIn(make_rule().expression, summary)

    def test_the_summary_hint_follows_the_rule_phase(self) -> None:
        """每个阶段各停几次要说清楚 —— 规则选阶段是会改变行为的。"""
        for phase in InterceptPhase:
            with self.subTest(phase=phase):
                self.assertIn(phase_hint(phase), rule_summary(make_rule(phase=phase)))

    def test_rule_summary_explains_an_unusable_rule(self) -> None:
        self.assertEqual(rule_summary(make_rule("")), "Match value cannot be empty")
        self.assertIn(
            "Invalid match value",
            rule_summary(make_rule("bad(", logic=InterceptLogic.REGEX)),
        )


class InterceptRuleTableModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = InterceptRuleTableModel()
        self.model.set_rules(
            [
                make_rule("api.example.com"),
                make_rule(
                    "POST",
                    field=InterceptField.METHOD,
                    logic=InterceptLogic.EQUALS,
                    enabled=False,
                    phase=InterceptPhase.RESPONSE,
                ),
            ]
        )

    def test_row_and_column_counts(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 5)

    def test_headers_are_labelled(self) -> None:
        for col, expected in enumerate(InterceptRuleTableModel.HEADERS):
            self.assertEqual(
                self.model.headerData(col, Qt.Orientation.Horizontal), expected
            )

    def test_display_columns_show_the_localized_choices(self) -> None:
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(
            self.model.data(self.model.index(1, 1), role),
            field_label(InterceptField.METHOD),
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 2), role),
            logic_label(InterceptLogic.EQUALS),
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 3), role),
            phase_label(InterceptPhase.RESPONSE),
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 4), role), "POST"
        )

    def test_check_state_reflects_enabled(self) -> None:
        role = Qt.ItemDataRole.CheckStateRole
        self.assertEqual(
            self.model.data(self.model.index(0, 0), role), Qt.CheckState.Checked
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 0), role), Qt.CheckState.Unchecked
        )

    def test_tooltip_shows_the_compiled_expression(self) -> None:
        tooltip = self.model.data(self.model.index(0, 3), Qt.ItemDataRole.ToolTipRole)
        self.assertIn("~u", tooltip)
        # 表达式里没有阶段选择器，提示里也就不该出现 —— 措辞和真正下发的东西对齐。
        self.assertNotIn("~q", tooltip)
        self.assertNotIn("~s", tooltip)

    def test_user_role_returns_the_rule(self) -> None:
        self.assertEqual(
            self.model.data(self.model.index(0, 0), Qt.ItemDataRole.UserRole),
            make_rule("api.example.com"),
        )

    def test_rule_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.model.rule_at(9))
        self.assertIsNone(self.model.rule_at(-1))

    def test_an_invalid_index_yields_nothing(self) -> None:
        from PySide6.QtCore import QModelIndex

        self.assertIsNone(self.model.data(QModelIndex()))

    def test_set_data_toggles_and_signals_on_the_next_turn(self) -> None:
        seen: list[tuple[int, bool]] = []
        self.model.enabled_toggled.connect(lambda row, on: seen.append((row, on)))
        ok = self.model.setData(
            self.model.index(1, 0),
            Qt.CheckState.Checked.value,
            Qt.ItemDataRole.CheckStateRole,
        )
        self.assertTrue(ok)
        toggled = self.model.rule_at(1)
        assert toggled is not None
        self.assertTrue(toggled.enabled)
        # 信号被 singleShot 推到下一轮事件循环，避免控制器回头 reset 造成重入。
        self.assertEqual(seen, [])
        app.processEvents()
        self.assertEqual(seen, [(1, True)])

    def test_set_data_ignores_other_roles_columns_and_no_ops(self) -> None:
        self.assertFalse(
            self.model.setData(self.model.index(0, 3), "x", Qt.ItemDataRole.EditRole)
        )
        self.assertFalse(
            self.model.setData(
                self.model.index(0, 3),
                Qt.CheckState.Checked.value,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        # 已经是勾选态，再勾一次不该惊动控制器（会白落一次盘、白下发一次）。
        self.assertFalse(
            self.model.setData(
                self.model.index(0, 0),
                Qt.CheckState.Checked.value,
                Qt.ItemDataRole.CheckStateRole,
            )
        )

    def test_only_the_first_column_is_checkable(self) -> None:
        self.assertTrue(
            self.model.flags(self.model.index(0, 0)) & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertFalse(
            self.model.flags(self.model.index(0, 3)) & Qt.ItemFlag.ItemIsUserCheckable
        )

    def test_the_model_offers_no_reordering(self) -> None:
        """行序没有语义（多条规则是 ``|`` 关系），所以刻意不提供上移/下移。"""
        self.assertFalse(hasattr(self.model, "move_rule"))

    def test_the_phase_column_sits_before_the_value(self) -> None:
        """规则选阶段。值是最长的一列、要占满剩余宽度，所以阶段排在它前面。"""
        self.assertEqual(
            InterceptRuleTableModel.HEADERS,
            ["Enabled", "Match on", "Condition", "Phase", "Value"],
        )


class InterceptRuleFilterProxyModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = InterceptRuleTableModel()
        self.source.set_rules(
            [
                make_rule("ads.example.com"),
                make_rule(
                    "POST",
                    field=InterceptField.METHOD,
                    logic=InterceptLogic.EQUALS,
                ),
            ]
        )
        self.proxy = InterceptRuleFilterProxyModel()
        self.proxy.setSourceModel(self.source)

    def test_empty_filter_keeps_every_row(self) -> None:
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_filter_matches_the_value(self) -> None:
        self.proxy.set_filter_text("ads")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_localized_labels(self) -> None:
        """用户看到的是「方法」「等于」，搜的也会是这几个字。"""
        self.proxy.set_filter_text(field_label(InterceptField.METHOD))
        self.assertEqual(self.proxy.rowCount(), 1)
        self.proxy.set_filter_text(logic_label(InterceptLogic.EQUALS))
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_phase_label(self) -> None:
        """阶段也进了搜索范围：两条都停在默认「请求和响应」，搜「响应」两条都在。"""
        self.proxy.set_filter_text(phase_label(InterceptPhase.BOTH))
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_filter_is_case_insensitive(self) -> None:
        self.proxy.set_filter_text("ADS.EXAMPLE")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_blank_filter_text_is_the_same_as_none(self) -> None:
        self.proxy.set_filter_text("   ")
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_no_match_yields_no_rows(self) -> None:
        self.proxy.set_filter_text("nothing-here")
        self.assertEqual(self.proxy.rowCount(), 0)


class HeldFlowTableModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = HeldFlowTableModel()
        self.waiting = flow_to("http://api.example.com/v1")
        self.answered = flow_to("http://cdn.example.com/a.js", resp=True)
        self.model.set_flows([self.waiting, self.answered])

    def test_row_and_column_counts(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 3)

    def test_headers_are_labelled(self) -> None:
        for col, expected in enumerate(HeldFlowTableModel.HEADERS):
            self.assertEqual(
                self.model.headerData(col, Qt.Orientation.Horizontal), expected
            )

    def test_display_columns_describe_the_held_flow(self) -> None:
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(
            self.model.data(self.model.index(0, 0), role), self.waiting.request.method
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 1), role),
            "http://api.example.com/v1",
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 1), role),
            "http://cdn.example.com/a.js",
        )

    def test_the_actions_column_carries_no_data(self) -> None:
        """操作列不放数据：那一格归 `setIndexWidget` 挂的「放行 / 丢弃」按钮组。"""
        role = Qt.ItemDataRole.DisplayRole
        for row in range(self.model.rowCount()):
            self.assertIsNone(self.model.data(self.model.index(row, 2), role))
        self.assertEqual(HeldFlowTableModel.ACTIONS_COLUMN, 2)

    def test_user_role_returns_the_flow(self) -> None:
        self.assertIs(
            self.model.data(self.model.index(1, 0), Qt.ItemDataRole.UserRole),
            self.answered,
        )

    def test_flow_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.model.flow_at(9))
        self.assertIsNone(self.model.flow_at(-1))

    def test_row_of_finds_by_id_not_by_object(self) -> None:
        """每次刷新拿到的都是一批新快照，只有 id 是稳定的。"""
        self.assertEqual(self.model.row_of(self.answered.id), 1)
        refreshed = [self.waiting.copy(), self.answered.copy()]
        # copy() 会换一个新的 id，所以刷新后旧 id 必然找不到 —— 这正是要锁的语义：
        # 界面记住的是选中行的 id，而不是那个 Python 对象。
        self.model.set_flows(refreshed)
        self.assertEqual(self.model.row_of(refreshed[1].id), 1)
        self.assertEqual(self.model.row_of("no-such-id"), -1)

    def test_an_invalid_index_yields_nothing(self) -> None:
        from PySide6.QtCore import QModelIndex

        self.assertIsNone(self.model.data(QModelIndex()))

    def test_set_flows_replaces_the_whole_queue(self) -> None:
        self.model.set_flows([])
        self.assertEqual(self.model.rowCount(), 0)


if __name__ == "__main__":
    unittest.main()
