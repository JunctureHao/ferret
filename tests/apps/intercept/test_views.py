"""断点页的测试：这一页只剩规则，以及那个通往断点窗口的入口。

上一版的队列和编辑器长在这一页里，命中时界面毫无反应 —— 用户停在捕获页就什么都看不到。
现在队列搬进了独立的 `InterceptWindow`，这一页只留一个带计数的入口按钮。它要守住两件事：

* **队列构件真的搬走了**，没有留下一份会跟窗口抢同一批快照的影子实现；
* **入口按钮的计数与启用态跟着 `flows_changed` 走** —— 用户在关窗时选了
  「保持挂起并隐藏」之后，它是唯一能把那些还钉着客户端的流量找回来的路。
"""

import os
import typing
import unittest
import unittest.mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.intercept.views import InterceptInterface
from ferret.core.mitm import InterceptRule

app = QApplication.instance() or QApplication([])


class FakeController(QObject):
    """只提供这一页真正连的那几条信号和那几个方法（真控制器同样是 `QObject`）。"""

    rules_changed = Signal(list)
    enabled_changed = Signal(bool)
    flows_changed = Signal(list)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.rules: list[InterceptRule] = []
        self.enabled = False
        self.flows: list = []
        self.refresh_calls = 0

    def emit_flows(self, count: int) -> None:
        self.flows = [tflow.tflow() for _ in range(count)]
        self.flows_changed.emit(self.flows)

    def refresh_flows(self) -> None:
        self.refresh_calls += 1

    def rule_at(self, index: int) -> InterceptRule | None:
        return self.rules[index] if 0 <= index < len(self.rules) else None

    def set_intercept_enabled(self, enabled: bool) -> bool:
        self.enabled = enabled
        return True

    def set_enabled(self, index: int, enabled: bool) -> bool:
        return True

    def add_rule(self, rule: InterceptRule) -> bool:
        return True

    def update_rule(self, index: int, rule: InterceptRule) -> bool:
        return True

    def remove_rules(self, indexes: list[int]) -> bool:
        return True


class InterceptInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FakeController()
        self.iface = InterceptInterface(self.controller)  # type: ignore
        self.addCleanup(app.processEvents)
        self.addCleanup(self.iface.deleteLater)

    def test_the_queue_widgets_moved_out(self) -> None:
        """队列只能有一份。这一页留个影子实现就会和窗口抢同一批快照。"""
        for name in (
            "flow_model",
            "flow_table",
            "editor_panel",
            "request_editor",
            "response_editor",
            "release_btn",
            "release_all_btn",
            "queue_stack",
            "page_pivot",
            "page_stack",
        ):
            with self.subTest(widget=name):
                self.assertFalse(hasattr(self.iface, name))

    def test_the_rule_table_and_master_switch_stayed(self) -> None:
        for name in ("rule_table", "add_btn", "edit_btn", "delete_btn"):
            with self.subTest(widget=name):
                self.assertTrue(hasattr(self.iface, name))
        self.assertFalse(self.iface.enable_switch.isChecked())

    def test_one_bar_holds_the_switch_the_entry_and_the_rule_actions(self) -> None:
        """总开关、队列入口和增删改同排；搜索由标题栏统一承载。"""
        bar = self.iface.add_btn.parentWidget()
        assert bar is not None
        for name in (
            "edit_btn",
            "delete_btn",
            "queue_btn",
            "enable_switch",
        ):
            with self.subTest(widget=name):
                self.assertIs(getattr(self.iface, name).parentWidget(), bar)
        # 徽标要跟按钮同父（挂在按钮上会被布局裁掉），但它不占布局位置。
        self.assertIs(self.iface.queue_badge.parentWidget(), bar)
        layout = bar.layout()
        assert layout is not None
        items = [layout.itemAt(i) for i in range(layout.count())]
        widgets = [item.widget() for item in items if item is not None]
        self.assertEqual(
            widgets,
            [
                self.iface.add_btn,
                None,  # addStretch
                self.iface.edit_btn,
                self.iface.delete_btn,
                None,  # addSpacing
                self.iface.queue_btn,
                None,  # addSpacing
                self.iface.enable_switch,
            ],
        )

    def test_search_protocol_filters_rules_and_keeps_text_for_the_global_box(
        self,
    ) -> None:
        self.controller.rules = [
            InterceptRule(value="alpha.example"),
            InterceptRule(value="beta.example"),
        ]
        self.controller.rules_changed.emit(self.controller.rules)
        self.iface.apply_search("  ALPHA  ")
        self.assertEqual(self.iface.current_search_text(), "  ALPHA  ")
        self.assertEqual(self.iface.rule_proxy.rowCount(), 1)
        self.assertIs(self.iface.search_focus_target(), self.iface.rule_table)
        self.iface.apply_search("")
        self.assertEqual(self.iface.current_search_text(), "")
        self.assertEqual(self.iface.rule_proxy.rowCount(), 2)

    def test_the_page_asks_for_the_queue_on_the_way_up(self) -> None:
        """页面起得比内核晚，起来时可能已经有拦下的流量在等着了。"""
        self.assertEqual(self.controller.refresh_calls, 1)

    def test_an_empty_queue_leaves_no_entry(self) -> None:
        btn, badge = self.iface.queue_btn, self.iface.queue_badge
        self.assertFalse(btn.isEnabled())
        self.assertFalse(badge.isVisibleTo(self.iface))

    def test_the_entry_button_counts_the_queue(self) -> None:
        # 徽标读 `isVisibleTo`：这一页此刻没有显示，藏起来的父窗口下 `isVisible()`
        # 恒为假，量不出「是不是被显式藏了」。
        btn, badge = self.iface.queue_btn, self.iface.queue_badge
        self.controller.emit_flows(3)
        self.assertTrue(btn.isEnabled())
        self.assertEqual(badge.text(), "3")
        self.assertTrue(badge.isVisibleTo(self.iface))

    def test_a_drained_queue_takes_the_entry_away(self) -> None:
        btn, badge = self.iface.queue_btn, self.iface.queue_badge
        self.controller.emit_flows(2)
        self.controller.emit_flows(0)
        self.assertFalse(btn.isEnabled())
        self.assertFalse(badge.isVisibleTo(self.iface))
        self.assertEqual(badge.text(), "0")

    def test_the_entry_button_asks_for_the_window(self) -> None:
        """这一页不认识那个窗口，只发信号 —— 由 `MainWindow` 牵线（沿用既有先例）。"""
        seen: list[int] = []
        self.iface.queue_requested.connect(lambda: seen.append(1))
        self.controller.emit_flows(1)
        self.iface.queue_btn.click()
        self.assertEqual(seen, [1])


