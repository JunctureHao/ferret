"""Tests for the breakpoint model, its native wiring and the message write-back."""

import asyncio
import unittest
from dataclasses import replace
from typing import Any

from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import HTTPFlow, Response
from mitmproxy.test import tflow

from ferret.core.mitm import (
    INTERCEPT_LIMIT,
    INTERCEPT_OPTIONS,
    FerretMaster,
    InterceptField,
    InterceptLogic,
    InterceptRule,
    RequestEdit,
    ResponseEdit,
    intercept_expression,
    intercept_option_updates,
    intercept_rules_from_config,
    intercept_rules_to_config,
)
from ferret.core.mitm.intercept import (
    FerretIntercept,
    InterceptState,
    apply_request_edit,
    apply_response_edit,
    fake_response,
)

# 一条直接写死的 flowfilter 表达式，用来把「命中判定」和「规则编译」分开测。
EXAMPLE_FILTER = "~u example.com"


def flow_at(host: str = "example.com", *, resp: bool = False):
    """A flow whose URL really contains ``host``.

    `tflow.tflow()` 的默认主机是 ``address``，直接拿来测命中判定会一条都匹配不上。
    """
    flow = tflow.tflow(resp=resp)
    flow.request.host = host
    return flow


def response_of(flow: HTTPFlow) -> Response:
    """`flow.response` 收窄成非空。写回类用例十行里有九行要用它。"""
    response = flow.response
    if response is None:
        raise AssertionError("这条流量没有响应")
    return response


def rule(**kwargs: Any) -> InterceptRule:
    fields: dict[str, Any] = {"value": "api.example.com", **kwargs}
    return InterceptRule(**fields)


class InterceptRulePatternTests(unittest.TestCase):
    def test_contains_escapes_the_literal_value(self) -> None:
        self.assertEqual(rule().pattern, r"api\.example\.com")

    def test_equals_anchors_the_whole_value(self) -> None:
        built = rule(logic=InterceptLogic.EQUALS, value="http://a.com/x")
        self.assertEqual(built.pattern, r"^http://a\.com/x$")

    def test_regex_is_passed_through_untouched(self) -> None:
        pattern = r"^https://a\.com/(.*)"
        self.assertEqual(
            rule(logic=InterceptLogic.REGEX, value=pattern).pattern, pattern
        )

    def test_blank_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = rule(value="   ").pattern

    def test_host_and_method_ignore_case(self) -> None:
        for field in (InterceptField.HOST, InterceptField.METHOD):
            with self.subTest(field=field):
                self.assertTrue(rule(field=field).pattern.startswith("(?i)"))

    def test_url_stays_case_sensitive(self) -> None:
        """Paths are case-sensitive, and the rewrite page escapes the same way."""
        self.assertFalse(rule(field=InterceptField.URL).pattern.startswith("(?i)"))

    def test_host_equals_does_not_allow_a_port(self) -> None:
        """``~d`` matches ``request.host``, which never carries the port."""
        built = rule(
            field=InterceptField.HOST, logic=InterceptLogic.EQUALS, value="a.com"
        )
        self.assertEqual(built.pattern, r"(?i)^a\.com$")


class InterceptRuleExpressionTests(unittest.TestCase):
    def test_the_expression_carries_no_phase_selector(self) -> None:
        """规则不筛阶段：不带 ~q / ~s 就是原生那两个钩子都拦。"""
        expression = rule().expression
        self.assertEqual(expression, r'(~u "api\\.example\\.com")')
        self.assertNotIn("~q", expression)
        self.assertNotIn("~s", expression)

    def test_every_expression_is_parenthesized(self) -> None:
        for field in InterceptField:
            built = rule(field=field)
            with self.subTest(field=field):
                self.assertTrue(built.expression.startswith("("))
                self.assertTrue(built.expression.endswith(")"))

    def test_every_combination_parses_natively(self) -> None:
        for field in InterceptField:
            for logic in InterceptLogic:
                built = rule(field=field, logic=logic)
                with self.subTest(field=field, logic=logic):
                    built.validate()

    def test_broken_regex_is_rejected_with_our_own_message(self) -> None:
        built = rule(logic=InterceptLogic.REGEX, value="(unclosed")
        with self.assertRaises(ValueError) as ctx:
            built.validate()
        self.assertIn("Invalid match value", str(ctx.exception))


