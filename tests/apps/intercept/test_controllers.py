"""`InterceptController` 的测试：规则落盘/下发，以及拦截队列的刷新与写回。

队列**不自己记账**，每次都向 `MitmFacade.intercepted_flows()` 重新要一份快照 ——
内核那边有三种「谁攥着这条流量」的情形（断点 addon、网关挂起策略、原生 addon），
本地记账必然对不齐。所以这里用一个假门面把「问了几次」直接数出来。
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication
from qfluentwidgets import qconfig

from ferret.apps.intercept.controllers import InterceptController
from ferret.core.mitm import (
    InterceptLogic,
    InterceptRule,
    MitmFacade,
    MitmRuntime,
    RequestEdit,
    ResponseEdit,
)
from ferret.core.settings import CONFIG

app = QApplication.instance() or QApplication([])


def make_rule(value: str = "api.example.com", **kwargs) -> InterceptRule:
    return InterceptRule(value=value, **kwargs)


BROKEN = make_rule("bad(", logic=InterceptLogic.REGEX)


class FakeRuntime(QObject):
    """只提供控制器真正连的那三条信号。"""

    flow_intercepted = Signal(object)
    ready = Signal(object)
    stopped = Signal()


class FakeFacade:
    """记账用的假门面：谁被调了几次、抛不抛，全在这里摆明。"""

    def __init__(self) -> None:
        self.runtime = FakeRuntime()
        self.rules: list[InterceptRule] = []
        self.enabled = False
        self.flows: list = []
        self.calls: list[str] = []
        self.snapshot_calls = 0
        self.fail_with: Exception | None = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def set_intercept_rules(self, rules: list[InterceptRule]) -> None:
        self._maybe_fail()
        # 真门面会在这里编译表达式，坏规则当场抛 —— 假门面照抄这个时机。
        for rule in rules:
            if rule.enabled and rule.value.strip():
                rule.validate()
        self.rules = list(rules)

    def set_intercept_enabled(self, enabled: bool) -> None:
        self._maybe_fail()
        self.enabled = enabled

    def intercepted_flows(self) -> list:
        self.snapshot_calls += 1
        self._maybe_fail()
        return list(self.flows)

    def release_flows(self, flow_ids: list[str]) -> int:
        self.calls.append(f"release:{','.join(flow_ids)}")
        self._maybe_fail()
        return len(flow_ids)

    def drop_flows(self, flow_ids: list[str]) -> int:
        self.calls.append(f"drop:{','.join(flow_ids)}")
        self._maybe_fail()
        return len(flow_ids)

    def release_all_intercepted(self) -> int:
        self.calls.append("release_all")
        self._maybe_fail()
        return len(self.flows)

    def revert_flow(self, flow_id: str) -> None:
        self.calls.append(f"revert:{flow_id}")
        self._maybe_fail()

    def apply_request_edits(
        self, flow_id: str, edit: RequestEdit, *, release: bool = False
    ) -> None:
        self.calls.append(f"request:{flow_id}:{release}:{edit.method}")
        self._maybe_fail()

    def apply_response_edits(
        self, flow_id: str, edit: ResponseEdit, *, release: bool = False
    ) -> None:
        self.calls.append(f"response:{flow_id}:{release}:{edit.status_code}")
        self._maybe_fail()

    def fake_response(self, flow_id: str, edit: ResponseEdit) -> None:
        self.calls.append(f"fake:{flow_id}:{edit.status_code}")
        self._maybe_fail()


class ConfigSandbox(unittest.TestCase):
    """所有子类共用：CONFIG 指向临时文件，绝不碰用户真实的 config.json。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(setattr, CONFIG, "file", CONFIG.file)
        qconfig.load(str(Path(self._tmp.name) / "config.json"), CONFIG)
        # 先清空再交还 CONFIG.file，避免临时目录删掉后还留着规则。
        self.addCleanup(CONFIG.set, CONFIG.intercept_enabled, False)
        self.addCleanup(CONFIG.set, CONFIG.intercept_rules, [])


