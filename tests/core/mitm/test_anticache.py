"""Tests for the anticache/anticomp synthesized switch (.plans/capture-preferences-page.md).

错误模型照 `test_block.py`（同为 bool 选项）：传错类型 optmanager 抛 TypeError，
不经 OptionsError —— `apply_anticache_plaintext` 的回滚分支据此写成「先回滚再
原样抛」。
"""

import asyncio
import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from ferret.core.mitm import (
    ANTICACHE_OPTIONS,
    MitmRuntime,
    MitmRuntimeState,
    anticache_option_updates,
)
from ferret.core.mitm.bindings import AntiCache, AntiComp
from ferret.core.mitm.master import FerretMaster


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class AnticacheOptionUpdatesTests(unittest.TestCase):
    def test_every_option_is_always_present(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertEqual(
                    sorted(anticache_option_updates(enabled)),
                    sorted(ANTICACHE_OPTIONS),
                )

    def test_both_options_flip_together(self) -> None:
        """合成一个开关：两个 option 同开同关，不存在只开其一的取值。"""
        self.assertEqual(
            anticache_option_updates(True), {"anticache": True, "anticomp": True}
        )
        self.assertEqual(
            anticache_option_updates(False), {"anticache": False, "anticomp": False}
        )


class FerretMasterAnticacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_master_holds_both_addons(self) -> None:
        self.assertIsInstance(self.master.addons.get("anticache"), AntiCache)
        self.assertIsInstance(self.master.addons.get("anticomp"), AntiComp)

    def test_options_are_registered_only_after_the_addon_is_added(self) -> None:
        """所以只能在 Master 建好之后 options.update，不能在构造 Options 时传。"""
        from ferret.core.mitm.bindings import Options

        self.assertNotIn("anticache", Options().keys())
        self.assertIn("anticache", self.master.options)
        self.assertIn("anticomp", self.master.options)

    def test_defaults_are_off(self) -> None:
        """默认关：开着会改写请求头，与「抓包应如实转发原件」冲突。"""
        self.assertFalse(self.master.options.anticache)
        self.assertFalse(self.master.options.anticomp)

    def test_both_switches_can_be_updated(self) -> None:
        self.master.options.update(**anticache_option_updates(True))
        self.assertTrue(self.master.options.anticache)
        self.assertTrue(self.master.options.anticomp)

    def test_wrong_type_raises_typeerror_not_optionserror(self) -> None:
        """两个都是 bool 选项，optmanager 的类型检查抛 TypeError，不走 OptionsError。"""
        with self.assertRaises(TypeError):
            self.master.options.update(anticache="yes")
        self.assertFalse(self.master.options.anticache)


class MitmRuntimeAnticacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait_for_signal(self, signal, timeout_ms: int = 30000):
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

    def test_default_is_off(self) -> None:
        self.assertFalse(MitmRuntime().anticache_plaintext)

    def test_stored_before_start_and_seeded_into_the_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port(), anticache_plaintext=True)
        self.addCleanup(runtime.stop)

        runtime.start()
        self.assertTrue(
            self.wait_for_signal(runtime.ready),
            f"runtime 未就绪: state={runtime.state}, last_error={runtime._last_error}",
        )
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)

        master = runtime._master
        assert master is not None
        # 内核启动前就该带上开关，第一条连接进来时请求头改写必须已经生效。
        self.assertTrue(runtime.call(lambda: master.options.anticache))
        self.assertTrue(runtime.call(lambda: master.options.anticomp))

    def test_hot_update_reaches_the_running_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(
            self.wait_for_signal(runtime.ready),
            f"runtime 未就绪: state={runtime.state}, last_error={runtime._last_error}",
        )

        runtime.apply_anticache_plaintext(True)

        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.options.anticache))
        self.assertTrue(runtime.call(lambda: master.options.anticomp))

    def test_turning_off_writes_false_not_none(self) -> None:
        """bool 选项关掉写 False（出厂默认），不是 sticky 过滤串那条 None 路径。"""
        runtime = MitmRuntime(listen_port=free_port(), anticache_plaintext=True)
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(
            self.wait_for_signal(runtime.ready),
            f"runtime 未就绪: state={runtime.state}, last_error={runtime._last_error}",
        )

        runtime.apply_anticache_plaintext(False)

        master = runtime._master
        assert master is not None
        self.assertFalse(runtime.call(lambda: master.options.anticache))
        self.assertFalse(runtime.call(lambda: master.options.anticomp))

    def test_a_rejected_push_rolls_back_the_stored_copy(self) -> None:
        """内核没收到就不能留下「已生效」的内存状态，否则界面会说谎。"""
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(
            self.wait_for_signal(runtime.ready),
            f"runtime 未就绪: state={runtime.state}, last_error={runtime._last_error}",
        )

        with self.assertRaises(TypeError):
            runtime.apply_anticache_plaintext("yes")  # type: ignore

        self.assertFalse(runtime.anticache_plaintext)
        master = runtime._master
        assert master is not None
        self.assertFalse(runtime.call(lambda: master.options.anticache))

    def test_stored_only_while_stopped(self) -> None:
        """内核没跑时只存不下发，下次启动由 _run_master 补上。"""
        runtime = MitmRuntime()
        runtime.apply_anticache_plaintext(True)
        self.assertTrue(runtime.anticache_plaintext)
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)


if __name__ == "__main__":
    unittest.main()