class InterceptExpressionCompilationTests(unittest.TestCase):
    def test_no_rules_compile_to_none(self) -> None:
        self.assertIsNone(intercept_expression([]))

    def test_disabled_and_blank_rules_are_skipped(self) -> None:
        rules = [rule(enabled=False), rule(value="   "), rule(value="keep.me")]
        expression = intercept_expression(rules)
        self.assertEqual(expression, r'(~u "keep\\.me")')

    def test_several_rules_are_or_joined(self) -> None:
        rules = [rule(value="a.com"), rule(value="b.com")]
        expression = intercept_expression(rules)
        assert expression is not None
        self.assertIn(" | ", expression)
        self.assertEqual(expression.count("("), 2 + expression.count("(?i)"))

    def test_or_joined_expression_really_means_or(self) -> None:
        """Without the per-rule parentheses this parses as FAnd and matches nothing."""
        rules = [rule(value="a.com"), rule(value="b.com")]
        expression = intercept_expression(rules)
        assert expression is not None
        matcher = parse_filter(expression)
        assert matcher is not None
        request_only = tflow.tflow(resp=False)
        request_only.request.url = "http://a.com/x"
        answered = tflow.tflow(resp=True)
        answered.request.url = "http://b.com/x"
        self.assertTrue(matcher(request_only))
        self.assertTrue(matcher(answered))

    def test_a_bad_rule_fails_the_whole_batch(self) -> None:
        rules = [rule(value="a.com"), rule(logic=InterceptLogic.REGEX, value="(")]
        with self.assertRaises(ValueError):
            intercept_expression(rules)


class InterceptOptionUpdatesTests(unittest.TestCase):
    def test_only_the_intercept_option_is_written(self) -> None:
        self.assertEqual(INTERCEPT_OPTIONS, ("intercept",))
        self.assertEqual(set(intercept_option_updates([rule()])), {"intercept"})

    def test_the_master_switch_clears_the_expression(self) -> None:
        """``intercept=None`` lets the native configure() clear intercept_active."""
        updates = intercept_option_updates([rule()], enabled=False)
        self.assertIsNone(updates["intercept"])

    def test_removing_every_rule_still_writes_the_option(self) -> None:
        self.assertEqual(intercept_option_updates([]), {"intercept": None})


class InterceptConfigRoundTripTests(unittest.TestCase):
    def test_rules_survive_a_round_trip(self) -> None:
        rules = [
            rule(field=InterceptField.HOST),
            rule(logic=InterceptLogic.REGEX, value="^x", enabled=False),
        ]
        self.assertEqual(
            intercept_rules_from_config(intercept_rules_to_config(rules)), rules
        )

    def test_unparseable_entries_are_dropped(self) -> None:
        raw = [{"field": "nope", "value": "x"}, "not-a-dict", rule().to_dict()]
        self.assertEqual(intercept_rules_from_config(raw), [rule()])

    def test_a_legacy_phase_key_is_ignored(self) -> None:
        """老配置里存过 `phase`（那时规则要选拦请求还是拦响应）。

        `from_dict` 只读认识的键，所以这个键自然作废，规则照常读出来 —— 不需要
        迁移代码，也不该把整条规则判死。
        """
        raw = [{"phase": "request", "value": "api.example.com"}]
        self.assertEqual(intercept_rules_from_config(raw), [rule()])

    def test_non_list_config_yields_no_rules(self) -> None:
        self.assertEqual(intercept_rules_from_config({"a": 1}), [])

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        built = intercept_rules_from_config([{}])
        self.assertEqual(built, [InterceptRule()])


class InterceptStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = InterceptState()
        self.seen: list[str] = []
        self.state.on_intercept_changed = lambda flow: self.seen.append(flow.id)

    def test_arming_holds_the_flow_and_notifies(self) -> None:
        flow = tflow.tflow()
        self.assertTrue(self.state.arm(flow))
        self.assertTrue(flow.intercepted)
        self.assertEqual(self.state.held_count, 1)
        self.assertEqual(self.seen, [flow.id])

    def test_notification_is_needed_because_view_never_fires(self) -> None:
        """No callback wired is fine; the state must not require one."""
        state = InterceptState()
        self.assertTrue(state.arm(tflow.tflow()))

    def test_arming_twice_is_idempotent(self) -> None:
        flow = tflow.tflow()
        self.state.arm(flow)
        self.assertTrue(self.state.arm(flow))
        self.assertEqual(self.state.held_count, 1)
        self.assertEqual(len(self.seen), 1)

    def test_a_flow_someone_else_intercepted_is_declined(self) -> None:
        """The gateway's SUSPEND policies hold flows too — two ledgers would desync."""
        flow = tflow.tflow()
        flow.intercept()
        self.assertFalse(self.state.arm(flow))
        self.assertEqual(self.state.held_count, 0)
        self.assertEqual(self.seen, [])

    def test_the_limit_lets_traffic_through_instead_of_wedging_it(self) -> None:
        held = [tflow.tflow() for _ in range(INTERCEPT_LIMIT)]
        for flow in held:
            self.assertTrue(self.state.arm(flow))
        overflow = tflow.tflow()
        # 别的测试建过的 Master 会往 root logger 挂一个持有已关闭 loop 的 handler，
        # assertLogs 会临时把它换掉（和网关的上限测试同一写法）。
        with self.assertLogs(level="WARNING"):
            self.assertFalse(self.state.arm(overflow))
        self.assertFalse(overflow.intercepted)
        self.assertEqual(self.state.held_count, INTERCEPT_LIMIT)

    def test_release_resumes_and_forgets(self) -> None:
        flow = tflow.tflow()
        self.state.arm(flow)
        self.assertEqual(self.state.release([flow.id]), 1)
        self.assertFalse(flow.intercepted)
        self.assertEqual(self.state.held_count, 0)
        self.assertEqual(self.seen, [flow.id, flow.id])

    def test_releasing_an_unknown_id_is_a_no_op(self) -> None:
        self.assertEqual(self.state.release(["nope"]), 0)

    def test_release_can_kill_the_connection(self) -> None:
        flow = tflow.tflow()
        self.state.arm(flow)
        self.state.release([flow.id], kill=True)
        self.assertFalse(flow.intercepted)
        self.assertIsNotNone(flow.error)

    def test_kill_never_leaves_a_flow_wedged_on_wait_for_resume(self) -> None:
        """``kill()`` clears ``intercepted``, so ``resume()`` has to come first."""
        flow = tflow.tflow()
        self.state.arm(flow)
        flow._resume_event = asyncio.Event()
        flow._resume_event.clear()
        self.state.release([flow.id], kill=True)
        self.assertTrue(flow._resume_event.is_set())

    def test_release_all_empties_the_ledger(self) -> None:
        flows = [tflow.tflow() for _ in range(3)]
        for flow in flows:
            self.state.arm(flow)
        self.assertEqual(self.state.release_all(), 3)
        self.assertEqual(self.state.held_count, 0)
        self.assertTrue(all(not flow.intercepted for flow in flows))

    def test_held_ids_lists_what_is_held(self) -> None:
        flows = [tflow.tflow() for _ in range(2)]
        for flow in flows:
            self.state.arm(flow)
        self.assertEqual(set(self.state.held_ids()), {flow.id for flow in flows})

    def test_forget_frees_the_quota_without_resuming(self) -> None:
        flow = tflow.tflow()
        self.state.arm(flow)
        self.assertEqual(self.state.forget([flow.id]), 1)
        self.assertEqual(self.state.held_count, 0)
        self.assertTrue(flow.intercepted)
        self.assertEqual(self.seen, [flow.id])

    def test_forgetting_an_unknown_id_is_a_no_op(self) -> None:
        self.assertEqual(self.state.forget(["nope"]), 0)