class InterceptRuleTests(ConfigSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.facade = FakeFacade()
        self.controller = InterceptController(mitm=self.facade)  # type: ignore

    def test_starts_empty_and_pushes_down_on_construction(self) -> None:
        """构造就下发一次：内核可能已经在跑，界面刚建好不该有一段空窗期。"""
        self.assertEqual(self.controller.rules, [])
        self.assertEqual(self.facade.rules, [])
        self.assertFalse(self.facade.enabled)

    def test_the_master_switch_defaults_off(self) -> None:
        """断点会把客户端连接一直钉住等人处理，默认开等于一启动流量就卡住。"""
        self.assertFalse(self.controller.enabled)

    def test_add_rule_persists_and_pushes_down(self) -> None:
        rule = make_rule()
        self.assertTrue(self.controller.add_rule(rule))
        self.assertEqual(self.controller.rules, [rule])
        self.assertEqual(self.facade.rules, [rule])
        self.assertEqual(CONFIG.get(CONFIG.intercept_rules), [rule.to_dict()])

    def test_update_rule_replaces_in_place(self) -> None:
        self.controller.add_rule(make_rule())
        self.assertTrue(self.controller.update_rule(0, make_rule("cdn.example.com")))
        self.assertEqual(self.controller.rules[0].value, "cdn.example.com")

    def test_update_rule_out_of_range_is_a_noop(self) -> None:
        self.assertFalse(self.controller.update_rule(3, make_rule()))

    def test_remove_rules_drops_the_given_rows(self) -> None:
        for host in ("a", "b", "c"):
            self.controller.add_rule(make_rule(f"{host}.example.com"))
        self.assertTrue(self.controller.remove_rules([0, 2, 99]))
        self.assertEqual([r.value for r in self.controller.rules], ["b.example.com"])
        self.assertEqual(len(CONFIG.get(CONFIG.intercept_rules)), 1)

    def test_remove_rules_with_no_valid_row_is_a_noop(self) -> None:
        self.assertFalse(self.controller.remove_rules([7]))

    def test_set_enabled_toggles_and_persists(self) -> None:
        self.controller.add_rule(make_rule())
        self.assertTrue(self.controller.set_enabled(0, False))
        self.assertFalse(self.controller.rules[0].enabled)
        self.assertFalse(self.facade.rules[0].enabled)
        # 已经是这个值了，再设一次不该白落一次盘、白下发一次。
        self.assertFalse(self.controller.set_enabled(0, False))
        self.assertFalse(self.controller.set_enabled(9, True))

    def test_rule_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.controller.rule_at(0))
        self.assertIsNone(self.controller.rule_at(-1))

    def test_rules_changed_carries_a_copy(self) -> None:
        seen: list[list[InterceptRule]] = []
        self.controller.rules_changed.connect(seen.append)
        self.controller.add_rule(make_rule())
        self.assertEqual(len(seen), 1)
        seen[0].clear()
        self.assertEqual(len(self.controller.rules), 1)

    def test_a_rejected_batch_rolls_back_and_reports(self) -> None:
        """`options.update` 是原子的：一条坏表达式会把整批规则连坐回滚。"""
        self.controller.add_rule(make_rule())
        failures: list[tuple[str, str]] = []
        reverts: list[list[InterceptRule]] = []
        self.controller.operation_failed.connect(
            lambda title, detail: failures.append((title, detail))
        )
        self.controller.rules_changed.connect(reverts.append)
        self.assertFalse(self.controller.add_rule(BROKEN))
        self.assertEqual([r.value for r in self.controller.rules], ["api.example.com"])
        self.assertEqual(len(failures), 1)
        # 回滚也要广播，否则表格上还留着那条根本没生效的规则。
        self.assertEqual([r.value for r in reverts[-1]], ["api.example.com"])

    def test_a_disabled_broken_rule_is_allowed_through(self) -> None:
        """停用的规则不参与下发，留着让用户回头改。"""
        from dataclasses import replace

        self.assertTrue(self.controller.add_rule(replace(BROKEN, enabled=False)))

    def test_a_broken_persisted_rule_is_kept_but_disabled(self) -> None:
        """手改坏的 config 不该让整批规则失效，也不该悄悄消失。"""
        CONFIG.set(CONFIG.intercept_rules, [BROKEN.to_dict()])
        controller = InterceptController(mitm=FakeFacade())  # type: ignore
        self.assertEqual(len(controller.rules), 1)
        self.assertFalse(controller.rules[0].enabled)
        self.assertEqual(controller.rules[0].value, "bad(")

    def test_the_master_switch_persists_and_announces(self) -> None:
        seen: list[bool] = []
        self.controller.enabled_changed.connect(seen.append)
        self.assertTrue(self.controller.set_intercept_enabled(True))
        self.assertTrue(self.controller.enabled)
        self.assertTrue(self.facade.enabled)
        self.assertTrue(CONFIG.get(CONFIG.intercept_enabled))
        self.assertEqual(seen, [True])
        # 同值再设一次是空操作。
        self.assertFalse(self.controller.set_intercept_enabled(True))
        self.assertEqual(seen, [True])

    def test_a_rejected_master_switch_snaps_back(self) -> None:
        self.facade.fail_with = RuntimeError("kernel is restarting")
        seen: list[bool] = []
        self.controller.enabled_changed.connect(seen.append)
        self.assertFalse(self.controller.set_intercept_enabled(True))
        self.assertFalse(self.controller.enabled)
        # 广播回原值，让界面上的开关自己弹回去。
        self.assertEqual(seen, [False])

    def test_a_persisted_switch_is_pushed_down_on_construction(self) -> None:
        CONFIG.set(CONFIG.intercept_enabled, True)
        facade = FakeFacade()
        controller = InterceptController(mitm=facade)  # type: ignore
        self.assertTrue(controller.enabled)
        self.assertTrue(facade.enabled)


