"""Tests for the rewrite-rule model, the self-built addon and its wiring."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.test import tflow

from ferret.core.mitm import (
    BODY_KINDS,
    FILE_REPLACEMENT_PREFIX,
    HEADER_KINDS,
    MAP_KINDS,
    REPLACE_KINDS,
    REPLACE_RESPONSE_DEFAULT_STATUS,
    REWRITE_ANSWERED_KEY,
    WHOLE_BODY_PATTERN,
    FerretMaster,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
    RewriteRuleSet,
    escape_template,
    read_replacement,
    rewrite_rules_from_config,
    rewrite_rules_to_config,
)
from ferret.core.mitm.addons import FerretRewriteAddon

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


def kind_rule(kind: RewriteKind, **kwargs: Any) -> RewriteRule:
    fields: dict[str, Any] = {
        "logic": RewriteLogic.CONTAINS,
        "value": "api.example.com",
        **kwargs,
    }
    return RewriteRule(kind=kind, **fields)


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


class RewriteRuleValidateTests(unittest.TestCase):
    """校验把「哪一栏写坏了怪哪一栏」讲清楚；过不了就不让保存。"""

    def test_each_field_is_blamed_for_its_own_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "无效的匹配值"):
            rule(RewriteLogic.REGEX, "bad(", "http://x.com/").validate()
        with self.assertRaisesRegex(ValueError, "无效的重写目标"):
            rule(RewriteLogic.REGEX, "ok.com", BACKSLASH + "1").validate()

    def test_equals_requires_an_absolute_replacement_url(self) -> None:
        with self.assertRaises(ValueError):
            rule(
                RewriteLogic.EQUALS, "http://a.com/x", "127.0.0.1:8000"
            ).validate()
        rule(
            RewriteLogic.EQUALS, "http://a.com/x", "http://127.0.0.1:8000/x"
        ).validate()

    def test_contains_does_not_require_an_absolute_replacement(self) -> None:
        """CONTAINS only swaps a fragment, so the result URL keeps its scheme."""
        rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000").validate()

    def test_a_blank_header_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能为空"):
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="  ", replacement="x"
            ).validate()

    def test_a_header_name_with_a_newline_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能含换行"):
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="A\nB"
            ).validate()

    def test_a_broken_body_regex_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "无效的体正则"):
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_BODY, target="bad(", replacement="x"
            ).validate()

    def test_a_missing_local_path_is_rejected(self) -> None:
        """和原生 resolve(strict=True) 同一道闸，挡住绝大多数手滑。"""
        with self.assertRaisesRegex(ValueError, "本地路径不存在或不可访问"):
            kind_rule(
                RewriteKind.MAP_LOCAL, replacement="no/such/path.json"
            ).validate()

    def test_a_bad_backreference_in_a_replace_body_is_not_the_engines_business(self):
        """体替换是字面量，反斜杠不是元字符 —— 规则照单全收。"""
        built = kind_rule(
            RewriteKind.REPLACE_RESPONSE,
            status_code=200,
            replacement="body" + BACKSLASH + "1",
        )
        built.validate()


class RewriteKindSetTests(unittest.TestCase):
    def test_the_kind_sets_cover_every_kind_exactly_once(self) -> None:
        self.assertEqual(
            MAP_KINDS | HEADER_KINDS | BODY_KINDS | REPLACE_KINDS, set(RewriteKind)
        )
        for left in (MAP_KINDS, HEADER_KINDS, BODY_KINDS, REPLACE_KINDS):
            for right in (MAP_KINDS, HEADER_KINDS, BODY_KINDS, REPLACE_KINDS):
                if left is not right:
                    self.assertEqual(left & right, set())


class RewriteRuleFilledTests(unittest.TestCase):
    """没填完的规则整条跳过，不能抛错 —— 下发是整批的。"""

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

    def test_replace_kinds_need_at_least_one_field(self) -> None:
        with self.subTest(kind=RewriteKind.REPLACE_REQUEST):
            self.assertFalse(kind_rule(RewriteKind.REPLACE_REQUEST).filled)
            self.assertTrue(
                kind_rule(RewriteKind.REPLACE_REQUEST, method="POST").filled
            )
            self.assertTrue(
                kind_rule(RewriteKind.REPLACE_REQUEST, path="/v1").filled
            )
            self.assertTrue(
                kind_rule(
                    RewriteKind.REPLACE_REQUEST, headers=(("A", "b"),)
                ).filled
            )
            self.assertTrue(
                kind_rule(RewriteKind.REPLACE_REQUEST, replacement="body").filled
            )
        with self.subTest(kind=RewriteKind.REPLACE_RESPONSE):
            self.assertFalse(kind_rule(RewriteKind.REPLACE_RESPONSE).filled)
            self.assertTrue(
                kind_rule(RewriteKind.REPLACE_RESPONSE, status_code=404).filled
            )
            self.assertTrue(
                kind_rule(
                    RewriteKind.REPLACE_RESPONSE, headers=(("A", "b"),)
                ).filled
            )
            self.assertTrue(
                kind_rule(RewriteKind.REPLACE_RESPONSE, replacement="body").filled
            )


class ReadReplacementTests(unittest.TestCase):
    """`@文件` 每请求现读，是相对原生「spec 解析时定格」的刻意差异。"""

    def test_a_literal_survives_the_utf8_round_trip(self) -> None:
        self.assertEqual(read_replacement("文本 body"), "文本 body".encode())

    def test_an_at_prefixed_replacement_is_read_from_a_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("from disk")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.assertEqual(
            read_replacement(FILE_REPLACEMENT_PREFIX + path), b"from disk"
        )

    def test_a_missing_file_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            read_replacement(FILE_REPLACEMENT_PREFIX + "no/such/file.txt")


class ConfigRoundTripTests(unittest.TestCase):
    def test_rules_survive_a_config_round_trip(self) -> None:
        rules = [
            rule(RewriteLogic.REGEX, "^http://a/", "http://b/", enabled=False),
            rule(RewriteLogic.EQUALS, "http://a.com/x", "http://b.com/x"),
            kind_rule(
                RewriteKind.REPLACE_REQUEST,
                method="POST",
                path="/v1",
                headers=(("X-A", "1"), ("X-B", "")),
                replacement="body",
            ),
            kind_rule(RewriteKind.REPLACE_RESPONSE, status_code=404),
            # 六类老规则的落盘形状一个字节都不变：替换类专属字段按需写出。
            kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-A"),
        ]
        restored = rewrite_rules_from_config(rewrite_rules_to_config(rules))
        self.assertEqual(restored, rules)
        self.assertNotIn("status_code", rewrite_rules_to_config(rules[:1])[0])
        self.assertNotIn("headers", rewrite_rules_to_config(rules[:1])[0])

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

    def test_bad_replace_payloads_are_dropped(self) -> None:
        raw = [
            {"kind": "replace_response", "value": "a", "status_code": "x"},
            {"kind": "replace_response", "value": "b", "headers": "nope"},
            {"kind": "replace_response", "value": "c", "status_code": 404},
        ]
        self.assertEqual(
            [r.value for r in rewrite_rules_from_config(raw)], ["c"]
        )

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


class RewriteRuleSetTests(unittest.TestCase):
    """编译快照：停用/没填完的跳过，坏规则在构造期抛。"""

    def test_disabled_and_blank_rules_are_skipped(self) -> None:
        ruleset = RewriteRuleSet(
            [
                rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
                rule(RewriteLogic.CONTAINS, "c.com", "d.com", enabled=False),
                rule(RewriteLogic.CONTAINS, "   ", "d.com"),
            ]
        )
        self.assertEqual(len(ruleset), 1)

    def test_rule_order_is_preserved(self) -> None:
        """行序＝执行序（跨类型也有意义）。"""
        ruleset = RewriteRuleSet(
            [
                kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-A"),
                kind_rule(RewriteKind.REPLACE_REQUEST, method="POST"),
                rule(RewriteLogic.CONTAINS, "b.com", "c.com"),
            ]
        )
        kinds = [entry.rule.kind for entry in ruleset.entries()]
        self.assertEqual(
            kinds,
            [
                RewriteKind.MODIFY_REQUEST_HEADER,
                RewriteKind.REPLACE_REQUEST,
                RewriteKind.MAP_REMOTE,
            ],
        )

    def test_one_bad_rule_fails_the_whole_batch(self) -> None:
        rules = [
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.REGEX, "bad(", "b.com"),
        ]
        with self.assertRaises(ValueError):
            RewriteRuleSet(rules)

    def test_the_whole_body_pattern_substitutes_exactly_once(self) -> None:
        """`.*` 会在非空匹配后再命中一次末尾空串，替换内容被插两遍。"""
        import re

        pattern = re.compile(WHOLE_BODY_PATTERN, re.DOTALL)
        self.assertEqual(pattern.sub("R", "body"), "R")

    def test_an_empty_ruleset_is_falsy(self) -> None:
        self.assertFalse(RewriteRuleSet([]))
        self.assertTrue(RewriteRuleSet([kind_rule(RewriteKind.REPLACE_REQUEST, method="GET")]))


class FerretMasterRewriteAddonTests(unittest.TestCase):
    """自研件在链上，位置对齐原生四件的原链位。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_the_addon_is_loaded(self) -> None:
        self.assertIsInstance(self.master.rewrite, FerretRewriteAddon)
        self.assertIn(self.master.rewrite, self.master.addons.chain)

    def test_runs_after_next_layer_and_before_the_view(self) -> None:
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        self.assertEqual(
            names.index(NextLayer.__name__) + 1, names.index("FerretRewriteAddon")
        )
        self.assertLess(
            names.index("FerretRewriteAddon"),
            names.index(type(self.master.view).__name__),
        )

    def test_the_native_four_are_gone(self) -> None:
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        for native in ("MapRemote", "MapLocal", "ModifyBody", "ModifyHeaders"):
            self.assertNotIn(native, names)
        for option in ("map_remote", "map_local", "modify_headers", "modify_body"):
            self.assertNotIn(option, self.master.options)

    def test_set_rules_swaps_the_snapshot(self) -> None:
        built = kind_rule(RewriteKind.REPLACE_REQUEST, method="POST")
        self.master.rewrite.set_rules(RewriteRuleSet([built]), enabled=True)
        self.assertEqual(len(self.master.rewrite._rules), 1)
        self.master.rewrite.set_rules(RewriteRuleSet([]), enabled=False)
        self.assertEqual(len(self.master.rewrite._rules), 0)
        self.assertFalse(self.master.rewrite._enabled)


