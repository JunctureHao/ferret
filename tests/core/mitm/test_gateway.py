"""Tests for the gateway rule model, its two planes and the legacy migration."""

import asyncio
import re
import unittest
from dataclasses import replace

from mitmproxy.addons.blocklist import BlockList
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.exceptions import OptionsError

from ferret.core.mitm import FerretMaster
from ferret.core.mitm.gateway import (
    GATEWAY_OPTIONS,
    GATEWAY_STATUS_DEFAULT,
    LAYER_POLICIES,
    GatewayDecision,
    GatewayField,
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
    GatewayRuleSet,
    gateway_option_updates,
    gateway_rules_from_block_config,
    gateway_rules_from_config,
    gateway_rules_to_config,
)

HOST = "example.com"


def l4(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L4, policy=policy, value=value, **kwargs)


def l7(policy: GatewayPolicy, value: str = HOST, **kwargs) -> GatewayRule:
    return GatewayRule(layer=GatewayLayer.L7, policy=policy, value=value, **kwargs)


class GatewayRulePatternTests(unittest.TestCase):
    def test_contains_escapes_the_literal_value(self) -> None:
        self.assertEqual(
            l7(GatewayPolicy.BYPASS, "api.example.com").pattern, r"api\.example\.com"
        )

    def test_regex_passes_through_untouched(self) -> None:
        source = r"^(a|b)\.test$"
        rule = l7(GatewayPolicy.BYPASS, source, logic=GatewayLogic.REGEX)
        self.assertEqual(rule.pattern, source)

    def test_equals_on_host_tolerates_an_optional_port(self) -> None:
        r"""连接级看到的主机全带端口，``^example\.com$`` 一条都匹配不上。"""
        rule = l7(GatewayPolicy.BYPASS, HOST, logic=GatewayLogic.EQUALS)
        self.assertEqual(rule.pattern, r"^example\.com(?::\d+)?$")
        matcher = rule.compile()
        self.assertTrue(matcher.search("example.com:443"))
        self.assertTrue(matcher.search("example.com"))
        self.assertIsNone(matcher.search("api.example.com:443"))
        self.assertIsNone(matcher.search("example.com.evil.net:443"))

    def test_equals_on_method_has_no_port_suffix(self) -> None:
        rule = l7(
            GatewayPolicy.BYPASS,
            "POST",
            field=GatewayField.METHOD,
            logic=GatewayLogic.EQUALS,
        )
        self.assertEqual(rule.pattern, "^POST$")

    def test_blank_value_is_rejected(self) -> None:
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaises(ValueError) as ctx:
                _ = l7(GatewayPolicy.BYPASS, value).pattern
            self.assertIn("匹配值不能为空", str(ctx.exception))

    def test_value_is_stripped_before_compiling(self) -> None:
        self.assertEqual(l7(GatewayPolicy.BYPASS, "  a.com  ").pattern, r"a\.com")

    def test_compile_is_case_insensitive_like_next_layer(self) -> None:
        matcher = l7(GatewayPolicy.BYPASS, HOST).compile()
        self.assertTrue(matcher.flags & re.IGNORECASE)
        self.assertTrue(matcher.search("EXAMPLE.COM:443"))

    def test_broken_regex_raises_value_error(self) -> None:
        rule = l7(GatewayPolicy.BYPASS, "(unclosed", logic=GatewayLogic.REGEX)
        with self.assertRaises(ValueError) as ctx:
            rule.compile()
        self.assertIn("正则", str(ctx.exception))


