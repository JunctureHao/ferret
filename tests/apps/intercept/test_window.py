"""断点窗口的测试：什么时候自己冒出来、什么时候自己收起来、关窗那三个出口。

这个窗口是**非模态**的独立顶层窗口，所以有两件容易悄悄坏掉的事必须锁住：

* **抢焦点只在队列空→非空那一次**。一条规则命中一次交互要停两次（请求期 + 响应期），
  风暴期每来一条就 `activateWindow()` 一次，会把正在打字的人反复踢出输入框。
* **队列非空时不许默默关掉**。窗口一藏，那几条流量还钉在 `wait_for_resume()` 上，
  客户端就那么转圈，而界面上再也看不到它们（本轮刻意没做超时自动放行）。

编辑区的标签与只读态读的是 `flow.response is None`，和规则写了什么阶段无关。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from qfluentwidgets import PushButton

from ferret.apps.intercept.dialogs import HeldFlowsChoice
from ferret.apps.intercept.models import HeldFlowTableModel
from ferret.apps.intercept.window import InterceptWindow

app = QApplication.instance() or QApplication([])


def flow_to(url: str = "http://api.example.com/v1", *, resp: bool = False):
    flow = tflow.tflow(resp=resp)
    flow.request.url = url
    return flow


def snapshot(flow):
    """照 `facade._snapshot` 的做法：`copy()` 完把 id 补回去。

    队列每次刷新拿到的都是这种副本 —— 新对象、老 id。光 `copy()` 会换一个新 uuid，
    那样界面就再也认不出「这还是我正在改的那条」了。
    """
    clone = flow.copy()
    clone.id = flow.id
    return clone


class FakeController(QObject):
    """只提供窗口真正连的那几条信号和那几个方法。

    刻意继承 `QObject`（真控制器也是 `QObject`）—— 换成 `QWidget` 就多了一个顶层
    控件，解释器退出时两个顶层控件一起销毁，实测会段错误。
    """

    flows_changed = Signal(list)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.flows: list = []
        self.calls: list[tuple] = []

    def emit_flows(self, flows) -> None:
        self.flows = list(flows)
        self.flows_changed.emit(self.flows)

    def release_flows(self, flow_ids: list[str]) -> bool:
        self.calls.append(("release_flows", tuple(flow_ids)))
        return True

    def drop_flows(self, flow_ids: list[str]) -> bool:
        self.calls.append(("drop_flows", tuple(flow_ids)))
        return True

    def apply_request(self, flow_id: str, edit, *, release: bool = False) -> bool:
        self.calls.append(("apply_request", flow_id, release))
        return True

    def apply_response(self, flow_id: str, edit, *, release: bool = False) -> bool:
        self.calls.append(("apply_response", flow_id, release))
        return True


class StubbedWindow(InterceptWindow):
    """把关窗确认框换成一个固定答案。

    `_confirm_close` 就是为此抽出来的：真跑 `exec()` 会开一个嵌套事件循环，
    离线测试里没人去点那两个按钮（按钮本身在 `test_dialogs.py` 里单独测）。
    """

    def __init__(self, controller, choice: HeldFlowsChoice) -> None:
        super().__init__(controller)
        self._choice = choice
        self.asked = 0

    def _confirm_close(self) -> HeldFlowsChoice:
        self.asked += 1
        return self._choice


class InterceptWindowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = FakeController()
        # 注册得最早 → LIFO 里跑得最晚：等 deleteLater 真正落地再退出这个用例。
        self.addCleanup(app.processEvents)

    def window(self, choice: HeldFlowsChoice = HeldFlowsChoice.CANCEL) -> StubbedWindow:
        win = StubbedWindow(self.controller, choice)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.hide)
        return win


class ConstructionTests(InterceptWindowTestCase):
    def test_a_fresh_window_stays_hidden(self) -> None:
        """队列空着的时候不许自己冒出来 —— 它是跟主窗口一起建的，不是用户叫的。"""
        win = self.window()
        self.assertFalse(win.isVisible())
        self.assertEqual(win.windowTitle(), "Breakpoints")

    def test_the_window_has_no_qt_parent(self) -> None:
        """`updateFrameless()` 不补 `Qt.Window`：给了 Qt 父对象就退化成子控件。"""
        win = self.window()
        self.assertIsNone(win.parent())
        self.assertTrue(win.isWindow())

    def test_an_empty_queue_shows_the_fallback_page(self) -> None:
        win = self.window()
        self.assertIs(win.queue_stack.currentWidget(), win.queue_empty_page)

    def test_the_title_bar_height_is_reserved(self) -> None:
        """标题栏是浮在窗口上的兄弟控件、不进布局，不留边距内容会被它压住。"""
        win = self.window()
        top = win.layout().contentsMargins().top()
        self.assertEqual(top, win.titleBar.height())
        self.assertGreater(top, 0)


class PopUpTests(InterceptWindowTestCase):
    def test_the_first_batch_pops_the_window_up(self) -> None:
        win = self.window()
        seen: list[int] = []
        win.attention_requested.connect(seen.append)
        self.controller.emit_flows([flow_to(), flow_to(resp=True)])
        self.assertTrue(win.isVisible())
        self.assertEqual(seen, [2])

    def test_later_arrivals_do_not_grab_focus_again(self) -> None:
        """一条规则命中一次交互就要停两次，每次都抢焦点等于没法打字。"""
        win = self.window()
        seen: list[int] = []
        win.attention_requested.connect(seen.append)
        self.controller.emit_flows([flow_to()])
        self.controller.emit_flows([flow_to(), flow_to()])
        self.assertEqual(seen, [1])

    def test_the_title_counts_the_queue(self) -> None:
        win = self.window()
        self.controller.emit_flows([flow_to(), flow_to()])
        self.assertEqual(win.windowTitle(), "Breakpoints · 2 pending")

    def test_an_emptied_queue_hides_the_window(self) -> None:
        """不留一个空窗挡着主窗口。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.controller.emit_flows([])
        self.assertFalse(win.isVisible())
        self.assertEqual(win.windowTitle(), "Breakpoints")

    def test_emptying_and_refilling_pops_up_again(self) -> None:
        """空→非空的边沿可以再来一次，不是一次性的。"""
        win = self.window()
        seen: list[int] = []
        win.attention_requested.connect(seen.append)
        self.controller.emit_flows([flow_to()])
        self.controller.emit_flows([])
        self.controller.emit_flows([flow_to()])
        self.assertTrue(win.isVisible())
        self.assertEqual(seen, [1, 1])

    def test_pop_up_is_what_the_queue_entry_button_calls(self) -> None:
        """「保持挂起并隐藏」之后，断点页那个入口按钮是唯一的回去路径。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        win.hide()
        win.pop_up()
        self.assertTrue(win.isVisible())


class PhaseTests(InterceptWindowTestCase):
    """编辑区标签的分工按流量停在哪个阶段来，判据是 `flow.response is None`。"""

    def test_a_request_phase_flow_is_fully_editable(self) -> None:
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.assertFalse(win.request_editor.url_edit.isReadOnly())
        self.assertIs(win.editor_panel.currentWidget(), win.request_editor)

    def test_a_response_phase_flow_locks_the_request(self) -> None:
        """请求早发出去了，改它没有任何效果，锁只读比让人白改一场好。"""
        win = self.window()
        self.controller.emit_flows([flow_to(resp=True)])
        self.assertTrue(win.request_editor.url_edit.isReadOnly())
        self.assertTrue(win.request_editor.method_edit.isReadOnly())
        self.assertFalse(win.response_editor.status_edit.isReadOnly())
        self.assertIs(win.editor_panel.currentWidget(), win.response_editor)

    def test_the_response_tab_shows_a_placeholder_before_the_request_goes_out(self) -> None:
        """请求期没有响应可编：切到「响应」标签只能看到一句提示，不给空表单。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        win.editor_nav.setCurrentItem("Response")
        self.assertIs(win.editor_panel.currentWidget(), win.response_pending_page)
        # 切回请求标签，编辑器还在、还是可编辑的那张。
        win.editor_nav.setCurrentItem("Request")
        self.assertIs(win.editor_panel.currentWidget(), win.request_editor)

    def test_the_response_tab_shows_the_editor_once_answered(self) -> None:
        """响应期两张标签都看得：请求只读供查看，响应可编辑。"""
        win = self.window()
        self.controller.emit_flows([flow_to(resp=True)])
        win.editor_nav.setCurrentItem("Request")
        self.assertIs(win.editor_panel.currentWidget(), win.request_editor)
        self.assertTrue(win.request_editor.url_edit.isReadOnly())
        win.editor_nav.setCurrentItem("Response")
        self.assertIs(win.editor_panel.currentWidget(), win.response_editor)

    def test_releasing_writes_back_and_releases_in_one_step(self) -> None:
        """没有单独的「应用改动」：放行（Ctrl+Enter）就是写回 + 放行，按阶段选编辑器。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        win._on_release()
        self.controller.emit_flows([flow_to(resp=True)])
        win._on_release()
        self.assertEqual(
            [(call[0], call[-1]) for call in self.controller.calls],
            [("apply_request", True), ("apply_response", True)],
        )

    def test_releasing_with_nothing_selected_does_nothing(self) -> None:
        win = self.window()
        win._on_release()
        self.assertEqual(self.controller.calls, [])


class CloseTests(InterceptWindowTestCase):
    def test_an_empty_queue_closes_without_asking(self) -> None:
        win = self.window()
        self.assertTrue(win.close())
        self.assertEqual(win.asked, 0)

    def test_cancelling_keeps_the_window_open(self) -> None:
        win = self.window(HeldFlowsChoice.CANCEL)
        self.controller.emit_flows([flow_to()])
        self.assertFalse(win.close())
        self.assertEqual(win.asked, 1)
        self.assertTrue(win.isVisible())
        self.assertEqual(self.controller.calls, [])

    def test_keeping_them_held_hides_without_releasing(self) -> None:
        """流量还钉着客户端，但这是用户明示选的 —— 计数留在断点页的入口按钮上。"""
        win = self.window(HeldFlowsChoice.KEEP_HELD)
        self.controller.emit_flows([flow_to()])
        self.assertTrue(win.close())
        self.assertFalse(win.isVisible())
        self.assertEqual(self.controller.calls, [])


class ActionTests(InterceptWindowTestCase):
    def row_buttons(self, win: StubbedWindow, row: int) -> list:
        """一行操作格里的按钮。`PrimaryPushButton` 是 `PushButton` 的子类，
        按 `PushButton` 找就全在，再单独数主按钮。"""
        host = win.flow_table.indexWidget(
            win.flow_model.index(row, HeldFlowTableModel.ACTIONS_COLUMN)
        )
        assert host is not None
        return host.findChildren(PushButton)

    def test_every_row_gets_its_own_release_and_drop(self) -> None:
        """操作列长在行上：一格一组「放行 / 丢弃」，刷新后跟着快照重建。"""
        win = self.window()
        self.controller.emit_flows([flow_to(), flow_to(resp=True)])
        for row in range(2):
            with self.subTest(row=row):
                self.assertEqual(len(self.row_buttons(win, row)), 2)
        self.controller.emit_flows([flow_to()])
        self.assertEqual(len(self.row_buttons(win, 0)), 2)

    def test_the_row_release_button_writes_back_that_row(self) -> None:
        """点哪行放哪行：先把它选成当前流量，再写回 + 放行。"""
        win = self.window()
        flow = flow_to()
        self.controller.emit_flows([flow])
        win._on_row_release(flow)
        self.assertEqual(self.controller.calls, [("apply_request", flow.id, True)])

    def test_the_row_release_button_selects_the_row_first(self) -> None:
        """多选时点其中一行的放行：只放这一行，别把别的一起送出去。"""
        win = self.window()
        keep, other = flow_to(), flow_to("http://cdn.example.com/a.js")
        self.controller.emit_flows([keep, other])
        win._on_row_release(other)
        self.assertEqual(self.controller.calls, [("apply_request", other.id, True)])

    def test_releasing_a_row_that_is_already_gone_does_nothing(self) -> None:
        """刷新间隙流量已经不在队列里：别把别的流量放出去。"""
        win = self.window()
        flow = flow_to()
        self.controller.emit_flows([flow])
        win._on_row_release(snapshot(flow_to("http://gone.example.com/")))
        self.assertEqual(self.controller.calls, [])

    def test_the_row_drop_button_only_drops_that_row(self) -> None:
        win = self.window()
        first, second = flow_to(), flow_to("http://cdn.example.com/a.js")
        self.controller.emit_flows([first, second])
        win._on_row_drop(second)
        self.assertEqual(self.controller.calls, [("drop_flows", (second.id,))])

    def test_dropping_the_selection_sends_the_selected_ids(self) -> None:
        """右键菜单那条路（多选）还在：选中几条丢几条。"""
        win = self.window()
        flow = flow_to()
        self.controller.emit_flows([flow])
        win._on_drop()
        self.assertEqual(self.controller.calls, [("drop_flows", (flow.id,))])

    def test_the_tabs_sit_above_the_editor_stack(self) -> None:
        """编辑区先是「请求 / 响应」两个标签，下面才是栈。"""
        win = self.window()
        layout = win.editor_box.layout()
        assert layout is not None
        self.assertIs(layout.itemAt(0).widget(), win.editor_nav)
        self.assertIs(layout.itemAt(1).widget(), win.editor_panel)

    def test_the_edited_flow_stays_selected_across_a_refresh(self) -> None:
        """快照每次都是新对象，认人只能靠 id —— 别人被拦下不该冲掉我正在改的。"""
        win = self.window()
        keep, other = flow_to(), flow_to("http://cdn.example.com/a.js")
        self.controller.emit_flows([keep])
        win.request_editor.url_edit.setText("http://edited.example.com/")
        # 新来的那条排在前面：按行号认人就会把编辑器切到它身上去。
        self.controller.emit_flows([other, snapshot(keep)])
        self.assertEqual(
            win.request_editor.url_edit.text(), "http://edited.example.com/"
        )


if __name__ == "__main__":
    unittest.main()