class InterceptQueueTests(ConfigSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.facade = FakeFacade()
        self.controller = InterceptController(mitm=self.facade)  # type: ignore
        self.seen: list[list] = []
        self.controller.flows_changed.connect(self.seen.append)

    def test_a_burst_of_intercepts_costs_one_snapshot(self) -> None:
        """每拦一条就跨线程问一次太贵：同一轮事件循环里攒着，末尾问一次。"""
        before = self.facade.snapshot_calls
        self.facade.flows = [tflow.tflow(), tflow.tflow()]
        for _ in range(5):
            self.facade.runtime.flow_intercepted.emit(object())
        app.processEvents()
        self.assertEqual(self.facade.snapshot_calls, before + 1)
        self.assertEqual(len(self.controller.flows), 2)
        self.assertEqual(len(self.seen[-1]), 2)

    def test_the_next_burst_is_scheduled_again(self) -> None:
        """合并只在一轮之内：上一轮问完了，下一轮还得再问。"""
        self.facade.runtime.flow_intercepted.emit(object())
        app.processEvents()
        first = self.facade.snapshot_calls
        self.facade.runtime.flow_intercepted.emit(object())
        app.processEvents()
        self.assertEqual(self.facade.snapshot_calls, first + 1)

    def test_ready_also_asks_for_a_snapshot(self) -> None:
        """内核刚起来时可能已经拦着东西了（规则是构造时就下发的）。"""
        before = self.facade.snapshot_calls
        self.facade.runtime.ready.emit(object())
        app.processEvents()
        self.assertEqual(self.facade.snapshot_calls, before + 1)

    def test_an_unreachable_kernel_is_not_an_error(self) -> None:
        """起停窗口期问不到不是用户的错：等下一次信号，不清队列也不报。"""
        self.facade.flows = [tflow.tflow()]
        self.controller.refresh_flows()
        self.assertEqual(len(self.controller.flows), 1)
        self.facade.fail_with = TimeoutError("kernel busy")
        self.controller.refresh_flows()
        self.assertEqual(len(self.controller.flows), 1)
        self.assertEqual(len(self.seen), 1)

    def test_a_stopped_kernel_empties_the_queue(self) -> None:
        """快照里的 flow 是内核线程上的活对象，内核停了它们就不该再摆在界面上。"""
        self.facade.flows = [tflow.tflow()]
        self.controller.refresh_flows()
        self.facade.runtime.stopped.emit()
        self.assertEqual(self.controller.flows, [])
        self.assertEqual(self.seen[-1], [])

    def test_stopping_reopens_the_coalescing_latch(self) -> None:
        """停机清掉的是「本轮已排过刷新」这个标记，重启后第一条拦截照样要刷新。

        排在事件队列里的那一次刷新并**没有**被撤销（它撤不掉），但停机后的内核会
        让 `intercepted_flows()` 返回空，所以它捞不回任何东西。
        """
        self.facade.runtime.flow_intercepted.emit(object())
        self.facade.runtime.stopped.emit()
        self.facade.flows = []
        app.processEvents()
        self.assertEqual(self.controller.flows, [])
        self.facade.flows = [tflow.tflow()]
        before = self.facade.snapshot_calls
        self.facade.runtime.flow_intercepted.emit(object())
        app.processEvents()
        self.assertEqual(self.facade.snapshot_calls, before + 1)
        self.assertEqual(len(self.controller.flows), 1)

    def test_flows_changed_carries_a_copy(self) -> None:
        self.facade.flows = [tflow.tflow()]
        self.controller.refresh_flows()
        self.seen[-1].clear()
        self.assertEqual(len(self.controller.flows), 1)

    def test_flow_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.controller.flow_at(0))
        self.assertIsNone(self.controller.flow_at(-1))
        self.facade.flows = [tflow.tflow()]
        self.controller.refresh_flows()
        self.assertIsNotNone(self.controller.flow_at(0))