class GatewayRuleValidateTests(unittest.TestCase):
    def test_every_documented_layer_policy_pair_is_accepted(self) -> None:
        for layer, policies in LAYER_POLICIES.items():
            for policy in policies:
                with self.subTest(layer=layer, policy=policy):
                    GatewayRule(layer=layer, policy=policy, value=HOST).validate()

    def test_policy_outside_its_layer_is_rejected(self) -> None:
        pairs = (
            (GatewayLayer.L4, GatewayPolicy.BLOCK_OUT),
            (GatewayLayer.L4, GatewayPolicy.SUSPEND_IN),
            (GatewayLayer.L7, GatewayPolicy.BLOCK),
        )
        for layer, policy in pairs:
            with self.subTest(layer=layer, policy=policy):
                rule = GatewayRule(layer=layer, policy=policy, value=HOST)
                with self.assertRaises(ValueError) as ctx:
                    rule.validate()
                self.assertIn("不支持策略", str(ctx.exception))

    def test_transport_layer_cannot_match_on_method(self) -> None:
        rule = l4(GatewayPolicy.BLOCK, "POST", field=GatewayField.METHOD)
        with self.assertRaises(ValueError) as ctx:
            rule.validate()
        self.assertIn("只能按主机匹配", str(ctx.exception))

    def test_block_out_status_code_bounds(self) -> None:
        for code in (99, 600, 0, -1):
            with self.subTest(code=code), self.assertRaises(ValueError):
                l7(GatewayPolicy.BLOCK_OUT, status_code=code).validate()
        for code in (100, 403, 444, 599):
            with self.subTest(code=code):
                l7(GatewayPolicy.BLOCK_OUT, status_code=code).validate()

    def test_status_code_is_ignored_for_policies_that_never_respond(self) -> None:
        for policy in (GatewayPolicy.BYPASS, GatewayPolicy.BLOCK_IN):
            with self.subTest(policy=policy):
                l7(policy, status_code=9999).validate()

    def test_validate_also_compiles_the_pattern(self) -> None:
        rule = l7(GatewayPolicy.BYPASS, "(", logic=GatewayLogic.REGEX)
        with self.assertRaises(ValueError):
            rule.validate()


class GatewayRuleSetDecisionTests(unittest.TestCase):
    def test_empty_ruleset_is_falsy_and_decides_nothing(self) -> None:
        rules = GatewayRuleSet()
        self.assertFalse(rules)
        self.assertIsNone(rules.decide(HOST, 443, "GET"))

    def test_disabled_and_blank_rules_are_skipped(self) -> None:
        rules = GatewayRuleSet(
            [
                l7(GatewayPolicy.BLOCK_OUT, enabled=False),
                l7(GatewayPolicy.BLOCK_OUT, "   "),
            ]
        )
        self.assertFalse(rules)
        self.assertIsNone(rules.decide(HOST, 443, "GET"))

    def test_enabled_broken_rule_raises_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            GatewayRuleSet([l7(GatewayPolicy.BYPASS, "(", logic=GatewayLogic.REGEX)])

    def test_disabled_broken_rule_does_not_raise(self) -> None:
        """控制器「保留但停用」损坏规则就靠这条：坏规则留在配置里也能开机。"""
        rules = GatewayRuleSet(
            [l7(GatewayPolicy.BYPASS, "(", logic=GatewayLogic.REGEX, enabled=False)]
        )
        self.assertFalse(rules)

    def test_host_is_matched_with_its_port_appended(self) -> None:
        rules = GatewayRuleSet([l7(GatewayPolicy.BLOCK_OUT, "example.com:8080")])
        self.assertIsNotNone(rules.decide(HOST, 8080, "GET"))
        self.assertIsNone(rules.decide(HOST, 443, "GET"))

    def test_method_rules_hit_and_miss(self) -> None:
        rules = GatewayRuleSet(
            [
                l7(
                    GatewayPolicy.BLOCK_OUT,
                    "POST",
                    field=GatewayField.METHOD,
                    logic=GatewayLogic.EQUALS,
                )
            ]
        )
        self.assertIsNotNone(rules.decide(HOST, 443, "POST"))
        self.assertIsNone(rules.decide(HOST, 443, "GET"))

    def test_method_rules_are_skipped_entirely_without_a_method(self) -> None:
        rules = GatewayRuleSet(
            [l7(GatewayPolicy.BLOCK_OUT, "POST", field=GatewayField.METHOD)]
        )
        self.assertIsNone(rules.decide(HOST, 443))

    def test_allow_only_by_method_does_not_bypass_every_connection(self) -> None:
        """否则按方法的仅允许会把所有连接判成绕行，而方法只有抓下来才知道。"""
        rules = GatewayRuleSet(
            [l7(GatewayPolicy.ALLOW_ONLY, "POST", field=GatewayField.METHOD)]
        )
        self.assertIsNone(rules.decide(HOST, 443))
        self.assertEqual(
            rules.decide(HOST, 443, "GET"), GatewayDecision(GatewayPolicy.BYPASS)
        )

    def test_priority_order_is_allow_then_bypass_then_block_then_suspend(self) -> None:
        ordered = (
            (GatewayPolicy.SUSPEND_OUT, GatewayPolicy.BLOCK_OUT),
            (GatewayPolicy.SUSPEND_IN, GatewayPolicy.BYPASS),
            (GatewayPolicy.BLOCK_OUT, GatewayPolicy.BYPASS),
        )
        for loser, winner in ordered:
            with self.subTest(loser=loser, winner=winner):
                rules = GatewayRuleSet([l7(loser), l7(winner)])
                decision = rules.decide(HOST, 443, "GET")
                assert decision is not None
                self.assertEqual(decision.policy, winner)

    def test_allow_only_hit_beats_a_matching_block(self) -> None:
        rules = GatewayRuleSet(
            [l7(GatewayPolicy.BLOCK_OUT), l7(GatewayPolicy.ALLOW_ONLY)]
        )
        self.assertIsNone(rules.decide(HOST, 443, "GET"))

    def test_allow_only_miss_falls_back_to_bypass(self) -> None:
        rules = GatewayRuleSet([l7(GatewayPolicy.ALLOW_ONLY, "allowed.test")])
        self.assertEqual(
            rules.decide(HOST, 443, "GET"), GatewayDecision(GatewayPolicy.BYPASS)
        )

    def test_same_priority_keeps_the_earlier_rule(self) -> None:
        rules = GatewayRuleSet(
            [l7(GatewayPolicy.BLOCK_OUT, status_code=418), l7(GatewayPolicy.BLOCK_IN)]
        )
        decision = rules.decide(HOST, 443, "GET")
        assert decision is not None
        self.assertEqual(decision.policy, GatewayPolicy.BLOCK_OUT)
        self.assertEqual(decision.status_code, 418)

    def test_status_code_travels_with_the_decision(self) -> None:
        rules = GatewayRuleSet([l7(GatewayPolicy.BLOCK_OUT, status_code=451)])
        decision = rules.decide(HOST, 443, "GET")
        assert decision is not None
        self.assertEqual(decision.status_code, 451)

    def test_non_matching_rules_decide_nothing(self) -> None:
        rules = GatewayRuleSet([l7(GatewayPolicy.BLOCK_OUT, "other.test")])
        self.assertIsNone(rules.decide(HOST, 443, "GET"))


