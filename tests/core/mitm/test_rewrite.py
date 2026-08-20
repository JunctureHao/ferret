"""Tests for the rewrite-rule model and its wiring into FerretMaster."""

import asyncio
import unittest
from dataclasses import replace

from mitmproxy.addons.mapremote import MapRemote, parse_map_remote_spec
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.exceptions import OptionsError
from mitmproxy.test import tflow

from ferret.core.mitm import (
    REWRITE_OPTIONS,
    FerretMaster,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
    escape_template,
    rewrite_option_updates,
    rewrite_rules_from_config,
    rewrite_rules_to_config,
)

BACKSLASH = "\\"


def rule(logic: RewriteLogic, value: str, replacement: str, **kwargs) -> RewriteRule:
    return RewriteRule(
        kind=RewriteKind.MAP_REMOTE,
        logic=logic,
        value=value,
        replacement=replacement,
        **kwargs,
    )


class RewriteRuleSubjectTests(unittest.TestCase):
    def test_contains_escapes_the_literal_value(self) -> None:
        self.assertEqual(
            rule(RewriteLogic.CONTAINS, "api.example.com", "x").subject,
            r"api\.example\.com",
        )

    def test_equals_anchors_the_whole_url(self) -> None:
        self.assertEqual(
            rule(RewriteLogic.EQUALS, "http://a.com/x", "y").subject,
            r"^http://a\.com/x$",
        )

    def test_regex_is_passed_through_untouched(self) -> None:
        pattern = r"^https://a\.com/(.*)"
        self.assertEqual(rule(RewriteLogic.REGEX, pattern, "y").subject, pattern)

    def test_blank_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _ = rule(RewriteLogic.CONTAINS, "   ", "y").subject

    def test_literal_template_keeps_slashes_but_doubles_backslashes(self) -> None:
        """`re.escape` on a replacement would leak backslashes into the URL."""
        built = rule(RewriteLogic.CONTAINS, "a.com", "b.com/x" + BACKSLASH).template
        self.assertEqual(built, "b.com/x" + BACKSLASH * 2)

    def test_regex_template_keeps_backreferences(self) -> None:
        template = "http://127.0.0.1:8000/" + BACKSLASH + "1"
        self.assertEqual(
            rule(RewriteLogic.REGEX, "^(.*)$", template).template, template
        )

    def test_blank_replacement_is_rejected(self) -> None:
        """An empty URL makes the native `request.url` setter raise mid-hook."""
        with self.assertRaises(ValueError):
            _ = rule(RewriteLogic.CONTAINS, "a.com", "  ").template

    def test_escape_template_only_doubles_backslashes(self) -> None:
        self.assertEqual(escape_template("a/b.c"), "a/b.c")
        self.assertEqual(escape_template(BACKSLASH), BACKSLASH * 2)


class RewriteRuleSpecTests(unittest.TestCase):
    def test_spec_round_trips_through_the_native_parser(self) -> None:
        built = rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000")
        spec = built.to_spec()
        self.assertEqual(spec[0], "|")
        parsed = parse_map_remote_spec(spec)
        self.assertEqual(parsed.subject, built.subject)
        self.assertEqual(parsed.replacement, built.template)

    def test_separator_falls_back_when_the_first_candidate_collides(self) -> None:
        built = rule(RewriteLogic.REGEX, "^(a|b)$", "http://x.com/")
        self.assertEqual(built.to_spec()[0], "#")
        parse_map_remote_spec(built.to_spec())

    def test_naive_slash_separator_would_be_silently_misparsed(self) -> None:
        """Guards why _pick_separator plus the round-trip check both exist.

        `parse_spec` does `rem.split(sep, 2)` and accepts 2 **or** 3 segments, so a
        "/" separator with a URL replacement is read as a filter + subject pair —
        no exception, just the wrong rule.
        """
        parsed = parse_map_remote_spec("/foo/http://new.com/x")
        self.assertEqual(parsed.subject, "http:")
        self.assertEqual(parsed.replacement, "/new.com/x")

    def test_bad_replacement_template_is_rejected_up_front(self) -> None:
        """The native parser only compiles the subject; `re.sub` explodes later."""
        bad = "|a|" + BACKSLASH + "1"
        parse_map_remote_spec(bad)  # native parser is happy with it
        with self.assertRaises(ValueError):
            rule(RewriteLogic.REGEX, "a", BACKSLASH + "1").to_spec()

    def test_unknown_group_name_is_rejected(self) -> None:
        """`re.sub` raises IndexError (not re.error) for an unknown group name."""
        with self.assertRaises(ValueError):
            rule(RewriteLogic.REGEX, "a", BACKSLASH + "g<nope>").to_spec()

    def test_invalid_subject_regex_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rule(RewriteLogic.REGEX, "bad(", "http://x.com/").to_spec()

    def test_each_field_is_blamed_for_its_own_error(self) -> None:
        """两栏的报错不能互相错怪：正则写一半时用户改的是「原始 URL」那一栏。"""
        with self.assertRaisesRegex(ValueError, "匹配值"):
            rule(RewriteLogic.REGEX, "bad(", "http://x.com/").to_spec()
        with self.assertRaisesRegex(ValueError, "重写目标"):
            rule(RewriteLogic.REGEX, "ok.com", BACKSLASH + "1").to_spec()

    def test_equals_requires_an_absolute_replacement_url(self) -> None:
        with self.assertRaises(ValueError):
            rule(RewriteLogic.EQUALS, "http://a.com/x", "127.0.0.1:8000").to_spec()
        rule(RewriteLogic.EQUALS, "http://a.com/x", "http://127.0.0.1:8000/x").to_spec()

    def test_contains_does_not_require_an_absolute_replacement(self) -> None:
        """CONTAINS only swaps a fragment, so the result URL keeps its scheme."""
        rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000").to_spec()

    def test_kind_without_a_spec_branch_is_rejected(self) -> None:
        """A future kind must fail loudly, not leak a KeyError past the runtime."""
        # 冒充一个还没落地的 kind：dataclasses.replace 的签名是 **changes: Any，
        # 所以这里不需要 type: ignore（加了 ty 会报 unused-type-ignore-comment）。
        built = replace(
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            kind="map_local",
        )
        with self.assertRaises(ValueError):
            built.to_spec()


