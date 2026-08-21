"""Tests for the gateway wiring in MitmRuntime and MitmFacade."""

import os
import socket
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Signal

from ferret.core.mitm import MitmFacade, MitmRuntime, MitmRuntimeState, View
from ferret.core.mitm.addons import GatewayState
from ferret.core.mitm.gateway import (
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
    GatewayRuleSet,
)

HOST = "example.com"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def l4(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L4, policy=policy, value=value, **kwargs)


def l7(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L7, policy=policy, value=value, **kwargs)


def broken() -> GatewayRule:
    return l7(GatewayPolicy.BLOCK_OUT, "(", logic=GatewayLogic.REGEX)


class FakeMaster:
    def __init__(self) -> None:
        self.gateway = GatewayState()
        self.options = MagicMock()


class FakeRuntime(QObject):
    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    flow_suspended = Signal(object)
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.view = View()
        self.master = FakeMaster()
        self.state = MitmRuntimeState.RUNNING
        self.is_running = True
        self.listen_host = "127.0.0.1"
        self.listen_port = 8080
        self.gateway_rules: list[GatewayRule] = []
        self.gateway_enabled = True
        self.applied: list[tuple] = []

    def call(self, callback, *, timeout=5.0):
        return callback()

    def apply_gateway_rules(self, rules=None, *, enabled=None) -> None:
        self.applied.append((rules, enabled))
        if rules is not None:
            self.gateway_rules = list(rules)
        if enabled is not None:
            self.gateway_enabled = enabled