class GatewayOptionUpdatesTests(unittest.TestCase):
    def test_every_option_is_always_present(self) -> None:
        for rules in ([], [l4(GatewayPolicy.BYPASS)], [l7(GatewayPolicy.BLOCK_OUT)]):
            with self.subTest(count=len(rules)):
                self.assertEqual(
                    sorted(gateway_option_updates(rules)), sorted(GATEWAY_OPTIONS)
                )

    def test_no_rules_clears_both_options(self) -> None:
        self.assertEqual(
            gateway_option_updates([]), {"allow_hosts": [], "ignore_hosts": []}
        )

    def test_flow_level_rules_never_reach_the_native_options(self) -> None:
        rules = [l7(GatewayPolicy.BYPASS), l7(GatewayPolicy.ALLOW_ONLY)]
        self.assertEqual(
            gateway_option_updates(rules), {"allow_hosts": [], "ignore_hosts": []}
        )

    def test_bypass_lands_on_ignore_hosts(self) -> None:
        rule = l4(GatewayPolicy.BYPASS)
        self.assertEqual(
            gateway_option_updates([rule]),
            {"allow_hosts": [], "ignore_hosts": [rule.pattern]},
        )

    def test_allow_only_lands_on_allow_hosts(self) -> None:
        rule = l4(GatewayPolicy.ALLOW_ONLY)
        self.assertEqual(
            gateway_option_updates([rule]),
            {"allow_hosts": [rule.pattern], "ignore_hosts": []},
        )

    def test_allow_only_forces_ignore_hosts_empty(self) -> None:
        """原生先查 allow 再查 ignore，两者都命中的结果是忽略 —— 优先级正好反了。"""
        rules = [l4(GatewayPolicy.ALLOW_ONLY), l4(GatewayPolicy.BYPASS, "other.test")]
        updates = gateway_option_updates(rules)
        self.assertEqual(updates["allow_hosts"], [rules[0].pattern])
        self.assertEqual(updates["ignore_hosts"], [])

    def test_transport_block_reaches_neither_option(self) -> None:
        self.assertEqual(
            gateway_option_updates([l4(GatewayPolicy.BLOCK)]),
            {"allow_hosts": [], "ignore_hosts": []},
        )

    def test_master_switch_off_clears_everything(self) -> None:
        rules = [l4(GatewayPolicy.BYPASS), l4(GatewayPolicy.ALLOW_ONLY, "a.test")]
        self.assertEqual(
            gateway_option_updates(rules, enabled=False),
            {"allow_hosts": [], "ignore_hosts": []},
        )

    def test_disabled_rules_are_skipped(self) -> None:
        rules = [l4(GatewayPolicy.BYPASS), l4(GatewayPolicy.ALLOW_ONLY, "a.test")]
        self.assertEqual(
            gateway_option_updates([replace(rule, enabled=False) for rule in rules]),
            {"allow_hosts": [], "ignore_hosts": []},
        )

    def test_one_broken_rule_raises_before_anything_is_returned(self) -> None:
        rules = [
            l4(GatewayPolicy.BYPASS),
            l4(GatewayPolicy.BYPASS, "(", logic=GatewayLogic.REGEX),
        ]
        with self.assertRaises(ValueError):
            gateway_option_updates(rules)

    def test_rule_order_is_preserved_within_an_option(self) -> None:
        rules = [l4(GatewayPolicy.BYPASS, "a.test"), l4(GatewayPolicy.BYPASS, "b.test")]
        self.assertEqual(
            gateway_option_updates(rules)["ignore_hosts"],
            [rules[0].pattern, rules[1].pattern],
        )