class RewriteOptionUpdatesTests(unittest.TestCase):
    def test_every_option_is_always_present(self) -> None:
        """Deleting every rule has to clear the option, not leave the old specs."""
        updates = rewrite_option_updates([])
        self.assertEqual(sorted(updates), sorted(REWRITE_OPTIONS))
        self.assertEqual(updates[RewriteKind.MAP_REMOTE], [])

    def test_disabled_and_blank_rules_are_skipped(self) -> None:
        rules = [
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.CONTAINS, "c.com", "d.com", enabled=False),
            rule(RewriteLogic.CONTAINS, "   ", "d.com"),
        ]
        updates = rewrite_option_updates(rules)
        self.assertEqual(updates[RewriteKind.MAP_REMOTE], [r"|a\.com|b.com"])

    def test_rule_order_is_preserved(self) -> None:
        """MapRemote.request applies every spec in order, so order is semantic."""
        rules = [
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.CONTAINS, "b.com", "c.com"),
        ]
        self.assertEqual(
            rewrite_option_updates(rules)[RewriteKind.MAP_REMOTE],
            [r"|a\.com|b.com", r"|b\.com|c.com"],
        )

    def test_one_bad_rule_fails_the_whole_batch(self) -> None:
        """options.update is atomic, so validation has to be too."""
        rules = [
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.REGEX, "bad(", "b.com"),
        ]
        with self.assertRaises(ValueError):
            rewrite_option_updates(rules)


class ConfigRoundTripTests(unittest.TestCase):
    def test_rules_survive_a_config_round_trip(self) -> None:
        rules = [
            rule(RewriteLogic.REGEX, "^http://a/", "http://b/", enabled=False),
            rule(RewriteLogic.EQUALS, "http://a.com/x", "http://b.com/x"),
        ]
        self.assertEqual(
            rewrite_rules_from_config(rewrite_rules_to_config(rules)), rules
        )

    def test_unparseable_entries_are_dropped(self) -> None:
        raw = [
            {"logic": "contains", "value": "a", "replacement": "b"},
            {"logic": "nope", "value": "b", "replacement": "c"},
            {"kind": "map_local", "value": "c", "replacement": "d"},
            "not a dict",
            None,
        ]
        self.assertEqual([r.value for r in rewrite_rules_from_config(raw)], ["a"])

    def test_non_list_config_yields_no_rules(self) -> None:
        self.assertEqual(rewrite_rules_from_config({"value": "a"}), [])
        self.assertEqual(rewrite_rules_from_config(None), [])

    def test_missing_keys_fall_back_to_defaults(self) -> None:
        self.assertEqual(
            RewriteRule.from_dict({"value": "a"}),
            RewriteRule(RewriteKind.MAP_REMOTE, RewriteLogic.CONTAINS, "a", "", True),
        )


class FerretMasterMapRemoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_master_holds_a_map_remote_instance(self) -> None:
        self.assertIsInstance(self.master.map_remote, MapRemote)
        self.assertIn(self.master.map_remote, self.master.addons.chain)

    def test_map_remote_lookup_by_name(self) -> None:
        self.assertIs(self.master.addons.get("mapremote"), self.master.map_remote)

    def test_map_remote_runs_after_next_layer_and_before_the_view(self) -> None:
        """Matches native default_addons() ordering; the table must see new URLs."""
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        self.assertEqual(
            names.index(NextLayer.__name__) + 1, names.index(MapRemote.__name__)
        )
        self.assertLess(
            names.index(MapRemote.__name__),
            names.index(type(self.master.view).__name__),
        )

    def test_option_exists_once_the_addon_is_loaded(self) -> None:
        self.assertIn("map_remote", self.master.options)

    def test_update_option_populates_replacements(self) -> None:
        built = rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000")
        self.master.options.update(**rewrite_option_updates([built]))
        self.assertEqual(len(self.master.map_remote.replacements), 1)
        self.assertEqual(self.master.map_remote.replacements[0].subject, built.subject)

    def test_illegal_spec_raises_and_leaves_replacements_untouched(self) -> None:
        built = rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000")
        self.master.options.update(map_remote=[built.to_spec()])
        with self.assertRaises(OptionsError):
            self.master.options.update(map_remote=["not-a-valid-spec"])
        self.assertEqual(len(self.master.map_remote.replacements), 1)
        self.assertEqual(self.master.options.map_remote, [built.to_spec()])

    def test_contains_rule_rewrites_only_the_matched_fragment(self) -> None:
        built = rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000")
        self.master.options.update(map_remote=[built.to_spec()])
        flow = tflow.tflow()
        flow.request.url = "https://api.example.com/v1/user"
        self.master.map_remote.request(flow)
        self.assertEqual(flow.request.pretty_url, "https://127.0.0.1:8000/v1/user")
        self.assertEqual(flow.request.port, 8000)

    def test_equals_rule_replaces_the_whole_url_and_the_host_header(self) -> None:
        built = rule(
            RewriteLogic.EQUALS,
            "https://api.example.com/v1",
            "http://127.0.0.1:8000/v1",
        )
        self.master.options.update(map_remote=[built.to_spec()])
        flow = tflow.tflow()
        flow.request.url = "https://api.example.com/v1"
        flow.request.headers["Host"] = "api.example.com"
        self.master.map_remote.request(flow)
        self.assertEqual(flow.request.pretty_url, "http://127.0.0.1:8000/v1")
        # 原生 setter 连 Host 头和端口一起改，这也是我们不自己动手的理由。
        self.assertEqual(flow.request.headers["Host"], "127.0.0.1:8000")
        self.assertEqual(flow.request.scheme, "http")

    def test_regex_rule_expands_backreferences(self) -> None:
        built = rule(
            RewriteLogic.REGEX,
            r"^https://api\.example\.com/(.*)",
            "http://127.0.0.1:8000/" + BACKSLASH + "1",
        )
        self.master.options.update(map_remote=[built.to_spec()])
        flow = tflow.tflow()
        flow.request.url = "https://api.example.com/v1/user"
        self.master.map_remote.request(flow)
        self.assertEqual(flow.request.pretty_url, "http://127.0.0.1:8000/v1/user")

    def test_non_matching_flow_is_left_alone(self) -> None:
        built = rule(RewriteLogic.EQUALS, "https://a.com/x", "http://b.com/x")
        self.master.options.update(map_remote=[built.to_spec()])
        flow = tflow.tflow()
        flow.request.url = "https://other.com/x"
        self.master.map_remote.request(flow)
        self.assertEqual(flow.request.pretty_url, "https://other.com/x")

    def test_rules_apply_cumulatively_in_order(self) -> None:
        """MapRemote.request has no `break`: every matching spec runs in turn."""
        rules = [
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.CONTAINS, "b.com", "c.com"),
        ]
        self.master.options.update(**rewrite_option_updates(rules))
        flow = tflow.tflow()
        flow.request.url = "https://a.com/x"
        self.master.map_remote.request(flow)
        self.assertEqual(flow.request.pretty_url, "https://c.com/x")

    def test_empty_option_clears_previous_rules(self) -> None:
        built = rule(RewriteLogic.CONTAINS, "a.com", "b.com")
        self.master.options.update(map_remote=[built.to_spec()])
        self.master.options.update(**rewrite_option_updates([]))
        self.assertEqual(self.master.map_remote.replacements, [])


if __name__ == "__main__":
    unittest.main()
