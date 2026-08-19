"""Tests for the block-rule model and its wiring into FerretMaster."""

import asyncio
import unittest

from mitmproxy.addons.anticache import AntiCache
from mitmproxy.addons.blocklist import BlockList, parse_spec
from mitmproxy.addons.strip_dns_https_records import StripDnsHttpsRecords
from mitmproxy.exceptions import OptionsError

from ferret.core.mitm import (
    BLOCK_STATUS_CLOSE,
    BLOCK_STATUS_DEFAULT,
    BlockField,
    BlockLogic,
    BlockRule,
    FerretMaster,
)
from ferret.core.mitm.blocklist import (
    rules_from_config,
    rules_to_config,
    specs_from_rules,
)


class BlockRuleExpressionTests(unittest.TestCase):
    def test_contains_escapes_literal_value(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "example.com")
        self.assertEqual(rule.expression, r"~d example\.com")

    def test_equals_anchors_the_pattern(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.EQUALS, "example.com")
        self.assertEqual(rule.expression, r"~d ^example\.com$")

    def test_regex_is_passed_through_untouched(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.REGEX, r"^ads\..*")
        self.assertEqual(rule.expression, r"~d ^ads\..*")

    def test_url_field_uses_tilde_u(self) -> None:
        rule = BlockRule(BlockField.URL, BlockLogic.CONTAINS, "ads")
        self.assertEqual(rule.expression, "~u ads")

    def test_method_field_uses_tilde_m(self) -> None:
        rule = BlockRule(BlockField.METHOD, BlockLogic.EQUALS, "POST")
        self.assertEqual(rule.expression, "~m ^POST$")

    def test_value_with_space_is_quoted(self) -> None:
        rule = BlockRule(BlockField.METHOD, BlockLogic.REGEX, "a b")
        self.assertEqual(rule.expression, '~m "a b"')

    def test_blank_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "   ").expression


class BlockRuleSpecTests(unittest.TestCase):
    def test_separator_avoids_slashes_in_url_values(self) -> None:
        """Native parse_spec splits on option[0]; "/" would break a URL rule."""
        spec = BlockRule(BlockField.URL, BlockLogic.CONTAINS, "http://a/b").to_spec()
        self.assertEqual(spec[0], "|")
        parsed = parse_spec(spec)
        self.assertEqual(parsed.status_code, BLOCK_STATUS_DEFAULT)

    def test_naive_slash_separator_would_have_failed(self) -> None:
        """Guards the reason _pick_separator exists at all."""
        expression = BlockRule(
            BlockField.URL, BlockLogic.CONTAINS, "http://a/b"
        ).expression
        with self.assertRaises(ValueError):
            parse_spec(f"/{expression}/403")

    def test_separator_falls_back_when_the_first_candidate_collides(self) -> None:
        rule = BlockRule(BlockField.METHOD, BlockLogic.REGEX, "^(POST|PUT)$")
        spec = rule.to_spec()
        self.assertEqual(spec[0], "#")
        parse_spec(spec)

    def test_close_status_round_trips(self) -> None:
        rule = BlockRule(
            BlockField.HOST, BlockLogic.EQUALS, "example.com", BLOCK_STATUS_CLOSE
        )
        self.assertEqual(parse_spec(rule.to_spec()).status_code, BLOCK_STATUS_CLOSE)

    def test_out_of_range_status_is_rejected(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "a", 99)
        with self.assertRaises(ValueError):
            rule.to_spec()

    def test_invalid_regex_is_rejected_by_native_parse_spec(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.REGEX, "~~bad(")
        with self.assertRaises(ValueError):
            rule.to_spec()

    def test_spec_matches_a_flow(self) -> None:
        from mitmproxy.test import tflow

        spec = parse_spec(
            BlockRule(BlockField.HOST, BlockLogic.EQUALS, "example.com").to_spec()
        )
        flow = tflow.tflow()
        flow.request.host = "example.com"
        self.assertTrue(spec.matches(flow))
        flow.request.host = "other.com"
        self.assertFalse(spec.matches(flow))