class GatewayConfigTests(unittest.TestCase):
    def test_round_trip_preserves_all_seven_fields(self) -> None:
        rule = GatewayRule(
            layer=GatewayLayer.L4,
            policy=GatewayPolicy.ALLOW_ONLY,
            field=GatewayField.HOST,
            logic=GatewayLogic.EQUALS,
            value=HOST,
            status_code=451,
            enabled=False,
        )
        self.assertEqual(
            gateway_rules_from_config(gateway_rules_to_config([rule])), [rule]
        )

    def test_serialized_enums_are_plain_strings(self) -> None:
        raw = l4(GatewayPolicy.BYPASS).to_dict()
        self.assertEqual(raw["layer"], "l4")
        self.assertEqual(raw["policy"], "bypass")
        self.assertIs(type(raw["layer"]), str)

    def test_unparseable_entries_are_dropped(self) -> None:
        good = l7(GatewayPolicy.BLOCK_OUT).to_dict()
        raw = [good, {"layer": "l9"}, "not-a-dict", {"status_code": "abc"}]
        self.assertEqual(gateway_rules_from_config(raw), [GatewayRule.from_dict(good)])

    def test_non_list_config_yields_no_rules(self) -> None:
        for raw in (None, {}, "x", 3):
            with self.subTest(raw=raw):
                self.assertEqual(gateway_rules_from_config(raw), [])

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        rule = GatewayRule.from_dict({"value": "a"})
        self.assertEqual(
            rule, GatewayRule(value="a", status_code=GATEWAY_STATUS_DEFAULT)
        )

    def test_non_dict_raises_type_error(self) -> None:
        with self.assertRaises(TypeError):
            GatewayRule.from_dict(["l7"])

    def test_unknown_enum_member_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            GatewayRule.from_dict({"policy": "nope"})
        self.assertIn("未知的规则字段", str(ctx.exception))

    def test_unparseable_status_code_raises_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            GatewayRule.from_dict({"status_code": "abc"})
        self.assertIn("状态码", str(ctx.exception))


