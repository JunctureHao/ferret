"""Tests for the http2/http3 protocol switches (.plans/2-protocol-switches.md).

错误模型照 `test_anticache.py`（同为 bool 选项）：传错类型 optmanager 抛 TypeError，
不经 OptionsError —— `apply_protocol_options` 的回滚分支据此写成「先回滚再原样抛」。
与 anticache 唯一的结构差异在注册时机：``http2`` / ``http3`` 是 Options 构造期就
注册的核心选项，不是 addon.load 才注册。
"""

import asyncio
import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    PROTOCOL_OPTIONS,
    MitmRuntime,
    MitmRuntimeState,
    protocol_option_updates,
)
from ferret.core.mitm.master import FerretMaster

from ._qt import start_runtime


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ProtocolOptionUpdatesTests(unittest.TestCase):
    def test_every_option_is_always_present(self) -> None:
        for http2 in (True, False):
            for http3 in (True, False):
                with self.subTest(http2=http2, http3=http3):
                    self.assertEqual(
                        sorted(protocol_option_updates(http2, http3)),
                        sorted(PROTOCOL_OPTIONS),
                    )

    def test_values_pass_through_independently(self) -> None:
        """两个开关相互独立（与 anticache 的「同开同关」相反）。"""
        self.assertEqual(
            protocol_option_updates(False, True), {"http2": False, "http3": True}
        )


class FerretMasterProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_options_exist_before_any_addon_is_added(self) -> None:
        """核心选项：构造 Options 时就注册（与 anticache 那批 addon.load 注册相反）。"""
        from ferret.core.mitm.bindings import Options

        self.assertIn("http2", Options().keys())
        self.assertIn("http3", Options().keys())

    def test_defaults_are_on(self) -> None:
        """默认开：原生出厂姿态就是全支持，关掉是显式的调试降级。"""
        self.assertTrue(self.master.options.http2)
        self.assertTrue(self.master.options.http3)

    def test_switches_can_be_updated(self) -> None:
        self.master.options.update(**protocol_option_updates(False, True))
        self.assertFalse(self.master.options.http2)
        self.assertTrue(self.master.options.http3)

    def test_wrong_type_raises_typeerror_not_optionserror(self) -> None:
        """两个都是 bool 选项，optmanager 的类型检查抛 TypeError，不走 OptionsError。"""
        with self.assertRaises(TypeError):
            self.master.options.update(http2="yes")
        self.assertTrue(self.master.options.http2)


class MitmRuntimeProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_is_on(self) -> None:
        runtime = MitmRuntime()
        self.assertTrue(runtime.http2_enabled)
        self.assertTrue(runtime.http3_enabled)

    def test_stored_before_start_and_seeded_into_the_master(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(), http2_enabled=False, http3_enabled=False
        )
        self.addCleanup(runtime.stop)

        start_runtime(runtime)
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)

        master = runtime._master
        assert master is not None
        # 内核启动前就该带上开关，第一条连接的协议协商必须已经生效。
        self.assertFalse(runtime.call(lambda: master.options.http2))
        self.assertFalse(runtime.call(lambda: master.options.http3))

    def test_hot_update_reaches_the_running_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        start_runtime(runtime)

        runtime.apply_protocol_options(http3=False)

        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.options.http2))
        self.assertFalse(runtime.call(lambda: master.options.http3))

    def test_none_leaves_the_item_untouched(self) -> None:
        """None = 不改动该项：只传 http3 时 http2 保持原值。"""
        runtime = MitmRuntime(listen_port=free_port(), http2_enabled=False)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)

        runtime.apply_protocol_options(http3=False)

        self.assertFalse(runtime.http2_enabled)
        self.assertFalse(runtime.http3_enabled)

    def test_turning_back_on_writes_true_not_none(self) -> None:
        """bool 选项关掉写 False、开回去写 True（出厂默认），不是 None 路径。"""
        runtime = MitmRuntime(listen_port=free_port(), http2_enabled=False)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)

        runtime.apply_protocol_options(http2=True)

        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.options.http2))

    def test_a_rejected_push_rolls_back_the_stored_copy(self) -> None:
        """内核没收到就不能留下「已生效」的内存状态，否则界面会说谎。"""
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        start_runtime(runtime)

        with self.assertRaises(TypeError):
            runtime.apply_protocol_options(http2="yes")  # ty: ignore[invalid-argument-type]

        self.assertTrue(runtime.http2_enabled)
        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.options.http2))

    def test_stored_only_while_stopped(self) -> None:
        """内核没跑时只存不下发，下次启动由 _run_master 补上。"""
        runtime = MitmRuntime()
        runtime.apply_protocol_options(http2=False, http3=False)
        self.assertFalse(runtime.http2_enabled)
        self.assertFalse(runtime.http3_enabled)
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)


if __name__ == "__main__":
    unittest.main()
