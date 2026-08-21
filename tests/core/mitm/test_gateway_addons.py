"""Tests for the gateway addons: state, the L4 plane and the L7 plane."""

import asyncio
import unittest

from mitmproxy import connection
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.exceptions import AddonHalt
from mitmproxy.proxy.server_hooks import ServerConnectionHookData
from mitmproxy.test import tflow

from ferret.core.mitm import FerretMaster
from ferret.core.mitm.addons import (
    SUSPEND_LIMIT,
    GatewayL4Addon,
    GatewayL7Addon,
    GatewayState,
)
from ferret.core.mitm.gateway import (
    GATEWAY_METADATA_KEY,
    GATEWAY_STATUS_CLOSE,
    GatewayLayer,
    GatewayPolicy,
    GatewayRule,
    GatewayRuleSet,
)

HOST = "example.com"


def l4(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L4, policy=policy, value=value, **kwargs)


def l7(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L7, policy=policy, value=value, **kwargs)


def ruleset(*rules: GatewayRule) -> GatewayRuleSet:
    return GatewayRuleSet(rules)


def http_flow(resp: bool = False, host: str = HOST, port: int = 443):
    flow = tflow.tflow(resp=resp)
    flow.request.host = host
    flow.request.port = port
    return flow


def server_data(address=(HOST, 443)) -> ServerConnectionHookData:
    return ServerConnectionHookData(
        server=connection.Server(address=address),
        client=tflow.tclient_conn(),
    )


class GatewayStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GatewayState()
        self.seen: list[str] = []
        self.state.on_suspend_changed = lambda flow: self.seen.append(flow.id)

    def test_a_fresh_state_decides_nothing(self) -> None:
        self.assertIsNone(self.state.decide(HOST, 443, "GET"))
        self.assertEqual(self.state.suspended_count, 0)

    def test_master_switch_off_short_circuits_every_rule(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)), enabled=False)
        self.assertIsNone(self.state.decide(HOST, 443, "GET"))
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)), enabled=True)
        self.assertIsNotNone(self.state.decide(HOST, 443, "GET"))

    def test_suspend_intercepts_marks_and_notifies(self) -> None:
        flow = http_flow()
        self.assertTrue(self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT))
        self.assertTrue(flow.intercepted)
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "suspend_out")
        self.assertEqual(self.state.suspended_count, 1)
        self.assertEqual(self.seen, [flow.id])

    def test_suspend_is_idempotent_per_flow(self) -> None:
        flow = http_flow()
        self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.assertTrue(self.state.suspend(flow, GatewayPolicy.SUSPEND_IN))
        self.assertEqual(self.state.suspended_count, 1)
        # 第二次不再通知，也不覆盖第一次记下的策略。
        self.assertEqual(self.seen, [flow.id])
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "suspend_out")

    def test_suspend_stops_at_the_limit_without_intercepting(self) -> None:
        """`handle_hook` 整个挂起期间都关着看门狗，`tcp_timeout` 不会兜底。"""
        for _ in range(SUSPEND_LIMIT):
            self.state.suspend(http_flow(), GatewayPolicy.SUSPEND_OUT)
        self.assertEqual(self.state.suspended_count, SUSPEND_LIMIT)

        overflow = http_flow()
        with self.assertLogs(level="WARNING"):
            self.assertFalse(self.state.suspend(overflow, GatewayPolicy.SUSPEND_OUT))
        self.assertFalse(overflow.intercepted)
        self.assertNotIn(GATEWAY_METADATA_KEY, overflow.metadata)
        self.assertEqual(self.state.suspended_count, SUSPEND_LIMIT)

    def test_release_returns_the_count_and_clears_the_mark(self) -> None:
        flow = http_flow()
        self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.assertEqual(self.state.release([flow.id]), 1)
        self.assertFalse(flow.intercepted)
        self.assertNotIn(GATEWAY_METADATA_KEY, flow.metadata)
        self.assertEqual(self.state.suspended_count, 0)
        self.assertEqual(self.seen, [flow.id, flow.id])

    def test_release_ignores_unknown_ids(self) -> None:
        self.assertEqual(self.state.release(["nope"]), 0)

    def test_release_all_empties_the_table(self) -> None:
        flows = [http_flow() for _ in range(3)]
        for flow in flows:
            self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.assertEqual(self.state.release_all(), 3)
        self.assertEqual(self.state.suspended_count, 0)
        self.assertFalse(any(flow.intercepted for flow in flows))
        self.assertEqual(self.state.release_all(), 0)

    def test_swapping_rules_releases_everything(self) -> None:
        """规则一变旧判定就不算数，而挂起是永久的 —— 不在这里放就再也没人放了。"""
        flow = http_flow()
        self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)))
        self.assertEqual(self.state.suspended_count, 0)
        self.assertFalse(flow.intercepted)

    def test_release_resumes_before_it_kills(self) -> None:
        """`kill()` 清掉 ``intercepted``，之后 ``resume()`` 开头就 return —— 顺序反了
        这条连接会永久挂死在 ``wait_for_resume()`` 上。"""
        calls: list[str] = []

        class Recorder:
            killable = True

            def __init__(self) -> None:
                self.id = "recorder"
                self.metadata: dict = {}

            def resume(self) -> None:
                calls.append("resume")

            def kill(self) -> None:
                calls.append("kill")

        self.state._suspended["recorder"] = Recorder()  # type: ignore
        self.state.release(["recorder"], kill=True)
        self.assertEqual(calls, ["resume", "kill"])

    def test_release_with_kill_ends_up_resumed_and_killed(self) -> None:
        flow = http_flow()
        self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.assertEqual(self.state.release_all(kill=True), 1)
        self.assertFalse(flow.intercepted)
        self.assertFalse(flow.killable)
        self.assertIsNotNone(flow.error)

    def test_release_skips_kill_when_the_flow_is_not_killable(self) -> None:
        flow = http_flow()
        flow.live = False
        self.state.suspend(flow, GatewayPolicy.SUSPEND_OUT)
        self.assertFalse(flow.killable)
        self.state.release_all(kill=True)
        self.assertIsNone(flow.error)

    def test_missing_callback_is_harmless(self) -> None:
        self.state.on_suspend_changed = None
        flow = http_flow()
        self.assertTrue(self.state.suspend(flow, GatewayPolicy.SUSPEND_IN))
        self.assertEqual(self.state.release_all(), 1)


class GatewayL4AddonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = GatewayState()
        self.addon = GatewayL4Addon(self.state)

    def test_block_rule_marks_the_server_connection(self) -> None:
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK)))
        data = server_data()
        self.addon.server_connect(data)
        self.assertEqual(data.server.error, "blocked by gateway")

    def test_non_matching_block_rule_leaves_the_connection_alone(self) -> None:
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK, "other.test")))
        data = server_data()
        self.addon.server_connect(data)
        self.assertIsNone(data.server.error)

    def test_no_rules_leaves_the_connection_alone(self) -> None:
        data = server_data()
        self.addon.server_connect(data)
        self.assertIsNone(data.server.error)

    def test_missing_address_is_skipped(self) -> None:
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK)))
        data = server_data(address=None)
        self.addon.server_connect(data)
        self.assertIsNone(data.server.error)

    def test_existing_error_is_not_overwritten(self) -> None:
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK)))
        data = server_data()
        data.server.error = "someone else said no"
        self.addon.server_connect(data)
        self.assertEqual(data.server.error, "someone else said no")

    def test_bypass_and_allow_only_are_left_to_the_native_options(self) -> None:
        for policy in (GatewayPolicy.BYPASS, GatewayPolicy.ALLOW_ONLY):
            with self.subTest(policy=policy):
                self.state.set_rules(ruleset(l4(policy)))
                data = server_data()
                self.addon.server_connect(data)
                self.assertIsNone(data.server.error)

    def test_flow_level_rules_do_not_act_on_connections(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)))
        data = server_data()
        self.addon.server_connect(data)
        self.assertIsNone(data.server.error)

    def test_port_is_part_of_the_subject(self) -> None:
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK, "example.com:8080")))
        blocked = server_data(address=(HOST, 8080))
        allowed = server_data(address=(HOST, 443))
        self.addon.server_connect(blocked)
        self.addon.server_connect(allowed)
        self.assertEqual(blocked.server.error, "blocked by gateway")
        self.assertIsNone(allowed.server.error)