class MitmRuntimeGatewayTests(unittest.TestCase):
    """Everything `apply_gateway_rules` guarantees without a kernel running."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = MitmRuntime(listen_port=free_port())

    def test_defaults_are_empty_rules_and_a_live_switch(self) -> None:
        self.assertEqual(self.runtime.gateway_rules, [])
        self.assertTrue(self.runtime.gateway_enabled)

    def test_rules_are_stored_as_a_copy(self) -> None:
        rules = [l7(GatewayPolicy.BLOCK_OUT)]
        self.runtime.apply_gateway_rules(rules)
        rules.append(l7(GatewayPolicy.BLOCK_IN))
        self.assertEqual(len(self.runtime.gateway_rules), 1)

    def test_flipping_the_switch_alone_keeps_the_rules(self) -> None:
        self.runtime.apply_gateway_rules([l7(GatewayPolicy.BLOCK_OUT)])
        self.runtime.apply_gateway_rules(enabled=False)
        self.assertEqual(len(self.runtime.gateway_rules), 1)
        self.assertFalse(self.runtime.gateway_enabled)

    def test_a_broken_rule_rolls_back_rules_and_switch_together(self) -> None:
        """两个平面对同一条流量给出不同判定，是这套设计最不能出的错。"""
        self.runtime.apply_gateway_rules([l7(GatewayPolicy.BLOCK_OUT)])
        before = list(self.runtime.gateway_rules)

        with self.assertRaises(ValueError):
            self.runtime.apply_gateway_rules([broken()], enabled=False)

        self.assertEqual(self.runtime.gateway_rules, before)
        self.assertTrue(self.runtime.gateway_enabled)

    def test_payload_carries_both_planes_and_commits_nothing(self) -> None:
        rule = l4(GatewayPolicy.BYPASS)
        self.runtime.apply_gateway_rules([rule, l7(GatewayPolicy.BLOCK_OUT)])
        ruleset, updates, enabled = self.runtime._gateway_payload()
        self.assertIsInstance(ruleset, GatewayRuleSet)
        self.assertTrue(ruleset)
        self.assertEqual(updates, {"allow_hosts": [], "ignore_hosts": [rule.pattern]})
        self.assertTrue(enabled)

    def test_payload_honours_the_master_switch(self) -> None:
        self.runtime.apply_gateway_rules([l4(GatewayPolicy.BYPASS)], enabled=False)
        _, updates, enabled = self.runtime._gateway_payload()
        self.assertEqual(updates, {"allow_hosts": [], "ignore_hosts": []})
        self.assertFalse(enabled)

    def test_disabled_broken_rules_still_produce_a_payload(self) -> None:
        """损坏规则被停用后必须还能开机 —— 控制器就是这么保住用户数据的。"""
        from dataclasses import replace

        self.runtime.apply_gateway_rules([replace(broken(), enabled=False)])
        ruleset, updates, _ = self.runtime._gateway_payload()
        self.assertFalse(ruleset)
        self.assertEqual(updates, {"allow_hosts": [], "ignore_hosts": []})

    def test_release_suspended_is_a_no_op_while_stopped(self) -> None:
        self.assertEqual(self.runtime.release_suspended(), 0)

    def test_suspend_callback_is_republished_as_a_qt_signal(self) -> None:
        seen: list = []
        self.runtime.flow_suspended.connect(seen.append)
        flow = tflow.tflow()
        self.runtime._on_flow_suspended(flow)
        self.assertEqual(seen, [flow])


class MitmFacadeGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = FakeRuntime()
        self.facade = MitmFacade(self.runtime)  # type: ignore
        self.state = self.runtime.master.gateway

    def suspended_flow(self):
        flow = tflow.tflow(resp=True)
        self.runtime.view.add([flow])
        self.state.suspend(flow, GatewayPolicy.SUSPEND_IN)
        self.assertTrue(flow.intercepted)
        return flow

    def test_rules_property_returns_a_copy(self) -> None:
        self.runtime.gateway_rules = [l7(GatewayPolicy.BLOCK_OUT)]
        snapshot = self.facade.gateway_rules
        snapshot.clear()
        self.assertEqual(len(self.runtime.gateway_rules), 1)

    def test_switch_property_reads_through(self) -> None:
        self.assertTrue(self.facade.gateway_enabled)
        self.runtime.gateway_enabled = False
        self.assertFalse(self.facade.gateway_enabled)

    def test_set_rules_delegates_without_touching_the_switch(self) -> None:
        rules = [l7(GatewayPolicy.BLOCK_OUT)]
        self.facade.set_gateway_rules(rules)
        self.assertEqual(self.runtime.applied, [(rules, None)])

    def test_set_enabled_delegates_without_touching_the_rules(self) -> None:
        self.facade.set_gateway_enabled(False)
        self.assertEqual(self.runtime.applied, [(None, False)])

    def test_clear_flows_releases_before_the_view_is_emptied(self) -> None:
        flow = self.suspended_flow()
        seen: list[bool] = []
        original = self.runtime.view.clear
        self.runtime.view.clear = lambda: (
            seen.append(flow.intercepted),
            original(),
        )

        self.facade.clear_flows()

        self.assertEqual(seen, [False])
        self.assertEqual(self.state.suspended_count, 0)
        self.assertEqual(len(self.runtime.view), 0)

    def test_remove_flows_releases_before_the_view_kills_them(self) -> None:
        """`View.remove` 会 kill 可 kill 的 flow，而 kill 之后 resume 唤不醒它。"""
        flow = self.suspended_flow()
        seen: list[bool] = []
        original = self.runtime.view.remove
        self.runtime.view.remove = lambda flows: (
            seen.append(flow.intercepted),
            original(flows),
        )

        self.facade.remove_flows([flow])

        self.assertEqual(seen, [False])
        self.assertEqual(self.state.suspended_count, 0)
        self.assertEqual(len(self.runtime.view), 0)

    def test_remove_flows_only_releases_the_named_flows(self) -> None:
        kept = self.suspended_flow()
        removed = self.suspended_flow()
        self.facade.remove_flows([removed])
        self.assertEqual(self.state.suspended_count, 1)
        self.assertTrue(kept.intercepted)

    def test_clear_flows_works_when_no_master_exists(self) -> None:
        self.runtime.master = None  # type: ignore
        self.runtime.view.add([tflow.tflow(resp=True)])
        self.facade.clear_flows()
        self.assertEqual(len(self.runtime.view), 0)


class MitmRuntimeGatewayLiveTests(unittest.TestCase):
    """The gateway must be armed before the proxy serves its first byte."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait_for_signal(self, signal, timeout_ms: int = 5000):
        loop = QEventLoop()
        values = []

        def receive(*args):
            values.append(args)
            loop.quit()

        signal.connect(receive)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        signal.disconnect(receive)
        return values

    def test_rules_seeded_before_start_are_live_when_the_kernel_is_ready(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        rule = l4(GatewayPolicy.BYPASS)
        runtime.apply_gateway_rules([rule, l7(GatewayPolicy.BLOCK_OUT)])
        runtime.start()

        self.assertTrue(self.wait_for_signal(runtime.ready))
        master = runtime.master
        assert master is not None
        self.assertEqual(master.options.ignore_hosts, [rule.pattern])
        self.assertIsNotNone(
            runtime.call(lambda: master.gateway.decide(HOST, 443, "GET"))
        )
        self.assertIsNotNone(master.gateway.on_suspend_changed)

    def test_switching_the_gateway_off_reaches_the_running_kernel(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.apply_gateway_rules([l7(GatewayPolicy.BLOCK_OUT)])
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))
        master = runtime.master
        assert master is not None

        runtime.apply_gateway_rules(enabled=False)

        self.assertIsNone(runtime.call(lambda: master.gateway.decide(HOST, 443, "GET")))
        self.assertTrue(runtime.stop())


if __name__ == "__main__":
    unittest.main()
