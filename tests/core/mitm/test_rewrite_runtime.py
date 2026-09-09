"""Tests for the rewrite wiring in MitmRuntime and MitmFacade (网关规则模式)."""

import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    MitmFacade,
    MitmRuntime,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
    RewriteRuleSet,
)
from ferret.core.mitm.addons import FerretRewriteAddon


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def good() -> RewriteRule:
    return RewriteRule(
        kind=RewriteKind.REPLACE_REQUEST, value="a.com", method="POST"
    )


def broken() -> RewriteRule:
    # CONTAINS 会把括号转义成合法正则，必须用 REGEX 才真的坏。
    return RewriteRule(
        kind=RewriteKind.MAP_REMOTE,
        logic=RewriteLogic.REGEX,
        value="bad(",
        replacement="http://b.com/",
    )


class FakeMaster:
    def __init__(self) -> None:
        self.rewrite = FerretRewriteAddon()


class MitmRuntimeRewriteTests(unittest.TestCase):
    """Everything `apply_rewrite_rules` guarantees without a kernel running."""

    @classmethod
    def setUpClass(cls) -> None:
        QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = MitmRuntime(listen_port=free_port())

    def test_defaults_are_empty_rules_and_a_live_switch(self) -> None:
        self.assertEqual(self.runtime.rewrite_rules, [])
        self.assertTrue(self.runtime.rewrite_enabled)

    def test_rules_are_stored_as_a_copy(self) -> None:
        rules = [good()]
        self.runtime.apply_rewrite_rules(rules)
        rules.append(broken())
        self.assertEqual(len(self.runtime.rewrite_rules), 1)

    def test_flipping_the_switch_alone_keeps_the_rules(self) -> None:
        """总开关不碰各行规则的落盘值（§4.4：临时下发，不是删规则）。"""
        self.runtime.apply_rewrite_rules([good()])
        self.runtime.apply_rewrite_rules(enabled=False)
        self.assertEqual(len(self.runtime.rewrite_rules), 1)
        self.assertFalse(self.runtime.rewrite_enabled)
        self.assertTrue(self.runtime.rewrite_rules[0].enabled)

    def test_a_broken_rule_rolls_back_rules_and_switch_together(self) -> None:
        """快照在提交任何东西之前编译：坏规则整批拒收，内存副本原样。"""
        self.runtime.apply_rewrite_rules([good()])
        before = list(self.runtime.rewrite_rules)

        with self.assertRaises(ValueError):
            self.runtime.apply_rewrite_rules([broken()], enabled=False)

        self.assertEqual(self.runtime.rewrite_rules, before)
        self.assertTrue(self.runtime.rewrite_enabled)

    def test_disabled_broken_rules_do_not_block_the_batch(self) -> None:
        """坏规则被停用后必须还能下发 —— 控制器就是这么保住用户数据的。"""
        from dataclasses import replace

        self.runtime.apply_rewrite_rules([replace(broken(), enabled=False)])
        self.assertEqual(len(self.runtime.rewrite_rules), 1)

    def test_seeding_a_stopped_runtime_keeps_the_copy(self) -> None:
        """内核没跑只存副本：下次启动 `_apply_rewrite_rules` 会补推。"""
        self.runtime.apply_rewrite_rules([good()], enabled=False)
        self.assertEqual(len(self.runtime.rewrite_rules), 1)
        self.assertFalse(self.runtime.rewrite_enabled)


class MitmFacadeRewriteTests(unittest.TestCase):
    """`MitmFacade.set_rewrite_rules` 签名不变；经 call 换的是预编译快照。"""

    @classmethod
    def setUpClass(cls) -> None:
        QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = MitmRuntime(listen_port=free_port())
        # 与 test_gateway_runtime 同一姿势：替身 master + 同步 call 替身。
        self.runtime._master = FakeMaster()  # ty: ignore[invalid-assignment]
        self.runtime._state = self.runtime._state.RUNNING
        # `call` 需要 mitm 线程的 event loop；这里没有线程，直接同步执行。
        self.runtime.call = lambda callback, *, timeout=5.0: callback()  # ty: ignore[invalid-assignment]
        self.facade = MitmFacade(self.runtime)
        self.state = self.runtime._master.rewrite  # ty: ignore[unresolved-attribute]

    def test_set_rewrite_rules_swaps_the_compiled_snapshot(self) -> None:
        self.facade.set_rewrite_rules([good()])
        self.assertEqual(len(self.state._rules), 1)
        self.assertIsInstance(self.state._rules, RewriteRuleSet)

    def test_a_bad_rule_is_rejected_before_touching_the_addon(self) -> None:
        self.facade.set_rewrite_rules([good()])
        with self.assertRaises(ValueError):
            self.facade.set_rewrite_rules([broken()])
        self.assertEqual(len(self.state._rules), 1)

    def test_set_rewrite_enabled_flips_only_the_switch(self) -> None:
        self.facade.set_rewrite_rules([good()])
        self.facade.set_rewrite_enabled(False)
        self.assertFalse(self.facade.rewrite_enabled)
        self.assertEqual(len(self.state._rules), 1)
        self.assertFalse(self.state._enabled)
        self.facade.set_rewrite_enabled(True)
        self.assertTrue(self.state._enabled)


if __name__ == "__main__":
    unittest.main()