class GatewayL7AddonTests(unittest.TestCase):
    HOOKS = ("requestheaders", "request", "response", "error")

    def setUp(self) -> None:
        self.state = GatewayState()
        self.addon = GatewayL7Addon(self.state)

    def test_bypass_halts_every_hook(self) -> None:
        """四个钩子是四次独立派发，漏一个 View / Save / LogAddon 就会看到这条流量。"""
        self.state.set_rules(ruleset(l7(GatewayPolicy.BYPASS)))
        for hook in self.HOOKS:
            with self.subTest(hook=hook), self.assertRaises(AddonHalt):
                getattr(self.addon, hook)(http_flow(resp=True))

    def test_non_matching_bypass_does_not_halt(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BYPASS, "other.test")))
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                getattr(self.addon, hook)(http_flow(resp=True))

    def test_allow_only_halts_on_a_miss_and_passes_on_a_hit(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.ALLOW_ONLY, "allowed.test")))
        with self.assertRaises(AddonHalt):
            self.addon.request(http_flow())
        self.addon.request(http_flow(host="allowed.test"))

    def test_block_out_answers_with_the_configured_status(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT, status_code=451)))
        flow = http_flow()
        self.addon.request(flow)
        assert flow.response is not None
        self.assertEqual(flow.response.status_code, 451)
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "block_out")
        self.assertIsNone(flow.error)

    def test_block_out_with_the_close_status_kills_instead(self) -> None:
        self.state.set_rules(
            ruleset(l7(GatewayPolicy.BLOCK_OUT, status_code=GATEWAY_STATUS_CLOSE))
        )
        flow = http_flow()
        self.addon.request(flow)
        self.assertIsNone(flow.response)
        self.assertIsNotNone(flow.error)
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "block_out")

    def test_block_out_does_nothing_on_the_response_hook(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)))
        flow = http_flow(resp=True)
        original = flow.response
        self.addon.response(flow)
        self.assertIs(flow.response, original)
        self.assertIsNone(flow.error)

    def test_block_in_kills_only_on_the_response_hook(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_IN)))
        on_request = http_flow()
        self.addon.request(on_request)
        self.assertIsNone(on_request.error)

        flow = http_flow(resp=True)
        self.addon.response(flow)
        self.assertIsNotNone(flow.error)
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "block_in")

    def test_transport_block_is_enforced_again_at_the_flow_level(self) -> None:
        """明文 HTTP 走不到 next_layer，连接复用也不会重放 server_connect。"""
        self.state.set_rules(ruleset(l4(GatewayPolicy.BLOCK)))
        flow = http_flow()
        self.addon.request(flow)
        self.assertIsNotNone(flow.error)
        self.assertEqual(flow.metadata[GATEWAY_METADATA_KEY], "block")

    def test_suspend_out_only_fires_on_the_request_hook(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.SUSPEND_OUT)))
        on_response = http_flow(resp=True)
        self.addon.response(on_response)
        self.assertEqual(self.state.suspended_count, 0)

        flow = http_flow()
        self.addon.request(flow)
        self.assertTrue(flow.intercepted)
        self.assertEqual(self.state.suspended_count, 1)

    def test_suspend_in_only_fires_on_the_response_hook(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.SUSPEND_IN)))
        on_request = http_flow()
        self.addon.request(on_request)
        self.assertEqual(self.state.suspended_count, 0)

        flow = http_flow(resp=True)
        self.addon.response(flow)
        self.assertTrue(flow.intercepted)
        self.assertEqual(self.state.suspended_count, 1)

    def test_request_hook_skips_flows_that_are_already_settled(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT, status_code=451)))
        answered = http_flow(resp=True)
        original = answered.response
        self.addon.request(answered)
        self.assertIs(answered.response, original)

        dead = http_flow()
        dead.live = False
        self.addon.request(dead)
        self.assertIsNone(dead.response)

    def test_response_hook_skips_flows_that_are_no_longer_live(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_IN)))
        dead = http_flow(resp=True)
        dead.live = False
        self.addon.response(dead)
        self.assertIsNone(dead.error)
        self.assertEqual(self.state.suspended_count, 0)

    def test_error_hook_only_gates(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_IN)))
        flow = http_flow()
        self.addon.error(flow)
        self.assertIsNone(flow.error)
        self.assertNotIn(GATEWAY_METADATA_KEY, flow.metadata)

    def test_requestheaders_hook_only_gates(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.BLOCK_OUT)))
        flow = http_flow()
        self.addon.requestheaders(flow)
        self.assertIsNone(flow.response)
        self.assertIsNone(flow.error)

    def test_no_rules_leaves_every_hook_a_no_op(self) -> None:
        flow = http_flow(resp=True)
        for hook in self.HOOKS:
            with self.subTest(hook=hook):
                getattr(self.addon, hook)(flow)
        self.assertIsNone(flow.error)
        self.assertEqual(flow.metadata, {})

    def test_done_releases_and_kills_everything_still_suspended(self) -> None:
        self.state.set_rules(ruleset(l7(GatewayPolicy.SUSPEND_OUT)))
        flow = http_flow()
        self.addon.request(flow)
        self.addon.done()
        self.assertEqual(self.state.suspended_count, 0)
        self.assertFalse(flow.intercepted)
        self.assertIsNotNone(flow.error)


class FerretMasterGatewayWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)
        self.names = [type(addon).__name__ for addon in self.master.addons.chain]

    def test_master_owns_one_gateway_state(self) -> None:
        self.assertIsInstance(self.master.gateway, GatewayState)

    def test_both_addons_are_on_the_chain_against_that_state(self) -> None:
        addons = {
            type(addon).__name__: addon
            for addon in self.master.addons.chain
            if isinstance(addon, GatewayL4Addon | GatewayL7Addon)
        }
        self.assertEqual(
            sorted(addons), [GatewayL4Addon.__name__, GatewayL7Addon.__name__]
        )
        for addon in addons.values():
            self.assertIs(addon._state, self.master.gateway)

    def test_l4_addon_runs_immediately_before_next_layer(self) -> None:
        """server_connect 要在原生决定「这条连接怎么处理」之前拿到发言权。"""
        self.assertEqual(
            self.names.index(GatewayL4Addon.__name__) + 1,
            self.names.index(NextLayer.__name__),
        )

    def test_l7_addon_runs_immediately_before_the_view(self) -> None:
        """AddonHalt 只截断当前派发，所以要挨着 View 才能让它一条都收不到。"""
        self.assertEqual(
            self.names.index(GatewayL7Addon.__name__) + 1,
            self.names.index(type(self.master.view).__name__),
        )

    def test_l7_addon_runs_after_the_rewrite_plane(self) -> None:
        """判定看到的是重写之后的目标主机。"""
        self.assertLess(
            self.names.index("MapRemote"), self.names.index(GatewayL7Addon.__name__)
        )

    def test_gateway_addons_precede_the_recorders(self) -> None:
        for name in ("ReadFile", "Save", "LogAddon"):
            with self.subTest(addon=name):
                self.assertLess(
                    self.names.index(GatewayL7Addon.__name__), self.names.index(name)
                )


if __name__ == "__main__":
    unittest.main()
