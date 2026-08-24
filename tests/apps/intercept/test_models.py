"""断点页两张表格模型的测试：规则列表与拦截队列。

两处刻意与网关页/重写页不同的地方在这里锁住：

* 规则**不给上移/下移** —— 所有启用的规则被 ``|`` 连成一条 flowfilter 表达式，
  命中任意一条就拦，行序没有语义；
* 队列装的是 `flow.copy()` 快照，判「停在哪个阶段」只能看有没有 response
  （与原生 ``~q`` / ``~s`` 逐字同义），不能自己另记一份阶段。
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
    both_phases_hint,
    field_hint,
    field_label,
    held_edited,
    held_phase_label,
    held_status,
    logic_label,
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
    def test_every_enum_member_has_a_label(self) -> None:
        """漏一个就会在界面上显示成 ``InterceptPhase.REQUEST`` 这种原始值。

        阶段只剩「标注队列里这条停在哪」这一个用途，所以只要 label，没有 hint ——
        规则的行为说明是一句固定的 `both_phases_hint()`，不按阶段分。
        """
        for phase in InterceptPhase:
            self.assertNotEqual(phase_label(phase), str(phase))
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
        self.assertIn(both_phases_hint(), summary)
        self.assertIn(make_rule().expression, summary)

    def test_rule_summary_explains_an_unusable_rule(self) -> None:
        self.assertEqual(rule_summary(make_rule("")), "Match value cannot be empty")
        self.assertIn(
            "Invalid match value",
            rule_summary(make_rule("bad(", logic=InterceptLogic.REGEX)),
        )


class HeldFlowLabelTests(unittest.TestCase):
    def test_the_phase_is_decided_by_having_a_response(self) -> None:
        """和原生 ``~q`` / ``~s`` 同一个判据，不另记一份阶段。"""
        self.assertEqual(
            held_phase_label(flow_to()), phase_label(InterceptPhase.REQUEST)
        )
        self.assertEqual(
            held_phase_label(flow_to(resp=True)),
            phase_label(InterceptPhase.RESPONSE),
        )

    def test_a_request_phase_flow_has_no_status_code_yet(self) -> None:
        self.assertEqual(held_status(flow_to()), "Waiting for response")

    def test_a_response_phase_flow_shows_its_status_code(self) -> None:
        self.assertEqual(held_status(flow_to(resp=True)), "200")

    def test_a_backed_up_flow_reads_as_edited(self) -> None:
        """`Flow.modified()` 的真实语义是「有备份可撤销」，写回前必然 backup 过。"""
        flow = flow_to(resp=True)
        self.assertFalse(held_edited(flow))
        flow.backup()
        self.assertTrue(held_edited(flow))
        self.assertEqual(held_status(flow), "200 · Edited")

    def test_reverting_takes_the_edited_mark_away(self) -> None:
        flow = flow_to(resp=True)
        flow.backup()
        flow.revert()
        self.assertFalse(held_edited(flow))
        self.assertEqual(held_status(flow), "200")

    def test_a_copy_carries_the_edited_mark(self) -> None:
        """队列里装的是 `flow.copy()`，标记必须跟着快照走。"""
        flow = flow_to()
        flow.backup()
        self.assertTrue(held_edited(flow.copy()))


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
                ),
            ]
        )

    def test_row_and_column_counts(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 4)

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
        self.assertEqual(self.model.data(self.model.index(1, 3), role), "POST")

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

    def test_the_table_has_no_phase_column(self) -> None:
        """规则不选阶段，表里也不该留一列去暗示它能选。"""
        self.assertEqual(
            InterceptRuleTableModel.HEADERS,
            ["Enabled", "Match on", "Condition", "Value"],
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
        self.assertEqual(self.model.columnCount(), 4)

    def test_headers_are_labelled(self) -> None:
        for col, expected in enumerate(HeldFlowTableModel.HEADERS):
            self.assertEqual(
                self.model.headerData(col, Qt.Orientation.Horizontal), expected
            )

    def test_display_columns_describe_the_held_flow(self) -> None:
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(
            self.model.data(self.model.index(0, 0), role),
            phase_label(InterceptPhase.REQUEST),
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 1), role), self.waiting.request.method
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 2), role),
            "http://api.example.com/v1",
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 3), role), "Waiting for response"
        )
        self.assertEqual(self.model.data(self.model.index(1, 3), role), "200")

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
