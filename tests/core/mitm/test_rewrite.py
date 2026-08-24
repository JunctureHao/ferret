"""Tests for the rewrite-rule model and its wiring into FerretMaster."""

import asyncio
import os
import re
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from mitmproxy.addons.maplocal import MapLocal, parse_map_local_spec
from mitmproxy.addons.mapremote import MapRemote, parse_map_remote_spec
from mitmproxy.addons.modifybody import ModifyBody
from mitmproxy.addons.modifyheaders import ModifyHeaders, parse_modify_spec
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.exceptions import OptionsError
from mitmproxy.test import tflow

from ferret.core.mitm import (
    BODY_KINDS,
    FILE_REPLACEMENT_PREFIX,
    HEADER_KINDS,
    MAP_KINDS,
    REWRITE_OPTIONS,
    WHOLE_BODY_PATTERN,
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

# `kind_rule` 默认匹配的那台主机，配一条真的落在它上面的 URL。
URL = "http://api.example.com/v1"


def flow_to(url: str, *, resp: bool = False):
    """A flow whose ``pretty_url`` is exactly ``url``.

    `tflow.tflow()` 的默认 URL 是 ``http://address:22/path``，测 URL 匹配得自己指定。
    """
    flow = tflow.tflow(resp=resp)
    flow.request.url = url
    return flow


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
        with self.assertRaisesRegex(ValueError, "Invalid match value"):
            rule(RewriteLogic.REGEX, "bad(", "http://x.com/").to_spec()
        with self.assertRaisesRegex(ValueError, "Invalid rewrite target"):
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
            {"kind": "map_sideways", "value": "c", "replacement": "d"},
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
            RewriteRule(
                kind=RewriteKind.MAP_REMOTE,
                logic=RewriteLogic.CONTAINS,
                value="a",
                target="",
                replacement="",
                enabled=True,
            ),
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


def kind_rule(kind: RewriteKind, **kwargs: Any) -> RewriteRule:
    fields: dict[str, Any] = {
        "logic": RewriteLogic.CONTAINS,
        "value": "api.example.com",
        **kwargs,
    }
    return RewriteRule(kind=kind, **fields)


class RewriteKindOptionMapTests(unittest.TestCase):
    """六种类型落在四个原生选项上，映射不能漏也不能重。"""

    def test_every_kind_has_an_option(self) -> None:
        for kind in RewriteKind:
            with self.subTest(kind=kind):
                self.assertIn(kind.option, REWRITE_OPTIONS)

    def test_the_option_list_is_deduplicated(self) -> None:
        """一个选项出现两次的话，后一个空列表会把前一个刚攒好的 spec 抹掉。"""
        self.assertEqual(len(REWRITE_OPTIONS), len(set(REWRITE_OPTIONS)))
        self.assertEqual(
            set(REWRITE_OPTIONS),
            {"map_remote", "map_local", "modify_headers", "modify_body"},
        )

    def test_header_and_body_kinds_share_one_addon_each(self) -> None:
        self.assertEqual({k.option for k in HEADER_KINDS}, {"modify_headers"})
        self.assertEqual({k.option for k in BODY_KINDS}, {"modify_body"})

    def test_the_kind_sets_cover_every_kind_exactly_once(self) -> None:
        self.assertEqual(MAP_KINDS | HEADER_KINDS | BODY_KINDS, set(RewriteKind))
        self.assertEqual(MAP_KINDS & HEADER_KINDS, set())
        self.assertEqual(HEADER_KINDS & BODY_KINDS, set())


class RewriteRuleFilledTests(unittest.TestCase):
    """没填完的规则整条跳过，不能抛错 —— options.update 是原子的。"""

    def test_a_blank_value_is_never_filled(self) -> None:
        for kind in RewriteKind:
            with self.subTest(kind=kind):
                self.assertFalse(kind_rule(kind, value="  ").filled)

    def test_map_kinds_need_a_replacement(self) -> None:
        for kind in MAP_KINDS:
            with self.subTest(kind=kind):
                self.assertFalse(kind_rule(kind).filled)
                self.assertTrue(kind_rule(kind, replacement="x").filled)

    def test_header_kinds_need_a_header_name(self) -> None:
        for kind in HEADER_KINDS:
            with self.subTest(kind=kind):
                self.assertFalse(kind_rule(kind).filled)
                self.assertTrue(kind_rule(kind, target="X-A").filled)

    def test_body_kinds_need_nothing_else(self) -> None:
        """空正则 = 整体替换，空内容 = 清空 body，两栏都可以留空。"""
        for kind in BODY_KINDS:
            with self.subTest(kind=kind):
                self.assertTrue(kind_rule(kind).filled)


class HeaderSpecTests(unittest.TestCase):
    def spec(self, kind: RewriteKind, **kwargs) -> str:
        return kind_rule(kind, target="X-Token", replacement="abc", **kwargs).to_spec()

    def test_the_native_parser_reads_back_what_we_meant(self) -> None:
        for kind in HEADER_KINDS:
            with self.subTest(kind=kind):
                parsed = parse_modify_spec(self.spec(kind), False)
                self.assertEqual(parsed.subject, b"X-Token")
                self.assertEqual(parsed.replacement_str, "abc")

    def test_the_request_kind_only_matches_before_the_response(self) -> None:
        parsed = parse_modify_spec(self.spec(RewriteKind.MODIFY_REQUEST_HEADER), False)
        self.assertTrue(parsed.matches(flow_to(URL)))
        self.assertFalse(parsed.matches(flow_to(URL, resp=True)))

    def test_the_response_kind_only_matches_after_the_response(self) -> None:
        parsed = parse_modify_spec(self.spec(RewriteKind.MODIFY_RESPONSE_HEADER), False)
        self.assertFalse(parsed.matches(flow_to(URL)))
        self.assertTrue(parsed.matches(flow_to(URL, resp=True)))

    def test_the_url_match_lands_in_the_flow_filter(self) -> None:
        """头名占了 subject，URL 只能塞进 flow-filter 段。"""
        spec = self.spec(RewriteKind.MODIFY_REQUEST_HEADER, value="nowhere.invalid")
        parsed = parse_modify_spec(spec, False)
        self.assertFalse(parsed.matches(flow_to(URL)))
        self.assertTrue(parsed.matches(flow_to("http://nowhere.invalid/x")))

    def test_a_blank_header_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="  ").to_spec()

    def test_a_header_name_with_a_newline_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="A\nB").to_spec()

    def test_an_empty_value_removes_the_header(self) -> None:
        """原生语义：替换串为空 = 删掉这个头（modifyheaders.py）。"""
        spec = kind_rule(
            RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement=""
        ).to_spec()
        self.assertEqual(parse_modify_spec(spec, False).replacement_str, "")


