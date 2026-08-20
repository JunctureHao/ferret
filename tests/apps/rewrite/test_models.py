import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qfluentwidgets import qconfig

from ferret.apps.rewrite.controllers import RewriteController
from ferret.apps.rewrite.models import (
    RewriteRuleFilterProxyModel,
    RewriteRuleTableModel,
    kind_label,
    logic_label,
)
from ferret.core.mitm import (
    MitmFacade,
    MitmRuntime,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
)
from ferret.core.settings import CONFIG

app = QApplication.instance() or QApplication([])


def make_rule(value: str, replacement: str = "http://127.0.0.1:8000/x", **kwargs):
    return RewriteRule(
        kind=RewriteKind.MAP_REMOTE,
        logic=RewriteLogic.EQUALS,
        value=value,
        replacement=replacement,
        **kwargs,
    )


class RewriteRuleTableModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = RewriteRuleTableModel()
        self.model.set_rules(
            [
                make_rule("https://a.com/x"),
                make_rule("https://b.com/x", enabled=False),
            ]
        )

    def test_row_and_column_counts(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 5)

    def test_check_state_reflects_enabled(self) -> None:
        role = Qt.ItemDataRole.CheckStateRole
        self.assertEqual(
            self.model.data(self.model.index(0, 0), role), Qt.CheckState.Checked
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 0), role), Qt.CheckState.Unchecked
        )

    def test_display_columns_show_what_the_user_typed(self) -> None:
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(
            self.model.data(self.model.index(0, 1), role),
            kind_label(RewriteKind.MAP_REMOTE),
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 2), role),
            logic_label(RewriteLogic.EQUALS),
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 3), role), "https://a.com/x"
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 4), role), "http://127.0.0.1:8000/x"
        )

    def test_tooltip_shows_the_generated_regex_pair(self) -> None:
        tooltip = self.model.data(self.model.index(0, 3), Qt.ItemDataRole.ToolTipRole)
        self.assertIn(r"^https://a\.com/x$", tooltip)
        self.assertIn("http://127.0.0.1:8000/x", tooltip)

    def test_tooltip_explains_an_unusable_rule(self) -> None:
        self.model.set_rules([make_rule("https://a.com/x", replacement="")])
        tooltip = self.model.data(self.model.index(0, 3), Qt.ItemDataRole.ToolTipRole)
        self.assertEqual(tooltip, "重写目标不能为空")

    def test_user_role_returns_the_rule(self) -> None:
        rule = self.model.data(self.model.index(1, 0), Qt.ItemDataRole.UserRole)
        self.assertEqual(rule, make_rule("https://b.com/x", enabled=False))

    def test_rule_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.model.rule_at(9))

    def test_set_data_toggles_and_signals(self) -> None:
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

    def test_set_data_ignores_other_roles_and_columns(self) -> None:
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

    def test_first_column_is_user_checkable(self) -> None:
        flags = self.model.flags(self.model.index(0, 0))
        self.assertTrue(flags & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertFalse(
            self.model.flags(self.model.index(0, 3)) & Qt.ItemFlag.ItemIsUserCheckable
        )


class RewriteRuleFilterProxyModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = RewriteRuleTableModel()
        self.source.set_rules(
            [
                make_rule("https://ads.example.com/x"),
                RewriteRule(
                    logic=RewriteLogic.CONTAINS,
                    value="cdn.example.com",
                    replacement="localhost:9000",
                ),
            ]
        )
        self.proxy = RewriteRuleFilterProxyModel()
        self.proxy.setSourceModel(self.source)

    def test_empty_filter_keeps_every_row(self) -> None:
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_filter_matches_the_value(self) -> None:
        self.proxy.set_filter_text("ads")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_replacement(self) -> None:
        self.proxy.set_filter_text("localhost")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_localized_labels(self) -> None:
        self.proxy.set_filter_text(logic_label(RewriteLogic.CONTAINS))
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_is_case_insensitive(self) -> None:
        self.proxy.set_filter_text("ADS.EXAMPLE")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_no_match_yields_no_rows(self) -> None:
        self.proxy.set_filter_text("nothing-here")
        self.assertEqual(self.proxy.rowCount(), 0)


class RewriteControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        # 配置指向临时文件，别碰用户真实的 config.json。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config_file = Path(self._tmp.name) / "config.json"
        self.addCleanup(setattr, CONFIG, "file", CONFIG.file)
        qconfig.load(str(self.config_file), CONFIG)
        self.runtime = MitmRuntime()
        self.controller = RewriteController(mitm=MitmFacade(self.runtime))

    def tearDown(self) -> None:
        # 先清空再交还 CONFIG.file，避免临时目录删掉后还留着规则/坏路径。
        CONFIG.set(CONFIG.rewrite_rules, [])

    def test_starts_empty_and_pushes_to_the_runtime(self) -> None:
        self.assertEqual(self.controller.rules, [])
        self.assertEqual(self.runtime.rewrite_rules, [])

    def test_add_rule_persists_and_pushes_down(self) -> None:
        rule = make_rule("https://a.com/x")
        self.assertTrue(self.controller.add_rule(rule))
        self.assertEqual(self.controller.rules, [rule])
        self.assertEqual(self.runtime.rewrite_rules, [rule])
        self.assertEqual(CONFIG.get(CONFIG.rewrite_rules), [rule.to_dict()])

    def test_update_rule_replaces_in_place(self) -> None:
        self.controller.add_rule(make_rule("https://a.com/x"))
        self.assertTrue(
            self.controller.update_rule(
                0, make_rule("https://b.com/x", replacement="http://localhost:1/y")
            )
        )
        self.assertEqual(self.controller.rules[0].value, "https://b.com/x")
        self.assertEqual(self.controller.rules[0].replacement, "http://localhost:1/y")

    def test_update_rule_out_of_range_is_a_noop(self) -> None:
        self.assertFalse(self.controller.update_rule(3, make_rule("https://a.com/x")))

    def test_remove_rules_drops_the_given_rows(self) -> None:
        for host in ("a", "b", "c"):
            self.controller.add_rule(make_rule(f"https://{host}.com/x"))
        self.assertTrue(self.controller.remove_rules([0, 2, 99]))
        self.assertEqual([r.value for r in self.controller.rules], ["https://b.com/x"])
        self.assertEqual(len(CONFIG.get(CONFIG.rewrite_rules)), 1)

    def test_remove_rules_with_no_valid_row_is_a_noop(self) -> None:
        self.assertFalse(self.controller.remove_rules([7]))

    def test_set_enabled_toggles_and_persists(self) -> None:
        self.controller.add_rule(make_rule("https://a.com/x"))
        self.assertTrue(self.controller.set_enabled(0, False))
        self.assertFalse(self.controller.rules[0].enabled)
        self.assertFalse(self.runtime.rewrite_rules[0].enabled)
        self.assertFalse(self.controller.set_enabled(0, False))

    def test_move_rule_swaps_neighbours(self) -> None:
        for host in ("a", "b"):
            self.controller.add_rule(make_rule(f"https://{host}.com/x"))
        self.assertTrue(self.controller.move_rule(1, -1))
        self.assertEqual(
            [r.value for r in self.controller.rules],
            ["https://b.com/x", "https://a.com/x"],
        )

    def test_move_rule_past_either_end_is_a_noop(self) -> None:
        self.controller.add_rule(make_rule("https://a.com/x"))
        self.assertFalse(self.controller.move_rule(0, -1))
        self.assertFalse(self.controller.move_rule(0, 1))
        self.assertFalse(self.controller.move_rule(5, 1))

    def test_rules_changed_carries_a_copy(self) -> None:
        seen: list[list[RewriteRule]] = []
        self.controller.rules_changed.connect(seen.append)
        self.controller.add_rule(make_rule("https://a.com/x"))
        self.assertEqual(len(seen), 1)
        seen[0].clear()
        self.assertEqual(len(self.controller.rules), 1)

    def test_unusable_persisted_rule_is_kept_but_disabled(self) -> None:
        broken = RewriteRule(
            logic=RewriteLogic.REGEX, value="bad(", replacement="http://b.com/"
        )
        CONFIG.set(CONFIG.rewrite_rules, [broken.to_dict()])
        controller = RewriteController(mitm=MitmFacade(MitmRuntime()))
        self.assertEqual(len(controller.rules), 1)
        self.assertFalse(controller.rules[0].enabled)

    def test_rule_missing_a_replacement_is_kept_but_disabled(self) -> None:
        broken = make_rule("https://a.com/x", replacement="")
        CONFIG.set(CONFIG.rewrite_rules, [broken.to_dict()])
        controller = RewriteController(mitm=MitmFacade(MitmRuntime()))
        self.assertEqual(len(controller.rules), 1)
        self.assertFalse(controller.rules[0].enabled)


if __name__ == "__main__":
    unittest.main()