class LegacyMigrationTests(unittest.TestCase):
    @staticmethod
    def old(field: str, **kwargs) -> dict:
        raw = {"field": field, "logic": "contains", "value": HOST}
        raw.update(kwargs)
        return raw

    def test_host_rules_become_flow_level_block_out(self) -> None:
        rules, dropped = gateway_rules_from_block_config(
            [self.old("host", logic="equals", status_code=451, enabled=False)]
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(
            rules,
            [
                GatewayRule(
                    layer=GatewayLayer.L7,
                    policy=GatewayPolicy.BLOCK_OUT,
                    field=GatewayField.HOST,
                    logic=GatewayLogic.EQUALS,
                    value=HOST,
                    status_code=451,
                    enabled=False,
                )
            ],
        )

    def test_method_rules_keep_their_field(self) -> None:
        rules, dropped = gateway_rules_from_block_config(
            [self.old("method", value="POST")]
        )
        self.assertEqual(dropped, 0)
        self.assertEqual(rules[0].field, GatewayField.METHOD)
        self.assertEqual(rules[0].policy, GatewayPolicy.BLOCK_OUT)

    def test_url_rules_have_no_equivalent_and_are_counted(self) -> None:
        """网关的匹配对象不含 URI，老规则只能丢弃 —— 悄悄消失比丢弃更糟。"""
        rules, dropped = gateway_rules_from_block_config([self.old("url")])
        self.assertEqual((rules, dropped), ([], 1))

    def test_mixed_config_migrates_what_it_can_and_counts_the_rest(self) -> None:
        rules, dropped = gateway_rules_from_block_config(
            [
                self.old("host"),
                self.old("url"),
                self.old("method", value="GET"),
                self.old("url", value="other"),
            ]
        )
        self.assertEqual(len(rules), 2)
        self.assertEqual(dropped, 2)

    def test_empty_and_garbage_input_migrates_nothing(self) -> None:
        for raw in ([], None, "x", [{"field": "nope"}], ["not-a-dict"]):
            with self.subTest(raw=raw):
                self.assertEqual(gateway_rules_from_block_config(raw), ([], 0))

    def test_migrated_rules_all_validate(self) -> None:
        rules, _ = gateway_rules_from_block_config(
            [self.old("host"), self.old("method", value="POST")]
        )
        for rule in rules:
            with self.subTest(rule=rule):
                rule.validate()


class FerretMasterGatewayOptionTests(unittest.TestCase):
    """The native half of the gateway: both planes must compile identically."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)
        self.next_layer = self.master.addons.get("nextlayer")

    def test_both_options_exist_once_next_layer_is_loaded(self) -> None:
        for option in GATEWAY_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, self.master.options)

    def test_retired_block_list_option_is_gone(self) -> None:
        """原生 BlockList 已从链上撤掉；这一条防的是有人把它悄悄加回来。"""
        self.assertNotIn("block_list", self.master.options)
        self.assertNotIn(
            BlockList.__name__,
            [type(addon).__name__ for addon in self.master.addons.chain],
        )

    def test_native_compiles_our_pattern_byte_for_byte(self) -> None:
        rule = l4(GatewayPolicy.ALLOW_ONLY, HOST, logic=GatewayLogic.EQUALS)
        self.master.options.update(**gateway_option_updates([rule]))
        compiled = self.next_layer.allow_hosts
        self.assertEqual([item.pattern for item in compiled], [rule.pattern])
        self.assertTrue(compiled[0].flags & re.IGNORECASE)
        self.assertEqual(self.next_layer.ignore_hosts, [])

    def test_bypass_rules_populate_ignore_hosts(self) -> None:
        rule = l4(GatewayPolicy.BYPASS)
        self.master.options.update(**gateway_option_updates([rule]))
        self.assertEqual(
            [item.pattern for item in self.next_layer.ignore_hosts], [rule.pattern]
        )

    def test_empty_updates_clear_previously_pushed_patterns(self) -> None:
        self.master.options.update(**gateway_option_updates([l4(GatewayPolicy.BYPASS)]))
        self.master.options.update(**gateway_option_updates([]))
        self.assertEqual(self.next_layer.ignore_hosts, [])
        self.assertEqual(self.next_layer.allow_hosts, [])

    def test_broken_pattern_updates_the_option_but_not_the_kernel(self) -> None:
        """这就是 `GatewayRule.compile` 必须先编一遍的理由。

        `NextLayer.configure` 抛的 ``re.error`` 被 `addonmanager.safecall` 记日志吞掉
        （只有 ``AddonHalt`` / ``OptionsError`` 会重抛），于是**选项值已经更新、编译
        后的列表还是旧的** —— 界面显示生效、内核其实没生效，而且一声不响。
        """
        good = l4(GatewayPolicy.ALLOW_ONLY)
        self.master.options.update(**gateway_option_updates([good]))
        # assertLogs 顺手把 root handler 换成自己的：Master 装的那个 handler 会往它的
        # event loop 上 call_soon_threadsafe，别的用例关掉 loop 之后就炸。
        with self.assertLogs(level="ERROR") as caught:
            self.master.options.update(allow_hosts=["("], ignore_hosts=[])
        self.assertTrue(any("Addon error" in line for line in caught.output))
        self.assertEqual(self.master.options.allow_hosts, ["("])
        self.assertEqual(
            [item.pattern for item in self.next_layer.allow_hosts], [good.pattern]
        )

    def test_ferret_rejects_the_broken_pattern_before_it_reaches_the_kernel(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            gateway_option_updates(
                [l4(GatewayPolicy.ALLOW_ONLY, "(", logic=GatewayLogic.REGEX)]
            )

    def test_options_error_rolls_the_whole_batch_back(self) -> None:
        """`optmanager.rollback` 只认 ``OptionsError``；这是 `_push_gateway` 的兜底。"""
        good = l4(GatewayPolicy.BYPASS)
        self.master.options.update(**gateway_option_updates([good]))
        with self.assertRaises(OptionsError):
            self.master.options.update(ignore_hosts=[], map_remote=["not-a-valid-spec"])
        self.assertEqual(self.master.options.ignore_hosts, [good.pattern])

    def test_next_layer_is_the_addon_that_owns_these_options(self) -> None:
        self.assertIsInstance(self.next_layer, NextLayer)


if __name__ == "__main__":
    unittest.main()