class _MenuStub(QObject):
    closedSignal = Signal()

    """替身菜单：真 `RoundMenu.exec` 会弹出非阻塞菜单，离线测试没人点它。

    必须是 `QObject`：菜单动作把菜单当 parent 构造，QAction 拒绝 MagicMock。
    """

    created: typing.ClassVar[list] = []

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        _MenuStub.created.append(self)

    def addAction(self, action) -> None:
        pass

    def exec(self, pos) -> None:
        pass


class ContextMenuSelectionTests(unittest.TestCase):
    """右键先选中光标下的规则（issues #50）：qfw 表格默认不开右键选行，选 A
    右键 B 时启停/删除还落在 A 上。"""

    def setUp(self) -> None:
        self.controller = FakeController()
        self.iface = InterceptInterface(self.controller)  # type: ignore
        self.iface.resize(700, 400)
        self.addCleanup(app.processEvents)
        self.addCleanup(self.iface.deleteLater)
        _MenuStub.created.clear()
        patcher = unittest.mock.patch(
            "ferret.apps.intercept.views.RoundMenu", _MenuStub
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _push_rules(self) -> None:
        self.controller.rules = [
            InterceptRule(value="a.example.com"),
            InterceptRule(value="b.example.com"),
        ]
        self.controller.rules_changed.emit(self.controller.rules)

    def test_right_clicking_an_unselected_row_selects_it(self) -> None:
        self._push_rules()
        self.iface.rule_table.selectRow(0)
        pos = self.iface.rule_table.visualRect(
            self.iface.rule_proxy.index(1, 0)
        ).center()
        self.iface._on_rule_context_menu(pos)
        self.assertEqual(len(_MenuStub.created), 1)
        self.assertEqual(self.iface._selected_rule_rows(), [1])

    def test_right_clicking_empty_space_shows_no_menu(self) -> None:
        self._push_rules()
        self.iface.rule_table.selectRow(0)
        # 页面还没经过真实布局，视口很小；直接给一个远超两行内容的坐标。
        self.iface._on_rule_context_menu(QPoint(5, 100000))
        self.assertEqual(_MenuStub.created, [])


if __name__ == "__main__":
    unittest.main()