class BodySpecTests(unittest.TestCase):
    def spec(self, kind: RewriteKind, **kwargs) -> str:
        defaults = {"replacement": "REPLACED"}
        return kind_rule(kind, **{**defaults, **kwargs}).to_spec()

    def test_a_blank_target_means_replace_the_whole_body(self) -> None:
        parsed = parse_modify_spec(self.spec(RewriteKind.MODIFY_RESPONSE_BODY), True)
        self.assertEqual(parsed.subject, WHOLE_BODY_PATTERN.encode())

    def test_the_whole_body_pattern_substitutes_exactly_once(self) -> None:
        """`.*` 会在非空匹配后再命中一次末尾空串，替换内容被插两遍。"""
        pattern = re.compile(WHOLE_BODY_PATTERN.encode(), re.DOTALL)
        self.assertEqual(pattern.sub(b"R", b"body"), b"R")

    def test_a_body_regex_is_kept_verbatim(self) -> None:
        parsed = parse_modify_spec(
            self.spec(RewriteKind.MODIFY_RESPONSE_BODY, target=r"\d+"), True
        )
        self.assertEqual(parsed.subject, rb"\d+")

    def test_leading_whitespace_in_the_regex_is_kept(self) -> None:
        """体正则里的空白有意义，不能 strip。"""
        parsed = parse_modify_spec(
            self.spec(RewriteKind.MODIFY_RESPONSE_BODY, target=" a"), True
        )
        self.assertEqual(parsed.subject, b" a")

    def test_the_request_kind_only_matches_before_the_response(self) -> None:
        parsed = parse_modify_spec(self.spec(RewriteKind.MODIFY_REQUEST_BODY), True)
        self.assertTrue(parsed.matches(flow_to(URL)))
        self.assertFalse(parsed.matches(flow_to(URL, resp=True)))

    def test_the_response_kind_only_matches_after_the_response(self) -> None:
        parsed = parse_modify_spec(self.spec(RewriteKind.MODIFY_RESPONSE_BODY), True)
        self.assertFalse(parsed.matches(flow_to(URL)))
        self.assertTrue(parsed.matches(flow_to(URL, resp=True)))

    def test_an_empty_replacement_clears_the_body(self) -> None:
        parsed = parse_modify_spec(
            self.spec(RewriteKind.MODIFY_RESPONSE_BODY, replacement=""), True
        )
        self.assertEqual(parsed.replacement_str, "")

    def test_a_broken_body_regex_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.spec(RewriteKind.MODIFY_RESPONSE_BODY, target="bad(")

    def test_a_trailing_backslash_is_rejected(self) -> None:
        r"""原生 escaped_str_to_bytes 会抛 "Trailing \ in string"。"""
        with self.assertRaises(ValueError):
            self.spec(RewriteKind.MODIFY_RESPONSE_BODY, target=BACKSLASH)