class InterceptWriteBackTests(ConfigSandbox):
    def setUp(self) -> None:
        super().setUp()
        self.facade = FakeFacade()
        self.facade.flows = [tflow.tflow()]
        self.controller = InterceptController(mitm=self.facade)  # type: ignore
        self.failures: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.controller.operation_failed.connect(
            lambda title, detail: self.failures.append((title, detail))
        )
        self.controller.operation_succeeded.connect(self.messages.append)

    def test_release_and_drop_reach_the_kernel(self) -> None:
        self.assertTrue(self.controller.release_flows(["a", "b"]))
        self.assertTrue(self.controller.drop_flows(["c"]))
        self.assertEqual(self.facade.calls, ["release:a,b", "drop:c"])
        self.assertEqual(len(self.messages), 2)

    def test_release_with_no_ids_is_a_noop(self) -> None:
        self.assertFalse(self.controller.release_flows([]))
        self.assertFalse(self.controller.drop_flows([]))
        self.assertEqual(self.facade.calls, [])

    def test_release_all_covers_flows_this_page_did_not_hold(self) -> None:
        """网关挂起和原生 addon 拦下的流量也算：队列是扫 `intercepted` 得来的。"""
        self.assertTrue(self.controller.release_all())
        self.assertEqual(self.facade.calls, ["release_all"])

    def test_release_all_with_an_empty_queue_reports_nothing(self) -> None:
        self.facade.flows = []
        self.assertFalse(self.controller.release_all())
        self.assertEqual(self.messages, [])

    def test_a_successful_release_re_asks_for_the_queue(self) -> None:
        """放行完队列就变了，必须重新要一份快照而不是本地删一行。"""
        before = self.facade.snapshot_calls
        self.controller.release_flows(["a"])
        self.assertEqual(self.facade.snapshot_calls, before + 1)

    def test_a_failed_release_reports_and_leaves_the_queue_alone(self) -> None:
        self.facade.fail_with = RuntimeError("kernel is gone")
        before = self.facade.snapshot_calls
        self.assertFalse(self.controller.release_flows(["a"]))
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.facade.snapshot_calls, before)

    def test_a_failed_release_all_reports(self) -> None:
        self.facade.fail_with = TimeoutError("too slow")
        self.assertFalse(self.controller.release_all())
        self.assertEqual(len(self.failures), 1)

    def test_revert_delegates(self) -> None:
        self.assertTrue(self.controller.revert_flow("f1"))
        self.assertEqual(self.facade.calls, ["revert:f1"])

    def test_apply_request_carries_the_release_flag(self) -> None:
        edit = RequestEdit(
            method="POST", url="http://api.example.com/v1", headers=[], content=b""
        )
        self.assertTrue(self.controller.apply_request("f1", edit))
        self.assertTrue(self.controller.apply_request("f1", edit, release=True))
        self.assertEqual(
            self.facade.calls, ["request:f1:False:POST", "request:f1:True:POST"]
        )

    def test_apply_response_carries_the_release_flag(self) -> None:
        edit = ResponseEdit(status_code=201, headers=[], content=b"")
        self.assertTrue(self.controller.apply_response("f1", edit))
        self.assertTrue(self.controller.apply_response("f1", edit, release=True))
        self.assertEqual(
            self.facade.calls, ["response:f1:False:201", "response:f1:True:201"]
        )

    def test_fake_response_delegates(self) -> None:
        """请求期直接回给客户端，根本不拨号 —— 走的是另一个门面方法。"""
        edit = ResponseEdit(status_code=404, headers=[], content=b"nope")
        self.assertTrue(self.controller.fake_response("f1", edit))
        self.assertEqual(self.facade.calls, ["fake:f1:404"])

    def test_a_rejected_edit_reports_without_refreshing(self) -> None:
        """内核那边什么都没改（校验全在 `backup()` 之前），队列不必重新要。"""
        self.facade.fail_with = ValueError("Invalid HTTP status code: 0")
        before = self.facade.snapshot_calls
        edit = ResponseEdit(status_code=0, headers=[], content=b"")
        self.assertFalse(self.controller.apply_response("f1", edit))
        self.assertEqual(self.facade.snapshot_calls, before)
        self.assertEqual(self.failures[-1][1], "Invalid HTTP status code: 0")