class RewriteEndToEndTests(unittest.TestCase):
    """规则 → 快照 → 自研件真的改到了报文（§5 契约逐条钉住）。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)
        self.addon = self.master.rewrite

    def push(self, *rules: RewriteRule, enabled: bool = True) -> None:
        self.addon.set_rules(RewriteRuleSet(list(rules)), enabled=enabled)

    # —— 匹配 ——

    def test_a_non_matching_flow_is_left_alone(self) -> None:
        self.push(kind_rule(RewriteKind.REPLACE_REQUEST, method="POST"))
        flow = flow_to("http://other.invalid/v1")
        self.addon.request(flow)
        self.assertEqual(flow.request.method, "GET")

    def test_rules_apply_cumulatively_in_order(self) -> None:
        self.push(
            rule(RewriteLogic.CONTAINS, "a.com", "b.com"),
            rule(RewriteLogic.CONTAINS, "b.com", "c.com"),
        )
        flow = flow_to("https://a.com/x")
        self.addon.request(flow)
        self.assertEqual(flow.request.pretty_url, "https://c.com/x")

    def test_the_master_switch_disables_every_rule(self) -> None:
        self.push(kind_rule(RewriteKind.REPLACE_REQUEST, method="POST"), enabled=False)
        flow = flow_to(URL)
        self.addon.request(flow)
        self.assertEqual(flow.request.method, "GET")

    def test_one_broken_rule_does_not_break_the_others(self) -> None:
        """一条规则执行炸了记日志跳过，整条钩子链照常。

        assertLogs 顺手把 root handler 换成自己的 —— Master 装的那个 handler 会往
        已关闭的 event loop 上 call_soon_threadsafe（test_gateway.py 同一个坑）。
        """
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER,
                target="X-Boom",
                replacement=FILE_REPLACEMENT_PREFIX + "no/such/file.txt",
            ),
            kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-Ok", replacement="1"),
        )
        flow = flow_to(URL)
        with self.assertLogs(level="WARNING"):
            self.addon.request(flow)
        self.assertEqual(flow.request.headers["X-Ok"], "1")

    # —— 修改请求/响应头 ——

    def test_a_request_header_rule_rewrites_the_request(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to(URL)
        flow.request.headers["X-token"] = "old"
        self.addon.request(flow)
        self.assertEqual(flow.request.headers["X-Token"], "new")

    def test_an_empty_header_value_removes_the_header(self) -> None:
        """头值留空 = 只删不加（对齐原生语义）。"""
        self.push(
            kind_rule(RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token")
        )
        flow = flow_to(URL)
        flow.request.headers["X-Token"] = "old"
        self.addon.request(flow)
        self.assertNotIn("X-Token", flow.request.headers)

    def test_an_at_prefixed_header_value_is_read_from_a_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("from disk")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER,
                target="X-Token",
                replacement=FILE_REPLACEMENT_PREFIX + path,
            )
        )
        flow = flow_to(URL)
        self.addon.request(flow)
        self.assertEqual(flow.request.headers["X-Token"], "from disk")

    def test_a_response_header_rule_rewrites_the_response(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_HEADER, target="X-Token", replacement="new"
            )
        )
        flow = flow_to(URL, resp=True)
        self.addon.response(flow)
        self.assertEqual(flow.response.headers["X-Token"], "new")

    def test_request_kinds_never_touch_the_response(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_REQUEST_HEADER, target="X-Token", replacement="new"
            ),
            kind_rule(RewriteKind.MODIFY_REQUEST_BODY, replacement="sent"),
        )
        flow = flow_to(URL, resp=True)
        flow.response.content = b'{"real": true}'
        self.addon.response(flow)
        self.assertNotIn("X-Token", flow.response.headers)
        self.assertEqual(flow.response.get_content(strict=False), b'{"real": true}')

    # —— 修改请求/响应体 ——

    def test_a_whole_body_rule_replaces_the_response_body(self) -> None:
        self.push(
            kind_rule(RewriteKind.MODIFY_RESPONSE_BODY, replacement='{"faked": true}')
        )
        flow = flow_to(URL, resp=True)
        flow.response.content = b'{"real": true}'
        self.addon.response(flow)
        self.assertEqual(flow.response.get_content(strict=False), b'{"faked": true}')

    def test_a_body_regex_rule_replaces_only_the_match(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.MODIFY_RESPONSE_BODY, target="real", replacement="faked"
            )
        )
        flow = flow_to(URL, resp=True)
        flow.response.content = b'{"real": true}'
        self.addon.response(flow)
        self.assertEqual(flow.response.get_content(strict=False), b'{"faked": true}')

    def test_a_request_body_rule_rewrites_the_request(self) -> None:
        self.push(kind_rule(RewriteKind.MODIFY_REQUEST_BODY, replacement="sent"))
        flow = flow_to(URL)
        flow.request.content = b"original"
        self.addon.request(flow)
        self.assertEqual(flow.request.get_content(strict=False), b"sent")

    def test_a_binary_body_is_skipped_with_a_debug_note(self) -> None:
        """二进制体跳过＋debug 日志，不静默也不炸钩子（§13 风险三）。"""
        self.push(kind_rule(RewriteKind.MODIFY_RESPONSE_BODY, replacement="R"))
        flow = flow_to(URL, resp=True)
        flow.response.content = bytes([0xFF, 0xFE, 0x00, 0x01])
        with self.assertLogs(level="DEBUG"):
            self.addon.response(flow)
        self.assertEqual(
            flow.response.get_content(strict=False), bytes([0xFF, 0xFE, 0x00, 0x01])
        )

    def test_the_body_content_length_is_recalculated(self) -> None:
        self.push(kind_rule(RewriteKind.MODIFY_RESPONSE_BODY, replacement="longer-body"))
        flow = flow_to(URL, resp=True)
        flow.response.content = b"x"
        self.addon.response(flow)
        self.assertEqual(flow.response.headers["Content-Length"], "11")

    # —— URL 重定向 ——

    def test_contains_rule_rewrites_only_the_matched_fragment(self) -> None:
        self.push(rule(RewriteLogic.CONTAINS, "api.example.com", "127.0.0.1:8000"))
        flow = flow_to("https://api.example.com/v1/user")
        self.addon.request(flow)
        self.assertEqual(flow.request.pretty_url, "https://127.0.0.1:8000/v1/user")
        self.assertEqual(flow.request.port, 8000)

    def test_equals_rule_replaces_the_whole_url_and_the_host_header(self) -> None:
        self.push(
            rule(
                RewriteLogic.EQUALS,
                "https://api.example.com/v1",
                "http://127.0.0.1:8000/v1",
            )
        )
        flow = flow_to("https://api.example.com/v1")
        flow.request.headers["Host"] = "api.example.com"
        self.addon.request(flow)
        self.assertEqual(flow.request.pretty_url, "http://127.0.0.1:8000/v1")
        # 原生 setter 连 Host 头和端口一起改，这也是不自造重定向的理由。
        self.assertEqual(flow.request.headers["Host"], "127.0.0.1:8000")
        self.assertEqual(flow.request.scheme, "http")

    def test_regex_rule_expands_backreferences(self) -> None:
        self.push(
            rule(
                RewriteLogic.REGEX,
                r"^https://api\.example\.com/(.*)",
                "http://127.0.0.1:8000/" + BACKSLASH + "1",
            )
        )
        flow = flow_to("https://api.example.com/v1/user")
        self.addon.request(flow)
        self.assertEqual(flow.request.pretty_url, "http://127.0.0.1:8000/v1/user")

    # —— 文件映射 ——

    def test_a_map_local_rule_answers_from_disk_without_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('{"local": true}')
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=path))
            flow = flow_to(URL)
            self.addon.request(flow)
        self.assertIsNotNone(flow.response)
        self.assertEqual(flow.response.get_content(strict=False), b'{"local": true}')
        self.assertEqual(flow.response.headers["Content-Type"], "application/json")
        self.assertTrue(flow.metadata[REWRITE_ANSWERED_KEY])

    def test_a_map_local_rule_guesses_the_content_type_from_the_extension(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("hello")
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=path))
            flow = flow_to(URL)
            self.addon.request(flow)
        self.assertEqual(flow.response.headers["Content-Type"], "text/plain")

    def test_a_map_local_rule_leaves_other_urls_alone(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=path))
            flow = flow_to("http://other.invalid/v1")
            self.addon.request(flow)
        self.assertIsNone(flow.response)

    def test_a_map_local_directory_rule_picks_the_file_by_url_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            sub = os.path.join(folder, "v1")
            os.mkdir(sub)
            with open(os.path.join(sub, "user.json"), "w", encoding="utf-8") as handle:
                handle.write('{"mapped": true}')
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=folder))
            flow = flow_to("https://api.example.com/v1/user.json")
            self.addon.request(flow)
        self.assertEqual(flow.response.get_content(strict=False), b'{"mapped": true}')

    def test_a_map_local_directory_rule_serves_index_html_for_a_bare_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as handle:
                handle.write("<h1>ok</h1>")
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=folder))
            flow = flow_to("https://api.example.com/")
            self.addon.request(flow)
        self.assertEqual(flow.response.get_content(strict=False), b"<h1>ok</h1>")

    def test_a_map_local_directory_rule_answers_404_when_nothing_exists(self) -> None:
        """对齐原生：目录候选一个都不在盘上时回 404，不放行去撞服务器。"""
        with tempfile.TemporaryDirectory() as folder:
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=folder))
            flow = flow_to("https://api.example.com/missing.json")
            self.addon.request(flow)
        self.assertEqual(flow.response.status_code, 404)

    def test_a_map_local_rule_never_escapes_the_directory(self) -> None:
        """URL 后缀是不可信输入，目录穿越守卫必须挡住 `..`。"""
        with tempfile.TemporaryDirectory() as folder:
            secret = Path(folder) / "secret.txt"
            secret.write_text("top secret", encoding="utf-8")
            public = Path(folder) / "public"
            public.mkdir()
            self.push(kind_rule(RewriteKind.MAP_LOCAL, replacement=str(public)))
            flow = flow_to("https://api.example.com/../secret.txt")
            self.addon.request(flow)
        # 候选被守卫判成不安全（空表）或不存在 → 404 / 不作答，总之读不到文件。
        self.assertTrue(
            flow.response is None or flow.response.status_code == 404
        )

    def test_a_map_local_rule_does_not_override_an_existing_response(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "payload.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
            self.push(
                kind_rule(RewriteKind.REPLACE_RESPONSE, status_code=418),
                kind_rule(RewriteKind.MAP_LOCAL, replacement=path),
            )
            flow = flow_to(URL)
            self.addon.request(flow)
        self.assertEqual(flow.response.status_code, 418)

    # —— 替换请求 ——

    def test_a_replace_request_rule_overrides_each_filled_field(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.REPLACE_REQUEST,
                method="post",
                path="/v1/login",
                headers=(("X-A", "1"),),
                replacement="payload",
            )
        )
        flow = flow_to(URL)
        self.addon.request(flow)
        # mitmproxy 的 method setter 原生就会大写化 —— 这正是只校验「不能含空白」
        # 就够了的理由（见 RewriteRule._validate_replace_request）。
        self.assertEqual(flow.request.method, "POST")
        self.assertEqual(flow.request.path, "/v1/login")
        self.assertEqual(flow.request.headers["X-A"], "1")
        self.assertEqual(flow.request.get_content(strict=False), b"payload")
        self.assertEqual(flow.request.headers["Content-Length"], "7")

    def test_a_replace_request_rule_keeps_the_blank_fields(self) -> None:
        """留空的栏保持原样（逐项覆盖，不是整条重造）。"""
        self.push(kind_rule(RewriteKind.REPLACE_REQUEST, method="PUT"))
        flow = flow_to(URL)
        flow.request.headers["X-Keep"] = "yes"
        self.addon.request(flow)
        self.assertEqual(flow.request.method, "PUT")
        self.assertEqual(flow.request.path, "/v1")
        self.assertEqual(flow.request.headers["X-Keep"], "yes")

    def test_an_at_prefixed_replace_body_is_read_from_a_file_per_request(self) -> None:
        """现读：第一次读到的内容改掉之后，第二条流量拿到的是新内容。"""
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("v1")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self.push(
            kind_rule(
                RewriteKind.REPLACE_REQUEST,
                replacement=FILE_REPLACEMENT_PREFIX + path,
            )
        )
        first = flow_to(URL)
        self.addon.request(first)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("v2")
        second = flow_to(URL)
        self.addon.request(second)
        self.assertEqual(first.request.get_content(strict=False), b"v1")
        self.assertEqual(second.request.get_content(strict=False), b"v2")

    # —— 替换响应 ——

    def test_a_replace_response_rule_answers_without_the_server(self) -> None:
        self.push(
            kind_rule(
                RewriteKind.REPLACE_RESPONSE,
                status_code=418,
                headers=(("X-Fake", "yes"),),
                replacement='{"faked": true}',
            )
        )
        flow = flow_to(URL)
        self.addon.request(flow)
        self.assertEqual(flow.response.status_code, 418)
        self.assertEqual(flow.response.headers["X-Fake"], "yes")
        self.assertEqual(flow.response.get_content(strict=False), b'{"faked": true}')
        self.assertTrue(flow.metadata[REWRITE_ANSWERED_KEY])

    def test_a_replace_response_rule_defaults_to_200(self) -> None:
        """状态码留空 = 执行期取 200（§5 契约）。"""
        self.push(kind_rule(RewriteKind.REPLACE_RESPONSE, replacement="ok"))
        flow = flow_to(URL)
        self.addon.request(flow)
        self.assertEqual(flow.response.status_code, REPLACE_RESPONSE_DEFAULT_STATUS)
        self.assertEqual(flow.response.get_content(strict=False), b"ok")

    def test_a_replace_response_rule_needs_at_least_one_field(self) -> None:
        """全空的替换响应本来就编译不进去（filled 为假，整条跳过）。"""
        self.push(kind_rule(RewriteKind.REPLACE_RESPONSE))
        flow = flow_to(URL)
        self.addon.request(flow)
        self.assertIsNone(flow.response)


if __name__ == "__main__":
    unittest.main()
