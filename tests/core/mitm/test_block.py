"""Tests for the native Block addon's wiring into FerretMaster and MitmRuntime."""

import asyncio
import ipaddress
import socket
import unittest

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from ferret.core.mitm import MitmRuntime, MitmRuntimeState
from ferret.core.mitm.bindings import Block, Core, StripDnsHttpsRecords
from ferret.core.mitm.master import FerretMaster


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class BlockCategoryTests(unittest.TestCase):
    """Block 只读 stdlib 的三个类别属性，没有任何名单 —— 先把类别边界钉住。"""

    def test_public_addresses_are_global(self) -> None:
        for host in ("47.85.36.205", "8.8.8.8", "2408:8000::1"):
            with self.subTest(host=host):
                address = ipaddress.ip_address(host)
                self.assertTrue(address.is_global)
                self.assertFalse(address.is_private)

    def test_lan_addresses_are_private(self) -> None:
        for host in ("192.168.1.5", "10.0.0.7", "172.16.0.1", "fd00::1"):
            with self.subTest(host=host):
                address = ipaddress.ip_address(host)
                self.assertTrue(address.is_private)
                self.assertFalse(address.is_global)

    def test_loopback_is_private_and_exempt(self) -> None:
        for host in ("127.0.0.1", "::1"):
            with self.subTest(host=host):
                address = ipaddress.ip_address(host)
                self.assertTrue(address.is_loopback)
                self.assertTrue(address.is_private)

    def test_cgnat_is_neither_global_nor_private(self) -> None:
        """RFC 6598 落在两个类别之外，两个开关全开也拦不住 —— 记录这个事实。

        对 ferret 无影响：「只有本机」是绑定环回实现的，这类来源根本到不了 socket。
        """
        address = ipaddress.ip_address("100.64.0.1")
        self.assertFalse(address.is_global)
        self.assertFalse(address.is_private)


class FerretMasterBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_master_holds_a_block_instance(self) -> None:
        self.assertIsInstance(self.master.addons.get("block"), Block)

    def test_block_runs_between_core_and_strip_dns(self) -> None:
        """对齐原生 default_addons()：必须早于任何流量成形。"""
        names = [type(addon).__name__ for addon in self.master.addons.chain]
        self.assertEqual(names.index(Core.__name__) + 1, names.index(Block.__name__))
        self.assertEqual(
            names.index(Block.__name__) + 1,
            names.index(StripDnsHttpsRecords.__name__),
        )

    def test_options_are_registered_only_after_the_addon_is_added(self) -> None:
        """所以只能在 Master 建好之后 options.update，不能在构造 Options 时传。"""
        from ferret.core.mitm.bindings import Options

        self.assertNotIn("block_global", Options().keys())
        self.assertIn("block_global", self.master.options)
        self.assertIn("block_private", self.master.options)

    def test_defaults_match_mitmproxy_factory_posture(self) -> None:
        self.assertTrue(self.master.options.block_global)
        self.assertFalse(self.master.options.block_private)

    def test_both_switches_can_be_updated(self) -> None:
        self.master.options.update(block_global=False, block_private=True)
        self.assertFalse(self.master.options.block_global)
        self.assertTrue(self.master.options.block_private)

    def test_wrong_type_raises_typeerror_not_optionserror(self) -> None:
        """两个都是 bool 选项，optmanager 的类型检查抛 TypeError，不走 OptionsError。

        apply_block_options 的回滚分支据此写成「先回滚再原样抛」——只 catch
        OptionsError 的话那段代码永远不会执行，等于没有回滚。
        """
        with self.assertRaises(TypeError):
            self.master.options.update(block_global="yes")
        self.assertTrue(self.master.options.block_global)

    def test_unknown_option_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            self.master.options.update(block_nonsense=True)
        self.assertTrue(self.master.options.block_global)


class MitmRuntimeBlockOptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait_for_signal(self, signal, timeout_ms: int = 5000):
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

    def test_defaults_are_the_mitmproxy_posture(self) -> None:
        runtime = MitmRuntime()
        self.assertTrue(runtime.block_global)
        self.assertFalse(runtime.block_private)

    def test_stored_before_start_and_seeded_into_the_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port(), block_private=True)
        self.addCleanup(runtime.stop)
        runtime.apply_block_options(block_global=False)
        self.assertFalse(runtime.block_global)

        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)

        master = runtime._master
        assert master is not None
        # 内核启动前就该带上开关，第一条连接进来时判定必须已经是对的。
        self.assertFalse(runtime.call(lambda: master.options.block_global))
        self.assertTrue(runtime.call(lambda: master.options.block_private))

    def test_hot_update_reaches_the_running_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))

        runtime.apply_block_options(block_global=False, block_private=True)

        master = runtime._master
        assert master is not None
        self.assertFalse(runtime.call(lambda: master.options.block_global))
        self.assertTrue(runtime.call(lambda: master.options.block_private))

    def test_restart_can_rebind_the_listen_host(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))

        runtime.restart(listen_host="0.0.0.0", listen_port=free_port())
        self.assertTrue(self.wait_for_signal(runtime.ready))
        self.assertEqual(runtime.listen_host, "0.0.0.0")

        master = runtime._master
        assert master is not None
        self.assertEqual(runtime.call(lambda: master.options.listen_host), "0.0.0.0")
        # 重启后开关必须跟着新 Master 一起收敛，不能停在默认值。
        self.assertTrue(runtime.call(lambda: master.options.block_global))

    def test_illegal_listen_host_is_corrected_instead_of_raising(self) -> None:
        runtime = MitmRuntime(listen_host="10.1.2.3")
        self.assertEqual(runtime.listen_host, "127.0.0.1")

    def test_a_rejected_push_rolls_back_the_stored_copy(self) -> None:
        """内核没收到就不能留下「已生效」的内存状态，否则界面会说谎。"""
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))

        with self.assertRaises(TypeError):
            runtime.apply_block_options(block_global="yes")  # type: ignore

        self.assertTrue(runtime.block_global)
        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.options.block_global))

    def test_stored_only_while_stopped(self) -> None:
        """内核没跑时只存不下发，下次启动由 _run_master 补上。"""
        runtime = MitmRuntime()
        runtime.apply_block_options(block_global=False, block_private=True)
        self.assertFalse(runtime.block_global)
        self.assertTrue(runtime.block_private)
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)


if __name__ == "__main__":
    unittest.main()