class ModifyReplacementTests(unittest.TestCase):
    """替换串的 `@文件` 语义与反斜杠转义，头/体两类共用一条路径。"""

    def test_a_literal_backslash_survives_the_native_unescaping(self) -> None:
        spec = kind_rule(
            RewriteKind.MODIFY_RESPONSE_BODY, replacement=BACKSLASH + "n"
        ).to_spec()
        parsed = parse_modify_spec(spec, True)
        self.assertEqual(parsed.read_replacement(), (BACKSLASH + "n").encode())

    def test_an_at_prefixed_replacement_is_read_from_a_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("from disk")
            path = handle.name
        self.addCleanup(os.unlink, path)
        spec = kind_rule(
            RewriteKind.MODIFY_RESPONSE_BODY,
            replacement=FILE_REPLACEMENT_PREFIX + path,
        ).to_spec()
        parsed = parse_modify_spec(spec, True)
        self.assertEqual(parsed.read_replacement(), b"from disk")

    def test_an_at_prefixed_missing_file_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_BODY,
                replacement=FILE_REPLACEMENT_PREFIX + "no/such/file.txt",
            ).to_spec()


class MapLocalSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = os.path.join(self.dir, "index.json")
        with open(self.file, "w", encoding="utf-8") as handle:
            handle.write("{}")

    def rule(self, **kwargs) -> RewriteRule:
        defaults = {"replacement": self.file}
        return kind_rule(RewriteKind.MAP_LOCAL, **{**defaults, **kwargs})

    def test_the_native_parser_reads_back_what_we_meant(self) -> None:
        parsed = parse_map_local_spec(self.rule().to_spec())
        self.assertEqual(parsed.regex, self.rule().subject)
        self.assertEqual(parsed.local_path, Path(self.file).resolve())

    def test_a_directory_target_is_accepted(self) -> None:
        parsed = parse_map_local_spec(self.rule(replacement=self.dir).to_spec())
        self.assertEqual(parsed.local_path, Path(self.dir).resolve())

    def test_a_missing_path_is_rejected_with_our_own_message(self) -> None:
        """原生 resolve(strict=True) 的失败会连坐整批规则，必须自己先拦。"""
        with self.assertRaises(ValueError) as ctx:
            self.rule(replacement=os.path.join(self.dir, "nope.json")).to_spec()
        self.assertIn("The local path does not exist", str(ctx.exception))

    def test_a_blank_path_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.rule(replacement="   ").to_spec()

    def test_a_broken_url_regex_is_blamed_on_the_match_value(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.rule(logic=RewriteLogic.REGEX, value="bad(").to_spec()
        self.assertIn("Invalid match value", str(ctx.exception))

    def test_the_separator_avoids_the_path(self) -> None:
        """Windows 路径里带 ``:`` 和 ``/``，分隔符必须挑一个都没出现的字符。"""
        spec = self.rule().to_spec()
        separator = spec[0]
        self.assertNotIn(separator, self.file)
        self.assertEqual(spec.count(separator), 2)


class FerretMasterRewriteAddonsTests(unittest.TestCase):
    """四个原生重写 addon 都要在链上，且都排在 View 之前。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_all_four_addons_are_loaded(self) -> None:
        for addon in (
            self.master.map_remote,
            self.master.map_local,
            self.master.modify_body,
            self.master.modify_headers,
        ):
            with self.subTest(addon=type(addon).__name__):
                self.assertIn(addon, self.master.addons.chain)

    def test_lookup_by_native_name(self) -> None:
        self.assertIs(self.master.addons.get("maplocal"), self.master.map_local)
        self.assertIs(self.master.addons.get("modifybody"), self.master.modify_body)
        self.assertIs(
            self.master.addons.get("modifyheaders"), self.master.modify_headers
        )

    def test_the_native_relative_order_is_preserved(self) -> None:
        """对齐原生 default_addons()：mapremote → maplocal → modifybody → modifyheaders。"""
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        order = [
            MapRemote.__name__,
            MapLocal.__name__,
            ModifyBody.__name__,
            ModifyHeaders.__name__,
        ]
        self.assertEqual([n for n in names if n in order], order)

    def test_every_rewrite_addon_runs_before_the_view(self) -> None:
        """流量表第一次上屏就该是重写后的报文，不能先闪一下原始值。"""
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        view_at = names.index(type(self.master.view).__name__)
        for name in (MapLocal.__name__, ModifyBody.__name__, ModifyHeaders.__name__):
            with self.subTest(addon=name):
                self.assertLess(names.index(name), view_at)

    def test_every_option_exists_once_the_addons_are_loaded(self) -> None:
        for option in REWRITE_OPTIONS:
            with self.subTest(option=option):
                self.assertIn(option, self.master.options)

    def test_one_update_populates_every_addon(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            rules = [
                kind_rule(RewriteKind.MAP_REMOTE, replacement="http://127.0.0.1:8000/"),
                kind_rule(RewriteKind.MAP_LOCAL, replacement=folder),
                kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-A"),
                kind_rule(RewriteKind.MODIFY_RESPONSE_HEADER, target="X-B"),
                kind_rule(RewriteKind.MODIFY_REQUEST_BODY, replacement="a"),
                kind_rule(RewriteKind.MODIFY_RESPONSE_BODY, replacement="b"),
            ]
            self.master.options.update(**rewrite_option_updates(rules))
        self.assertEqual(len(self.master.map_remote.replacements), 1)
        self.assertEqual(len(self.master.map_local.replacements), 1)
        self.assertEqual(len(self.master.modify_headers.replacements), 2)
        self.assertEqual(len(self.master.modify_body.replacements), 2)

    def test_deleting_every_rule_clears_every_addon(self) -> None:
        built = kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-A")
        self.master.options.update(**rewrite_option_updates([built]))
        self.assertEqual(len(self.master.modify_headers.replacements), 1)
        self.master.options.update(**rewrite_option_updates([]))
        self.assertEqual(self.master.modify_headers.replacements, [])
        self.assertEqual(self.master.modify_body.replacements, [])
        self.assertEqual(self.master.map_local.replacements, [])
        self.assertEqual(self.master.map_remote.replacements, [])


class RewriteEndToEndTests(unittest.TestCase):
    """规则 → spec → 选项 → 原生 addon 真的改到了报文。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def push(self, *rules: RewriteRule) -> None:
        self.master.options.update(**rewrite_option_updates(list(rules)))

    def test_a_request_header_rule_rewrites_the_request(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to(URL)
        self.master.modify_headers.requestheaders(flow)
        self.assertEqual(flow.request.headers["X-Token"], "new")

    def test_a_request_header_rule_leaves_other_urls_alone(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to("http://other.invalid/v1")
        self.master.modify_headers.requestheaders(flow)
        self.assertNotIn("X-Token", flow.request.headers)

    def test_a_request_header_rule_does_not_touch_the_response(self) -> None:
        """同一个 addon 挂了两侧钩子，方向只靠 `~q` / `~s` 区分。"""
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to(URL, resp=True)
        self.master.modify_headers.responseheaders(flow)
        self.assertNotIn("X-Token", flow.response.headers)

    def test_a_response_header_rule_rewrites_the_response(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to(URL, resp=True)
        self.master.modify_headers.responseheaders(flow)
        self.assertEqual(flow.response.headers["X-Token"], "new")

    def test_a_whole_body_rule_replaces_the_response_body(self) -> None:
        self.push(
            kind_rule(RewriteKind.MODIFY_RESPONSE_BODY, replacement='{"faked": true}')
        )
        flow = flow_to(URL, resp=True)
        flow.response.content = b'{"real": true}'
        self.master.modify_body.response(flow)
        self.assertEqual(flow.response.get_content(strict=False), b'{"faked": true}')

    def test_a_body_regex_rule_replaces_only_the_match(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_BODY, target="real", replacement="faked"
            )
        )
        flow = flow_to(URL, resp=True)
        flow.response.content = b'{"real": true}'
        self.master.modify_body.response(flow)
        self.assertEqual(flow.response.get_content(strict=False), b'{"faked": true}')

    def test_a_request_body_rule_rewrites_the_request(self) -> None:
        self.push(kind_rule(RewriteKind.MODIFY_REQUEST_BODY, replacement="sent"))
        flow = flow_to(URL)
        flow.request.content = b"original"
        self.master.modify_body.request(flow)
        self.assertEqual(flow.request.get_content(strict=False), b"sent")

    def test_a_map_local_rule_answers_from_disk_without_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"local": true}')
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=path))
            flow = flow_to(URL)
            self.master.map_local.request(flow)
        self.assertIsNotNone(flow.response)
        self.assertEqual(flow.response.get_content(strict=False), b'{"local": true}')

    def test_a_map_local_rule_leaves_other_urls_alone(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=path))
            flow = flow_to("http://other.invalid/v1")
            self.master.map_local.request(flow)
        self.assertIsNone(flow.response)