class FerretInterceptTests(unittest.TestCase):
    """The addon must keep the native hit test and only replace the arming step.

    要借一个真的 Master：命中判定读 ``ctx.options.intercept_active``，而这个选项由
    ``Intercept.load`` 注册，裸构造的 addon 身上根本没有。
    """

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)
        self.addon = self.master.intercept
        self.state = self.master.intercept_state

    def arm_filter(self, expression: str | None) -> None:
        self.master.options.update(intercept=expression)

    def test_a_matching_flow_is_armed_through_our_state(self) -> None:
        self.arm_filter(EXAMPLE_FILTER)
        self.addon.process_flow(flow_at())
        self.assertEqual(self.state.held_count, 1)

    def test_a_non_matching_flow_is_left_alone(self) -> None:
        self.arm_filter("~u nowhere.invalid")
        flow = flow_at()
        self.addon.process_flow(flow)
        self.assertFalse(flow.intercepted)
        self.assertEqual(self.state.held_count, 0)

    def test_no_filter_means_no_interception(self) -> None:
        """An empty expression makes the native configure() clear intercept_active."""
        flow = flow_at()
        self.addon.process_flow(flow)
        self.assertFalse(self.master.options.intercept_active)
        self.assertFalse(flow.intercepted)

    def test_clearing_the_expression_stops_intercepting(self) -> None:
        self.arm_filter(EXAMPLE_FILTER)
        self.arm_filter(None)
        flow = flow_at()
        self.addon.process_flow(flow)
        self.assertFalse(flow.intercepted)

    def test_replays_are_never_intercepted(self) -> None:
        """Inherited from the native ``should_intercept``; we must not lose it."""
        self.arm_filter(EXAMPLE_FILTER)
        flow = flow_at()
        flow.is_replay = "request"
        self.addon.process_flow(flow)
        self.assertFalse(flow.intercepted)

    def test_a_compiled_rule_really_intercepts_end_to_end(self) -> None:
        """规则 → 表达式 → 选项 → 原生命中判定，整条链一次走通。"""
        built = InterceptRule(value="example.com")
        self.master.options.update(**intercept_option_updates([built]))
        flow = flow_at()
        self.addon.process_flow(flow)
        self.assertTrue(flow.intercepted)

    def test_the_same_rule_also_arms_answered_flows(self) -> None:
        """一条规则要在请求期和响应期各拦一次，所以有响应的流量也得拦得下来。

        `process_flow` 是两个钩子共用的入口，这里等价于放行之后 `response` 钩子
        再进来一次。
        """
        built = InterceptRule(value="example.com")
        self.master.options.update(**intercept_option_updates([built]))
        flow = flow_at(resp=True)
        self.addon.process_flow(flow)
        self.assertTrue(flow.intercepted)


class InterceptMasterWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_the_master_installs_our_subclass(self) -> None:
        self.assertIsInstance(self.master.intercept, FerretIntercept)
        self.assertIs(self.master.intercept.state, self.master.intercept_state)

    def test_the_native_intercept_addon_is_not_also_loaded(self) -> None:
        addons = self.master.addons.chain
        self.assertEqual(
            [a for a in addons if isinstance(a, FerretIntercept)],
            [self.master.intercept],
        )

    def test_intercept_sits_after_the_gateway_and_before_the_view(self) -> None:
        """Rewrites must already be applied, and a held flow must reach the view."""
        from ferret.core.mitm.addons import GatewayL7Addon
        from ferret.core.mitm.bindings import View

        names = [type(a).__name__ for a in self.master.addons.chain]
        self.assertLess(
            names.index(GatewayL7Addon.__name__), names.index(FerretIntercept.__name__)
        )
        self.assertLess(
            names.index(FerretIntercept.__name__), names.index(View.__name__)
        )

    def test_the_intercept_option_exists_once_addons_are_loaded(self) -> None:
        """``intercept`` is registered by ``Intercept.load``, not by ``Options``."""
        self.assertIn("intercept", self.master.options)
        self.assertIsNone(self.master.options.intercept)


class ApplyRequestEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = tflow.tflow(resp=False)

    def edit(self, **kwargs: Any) -> RequestEdit:
        fields: dict[str, Any] = {
            "method": "post",
            "url": "http://a.com/new?q=1",
            "headers": [("Host", "a.com"), ("X-Trace", "1")],
            "content": b'{"a": 1}',
            **kwargs,
        }
        return RequestEdit(**fields)

    def test_every_field_is_written(self) -> None:
        apply_request_edit(self.flow, self.edit())
        request = self.flow.request
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, "http://a.com/new?q=1")
        self.assertEqual(request.headers["X-Trace"], "1")
        self.assertEqual(request.get_content(strict=False), b'{"a": 1}')

    def test_the_method_is_upper_cased(self) -> None:
        apply_request_edit(self.flow, self.edit(method="  delete "))
        self.assertEqual(self.flow.request.method, "DELETE")

    def test_duplicate_headers_survive(self) -> None:
        headers = [("Cookie", "a=1"), ("Cookie", "b=2")]
        apply_request_edit(self.flow, self.edit(headers=headers))
        self.assertEqual(self.flow.request.headers.get_all("Cookie"), ["a=1", "b=2"])

    def test_blank_header_names_are_dropped(self) -> None:
        apply_request_edit(self.flow, self.edit(headers=[("  ", "x"), ("A", "1")]))
        self.assertEqual(self.flow.request.headers.get_all(""), [])
        self.assertEqual(self.flow.request.headers.get_all("A"), ["1"])

    def test_content_length_is_recomputed_by_the_kernel(self) -> None:
        """We must never hand-write it, so a stale value has to be corrected."""
        headers = [("Content-Length", "999")]
        apply_request_edit(self.flow, self.edit(headers=headers, content=b"abcd"))
        self.assertEqual(self.flow.request.headers["Content-Length"], "4")

    def test_editing_marks_the_flow_revertable(self) -> None:
        apply_request_edit(self.flow, self.edit())
        self.assertTrue(self.flow.modified())
        self.flow.revert()
        self.assertEqual(self.flow.request.method, "GET")

    def test_reverting_goes_back_to_the_original_not_the_previous_edit(self) -> None:
        apply_request_edit(self.flow, self.edit(method="POST"))
        apply_request_edit(self.flow, self.edit(method="PUT"))
        self.flow.revert()
        self.assertEqual(self.flow.request.method, "GET")

    def test_a_blank_method_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_request_edit(self.flow, self.edit(method="   "))

    def test_a_blank_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_request_edit(self.flow, self.edit(url=""))

    def test_an_unparseable_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            apply_request_edit(self.flow, self.edit(url="not a url"))

    def test_a_failed_save_leaves_no_trace(self) -> None:
        """`modified()` means "has a backup", so every check precedes `backup()`."""
        with self.assertRaises(ValueError):
            apply_request_edit(self.flow, self.edit(url="not a url"))
        self.assertFalse(self.flow.modified())
        self.assertEqual(self.flow.request.method, "GET")


class ApplyResponseEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.flow = tflow.tflow(resp=True)

    def edit(self, **kwargs: Any) -> ResponseEdit:
        fields: dict[str, Any] = {
            "status_code": 404,
            "headers": [("Content-Type", "text/plain")],
            "content": b"gone",
            **kwargs,
        }
        return ResponseEdit(**fields)

    def test_every_field_is_written(self) -> None:
        apply_response_edit(self.flow, self.edit())
        response = response_of(self.flow)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Content-Type"], "text/plain")
        self.assertEqual(response.get_content(strict=False), b"gone")

    def test_a_blank_reason_falls_back_to_the_standard_phrase(self) -> None:
        apply_response_edit(self.flow, self.edit())
        self.assertEqual(response_of(self.flow).reason, "Not Found")

    def test_an_explicit_reason_wins(self) -> None:
        apply_response_edit(self.flow, self.edit(reason="Nope"))
        self.assertEqual(response_of(self.flow).reason, "Nope")

    def test_an_unknown_status_code_gets_an_empty_phrase(self) -> None:
        apply_response_edit(self.flow, self.edit(status_code=599))
        self.assertEqual(response_of(self.flow).reason, "")

    def test_duplicate_headers_survive(self) -> None:
        headers = [("Set-Cookie", "a=1"), ("Set-Cookie", "b=2")]
        apply_response_edit(self.flow, self.edit(headers=headers))
        kept = response_of(self.flow).headers.get_all("Set-Cookie")
        self.assertEqual(kept, ["a=1", "b=2"])

    def test_out_of_range_status_codes_are_rejected(self) -> None:
        for status_code in (0, 99, 600, -1):
            with self.subTest(status_code=status_code):
                flow = tflow.tflow(resp=True)
                with self.assertRaises(ValueError):
                    apply_response_edit(flow, self.edit(status_code=status_code))
                self.assertFalse(flow.modified())

    def test_a_flow_without_a_response_is_rejected(self) -> None:
        flow = tflow.tflow(resp=False)
        with self.assertRaises(ValueError):
            apply_response_edit(flow, self.edit())
        self.assertFalse(flow.modified())

    def test_editing_is_revertable(self) -> None:
        original = response_of(self.flow).status_code
        apply_response_edit(self.flow, self.edit())
        self.flow.revert()
        self.assertEqual(response_of(self.flow).status_code, original)


class FakeResponseTests(unittest.TestCase):
    """Answering a request-phase breakpoint locally, without contacting the server."""

    def setUp(self) -> None:
        self.flow = tflow.tflow(resp=False)
        self.draft = ResponseEdit(
            status_code=201,
            headers=[("Content-Type", "application/json")],
            content=b'{"ok": true}',
        )

    def test_a_response_is_attached(self) -> None:
        fake_response(self.flow, self.draft)
        response = response_of(self.flow)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_content(strict=False), b'{"ok": true}')
        self.assertEqual(response.headers["Content-Type"], "application/json")

    def test_the_standard_reason_is_filled_in(self) -> None:
        fake_response(self.flow, self.draft)
        self.assertEqual(response_of(self.flow).reason, "Created")

    def test_an_explicit_reason_wins(self) -> None:
        fake_response(self.flow, replace(self.draft, reason="Made It"))
        self.assertEqual(response_of(self.flow).reason, "Made It")

    def test_content_length_is_set_by_the_kernel(self) -> None:
        fake_response(self.flow, self.draft)
        self.assertEqual(response_of(self.flow).headers["Content-Length"], "12")

    def test_faking_is_revertable(self) -> None:
        fake_response(self.flow, self.draft)
        self.assertTrue(self.flow.modified())
        self.flow.revert()
        self.assertIsNone(self.flow.response)

    def test_a_flow_that_already_answered_is_rejected(self) -> None:
        flow = tflow.tflow(resp=True)
        with self.assertRaises(ValueError):
            fake_response(flow, self.draft)
        self.assertFalse(flow.modified())

    def test_an_invalid_status_code_leaves_no_trace(self) -> None:
        with self.assertRaises(ValueError):
            fake_response(self.flow, replace(self.draft, status_code=0))
        self.assertIsNone(self.flow.response)
        self.assertFalse(self.flow.modified())


if __name__ == "__main__":
    unittest.main()