class SpecsFromRulesTests(unittest.TestCase):
    def test_disabled_and_blank_rules_are_skipped(self) -> None:
        rules = [
            BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "a"),
            BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "b", enabled=False),
            BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "  "),
        ]
        self.assertEqual(specs_from_rules(rules), ["|~d a|403"])

    def test_one_bad_rule_fails_the_whole_batch(self) -> None:
        """options.update is atomic, so validation has to be too."""
        rules = [
            BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "a"),
            BlockRule(BlockField.HOST, BlockLogic.REGEX, "bad("),
        ]
        with self.assertRaises(ValueError):
            specs_from_rules(rules)


class ConfigRoundTripTests(unittest.TestCase):
    def test_rules_survive_a_config_round_trip(self) -> None:
        rules = [
            BlockRule(BlockField.URL, BlockLogic.REGEX, "^http://a/", 502, False),
            BlockRule(BlockField.METHOD, BlockLogic.EQUALS, "POST", 444),
        ]
        self.assertEqual(rules_from_config(rules_to_config(rules)), rules)

    def test_unparseable_entries_are_dropped(self) -> None:
        raw = [
            {"field": "host", "logic": "contains", "value": "a"},
            {"field": "nope", "logic": "contains", "value": "b"},
            {"field": "host", "logic": "contains", "value": "c", "status_code": "x"},
            "not a dict",
            None,
        ]
        rules = rules_from_config(raw)
        self.assertEqual([rule.value for rule in rules], ["a"])

    def test_non_list_config_yields_no_rules(self) -> None:
        self.assertEqual(rules_from_config({"field": "host"}), [])
        self.assertEqual(rules_from_config(None), [])

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        rule = BlockRule.from_dict({"value": "a"})
        self.assertEqual(
            rule,
            BlockRule(BlockField.HOST, BlockLogic.CONTAINS, "a", 403, True),
        )


class FerretMasterBlockListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_master_holds_a_block_list_instance(self) -> None:
        self.assertIsInstance(self.master.block_list, BlockList)
        self.assertIn(self.master.block_list, self.master.addons.chain)

    def test_block_list_lookup_by_name(self) -> None:
        self.assertIs(self.master.addons.get("blocklist"), self.master.block_list)

    def test_block_list_runs_between_strip_dns_and_anticache(self) -> None:
        """Matches native default_addons() ordering; View must still see the flow."""
        chain = self.master.addons.chain
        names = [type(addon).__name__ for addon in chain]
        self.assertEqual(
            names.index(StripDnsHttpsRecords.__name__) + 1,
            names.index(BlockList.__name__),
        )
        self.assertLess(
            names.index(BlockList.__name__), names.index(AntiCache.__name__)
        )
        self.assertLess(
            names.index(BlockList.__name__),
            names.index(type(self.master.view).__name__),
        )

    def test_option_is_registered_only_after_the_addon_is_added(self) -> None:
        self.assertIn("block_list", self.master.options)

    def test_update_option_populates_items(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.EQUALS, "example.com")
        self.master.options.update(block_list=[rule.to_spec()])
        self.assertEqual(len(self.master.block_list.items), 1)
        self.assertEqual(self.master.block_list.items[0].status_code, 403)

    def test_illegal_spec_raises_and_leaves_items_untouched(self) -> None:
        rule = BlockRule(BlockField.HOST, BlockLogic.EQUALS, "example.com")
        self.master.options.update(block_list=[rule.to_spec()])
        with self.assertRaises(OptionsError):
            self.master.options.update(block_list=["not-a-valid-spec"])
        self.assertEqual(len(self.master.block_list.items), 1)
        self.assertEqual(self.master.options.block_list, [rule.to_spec()])


if __name__ == "__main__":
    unittest.main()