class RealFacadeTests(ConfigSandbox):
    """同一批操作走真门面再验一遍：假门面只证明"调对了"，证不了"下发对了"。"""

    def setUp(self) -> None:
        super().setUp()
        self.runtime = MitmRuntime()
        self.controller = InterceptController(mitm=MitmFacade(self.runtime))

    def test_starts_empty_and_pushes_to_the_runtime(self) -> None:
        self.assertEqual(self.controller.rules, [])
        self.assertEqual(self.runtime.intercept_rules, [])
        self.assertFalse(self.runtime.intercept_enabled)

    def test_add_rule_persists_and_pushes_down(self) -> None:
        rule = make_rule()
        self.assertTrue(self.controller.add_rule(rule))
        self.assertEqual(self.runtime.intercept_rules, [rule])
        self.assertEqual(CONFIG.get(CONFIG.intercept_rules), [rule.to_dict()])

    def test_the_master_switch_pushes_down(self) -> None:
        self.controller.add_rule(make_rule())
        self.assertTrue(self.controller.set_intercept_enabled(True))
        self.assertTrue(self.runtime.intercept_enabled)

    def test_a_broken_rule_is_rejected_by_the_native_parser(self) -> None:
        """坏正则要在下发那一刻被原生 `parse_filter` 挡掉，规则表保持原样。"""
        self.controller.add_rule(make_rule())
        self.controller.set_intercept_enabled(True)
        failures: list[tuple[str, str]] = []
        self.controller.operation_failed.connect(
            lambda title, detail: failures.append((title, detail))
        )
        self.assertFalse(self.controller.add_rule(BROKEN))
        self.assertEqual(self.runtime.intercept_rules, self.controller.rules)
        self.assertEqual(len(self.controller.rules), 1)
        self.assertIn("Invalid match value", failures[-1][1])

    def test_a_broken_rule_is_not_compiled_while_the_switch_is_off(self) -> None:
        """总开关关掉时下发的是 ``intercept=None``，一条表达式都不编译。

        所以坏规则进得来 —— 但它出不了门：一开开关就会被原生解析器打回来（开关自己
        弹回去），而手改坏的 config 在构造时就被 `_usable` 停用了。对话框那一层也
        不让保存写坏的规则。
        """
        self.assertTrue(self.controller.add_rule(BROKEN))
        self.assertFalse(self.controller.set_intercept_enabled(True))
        self.assertFalse(self.controller.enabled)

    def test_an_empty_queue_when_the_kernel_is_not_running(self) -> None:
        """内核没起来时 `intercepted_flows()` 走本地快照，问得到、且是空的。"""
        self.controller.refresh_flows()
        self.assertEqual(self.controller.flows, [])


if __name__ == "__main__":
    unittest.main()
