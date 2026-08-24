"""断点窗口的测试：什么时候自己冒出来、什么时候自己收起来、关窗那三个出口。

这个窗口是**非模态**的独立顶层窗口，所以有两件容易悄悄坏掉的事必须锁住：

* **抢焦点只在队列空→非空那一次**。一条规则命中一次交互要停两次（请求期 + 响应期），
  风暴期每来一条就 `activateWindow()` 一次，会把正在打字的人反复踢出输入框。
* **队列非空时不许默默关掉**。窗口一藏，那几条流量还钉在 `wait_for_resume()` 上，
  客户端就那么转圈，而界面上再也看不到它们（本轮刻意没做超时自动放行）。

编辑器的只读态读的是 `flow.response is None`，和规则写了什么无关 —— 规则不选阶段。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.intercept.dialogs import HeldFlowsChoice
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

    def release_all(self) -> bool:
        self.calls.append(("release_all",))
        # 真控制器放行后会 `refresh_flows()` 再发一次空队列，窗口的自动隐藏依赖这一步。
        self.emit_flows([])
        return True

    def release_flows(self, flow_ids: list[str]) -> bool:
        self.calls.append(("release_flows", tuple(flow_ids)))
        return True

    def drop_flows(self, flow_ids: list[str]) -> bool:
        self.calls.append(("drop_flows", tuple(flow_ids)))
        return True

    def revert_flow(self, flow_id: str) -> bool:
        self.calls.append(("revert_flow", flow_id))
        return True

    def apply_request(self, flow_id: str, edit, *, release: bool = False) -> bool:
        self.calls.append(("apply_request", flow_id, release))
        return True

    def apply_response(self, flow_id: str, edit, *, release: bool = False) -> bool:
        self.calls.append(("apply_response", flow_id, release))
        return True

    def fake_response(self, flow_id: str, edit) -> bool:
        self.calls.append(("fake_response", flow_id))
        return True


class StubbedWindow(InterceptWindow):
    """把关窗确认框换成一个固定答案。

    `_confirm_close` 就是为此抽出来的：真跑 `exec()` 会开一个嵌套事件循环，
    离线测试里没人去点那三个按钮（按钮本身在 `test_dialogs.py` 里单独测）。
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
    """编辑器的分工按流量停在哪个阶段来，判据是 `flow.response is None`。"""

    def test_a_request_phase_flow_is_fully_editable(self) -> None:
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.assertFalse(win.request_editor.url_edit.isReadOnly())
        self.assertIs(win.editor_panel.stacked.currentWidget(), win.request_editor)

    def test_a_response_phase_flow_locks_the_request(self) -> None:
        """请求早发出去了，改它没有任何效果，锁只读比让人白改一场好。"""
        win = self.window()
        self.controller.emit_flows([flow_to(resp=True)])
        self.assertTrue(win.request_editor.url_edit.isReadOnly())
        self.assertFalse(win.response_editor.status_edit.isReadOnly())
        self.assertIs(win.editor_panel.stacked.currentWidget(), win.response_editor)

    def test_faking_a_response_is_request_phase_only(self) -> None:
        """原生要求 flow.response 为空才能挂一条伪造响应上去。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.assertTrue(win.fake_btn.isEnabled())
        self.controller.emit_flows([flow_to(resp=True)])
        self.assertFalse(win.fake_btn.isEnabled())

    def test_the_write_back_follows_the_phase(self) -> None:
        win = self.window()
        self.controller.emit_flows([flow_to()])
        win.release_btn.click()
        self.controller.emit_flows([flow_to(resp=True)])
        win.apply_btn.click()
        self.assertEqual(
            [(call[0], call[-1]) for call in self.controller.calls],
            [("apply_request", True), ("apply_response", False)],
        )


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

    def test_releasing_everything_empties_the_queue(self) -> None:
        win = self.window(HeldFlowsChoice.RELEASE_ALL)
        self.controller.emit_flows([flow_to(), flow_to()])
        self.assertTrue(win.close())
        self.assertEqual(self.controller.calls, [("release_all",)])
        self.assertFalse(win.isVisible())


class ActionTests(InterceptWindowTestCase):
    def test_release_all_is_reachable_from_the_bar(self) -> None:
        """风暴期的逃生口。非模态就是为了让它一直点得到。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.assertTrue(win.release_all_btn.isEnabled())
        win.release_all_btn.click()
        self.assertEqual(self.controller.calls, [("release_all",)])

    def test_nothing_selected_disables_the_per_flow_actions(self) -> None:
        win = self.window()
        for btn in (win.release_btn, win.apply_btn, win.fake_btn, win.drop_btn):
            with self.subTest(btn=btn.text()):
                self.assertFalse(btn.isEnabled())

    def test_dropping_sends_the_selected_ids(self) -> None:
        win = self.window()
        flow = flow_to()
        self.controller.emit_flows([flow])
        win.drop_btn.click()
        self.assertEqual(self.controller.calls, [("drop_flows", (flow.id,))])

    def test_reverting_needs_an_edited_flow(self) -> None:
        """`Flow.modified()` 的真实语义是「有备份可撤销」。"""
        win = self.window()
        self.controller.emit_flows([flow_to()])
        self.assertFalse(win.revert_btn.isEnabled())
        edited = flow_to()
        edited.backup()
        self.controller.emit_flows([edited])
        self.assertTrue(win.revert_btn.isEnabled())

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
