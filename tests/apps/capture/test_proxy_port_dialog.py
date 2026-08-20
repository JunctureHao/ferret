"""Tests for the proxy listen-settings dialog.

这里守住的是「三个地址不能混用」这条设计线（见 core/network.py 的模块注释）：
绑定地址可切，本机接入地址恒为环回，局域网地址只用来显示和复制。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.capture.views import ProxyPortDialog
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, PORT_MAX, PORT_MIN


class ProxyPortDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # MessageBoxBase 会读 parent.width()，没有真实父组件会 AttributeError。
        self.host = QWidget()
        self.host.resize(900, 600)
        # 真的 show 出来（离屏平台）：isVisible() 要求整条祖先链都可见，
        # 否则每个 setVisible(True) 都会被判成 False。
        self.host.show()
        self.app.processEvents()
        self.addCleanup(self._destroy_host)

    def _destroy_host(self) -> None:
        self.host.close()
        self.host.deleteLater()
        self.app.processEvents()

    def dialog(self, **overrides) -> ProxyPortDialog:
        values = {
            "current_port": 8080,
            "parent": self.host,
            "is_running": False,
            "listen_host": LOOPBACK_HOST,
            "block_global": True,
            "block_private": False,
            "lan_address": "192.168.1.9",
        }
        values.update(overrides)
        dlg = ProxyPortDialog(**values)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_getters_round_trip_the_incoming_values(self) -> None:
        dlg = self.dialog(
            current_port=9090,
            listen_host=ANY_HOST,
            block_global=False,
            block_private=True,
        )
        self.assertEqual(dlg.get_port(), 9090)
        self.assertEqual(dlg.get_listen_host(), ANY_HOST)
        self.assertFalse(dlg.get_block_global())
        self.assertTrue(dlg.get_block_private())

    def test_port_range_comes_from_core_network(self) -> None:
        """对话框和配置收敛必须用同一套边界，否则用户能填出内核不接受的端口。"""
        dlg = self.dialog()
        self.assertEqual(dlg.port_spin.minimum(), PORT_MIN)
        self.assertEqual(dlg.port_spin.maximum(), PORT_MAX)

    def test_unknown_listen_host_selects_loopback(self) -> None:
        """配置被手改成别的地址时，对话框要落在更安全的那个选项上。"""
        dlg = self.dialog(listen_host="10.1.2.3")
        self.assertEqual(dlg.host_combo.currentIndex(), 0)
        self.assertEqual(dlg.get_listen_host(), LOOPBACK_HOST)

    def test_local_hint_always_names_loopback(self) -> None:
        """整个改动的核心：放开监听不改变本机接入路径，文案必须这么说。"""
        for listen_host in (LOOPBACK_HOST, ANY_HOST):
            with self.subTest(listen_host=listen_host):
                dlg = self.dialog(listen_host=listen_host)
                self.assertIn(f"{LOOPBACK_HOST}:8080", dlg.local_hint.text())

    def test_loopback_hides_the_lan_row(self) -> None:
        dlg = self.dialog(listen_host=LOOPBACK_HOST)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.lan_label.isVisible())
        self.assertFalse(dlg.lan_value.isVisible())
        self.assertFalse(dlg.lan_copy_btn.isVisible())

    def test_switching_to_any_host_reveals_the_lan_address(self) -> None:
        dlg = self.dialog(listen_host=LOOPBACK_HOST, current_port=8899)
        dlg.show()
        self.app.processEvents()

        dlg.host_combo.setCurrentIndex(1)
        self.app.processEvents()

        self.assertEqual(dlg.get_listen_host(), ANY_HOST)
        self.assertTrue(dlg.lan_value.isVisible())
        self.assertEqual(dlg.lan_value.text(), "192.168.1.9:8899")
        self.assertTrue(dlg.lan_copy_btn.isEnabled())

    def test_lan_address_follows_the_port_spin(self) -> None:
        dlg = self.dialog(listen_host=ANY_HOST)
        dlg.port_spin.setValue(9100)
        self.assertEqual(dlg.lan_value.text(), "192.168.1.9:9100")
        self.assertIn(f"{LOOPBACK_HOST}:9100", dlg.local_hint.text())

    def test_failed_probe_says_unknown_instead_of_guessing(self) -> None:
        """多网卡 / VPN 下探测会失败；不能显示一个连不上的地址让用户白试。"""
        dlg = self.dialog(listen_host=ANY_HOST, lan_address=None)
        self.assertNotIn("192.168", dlg.lan_value.text())
        self.assertFalse(dlg.lan_copy_btn.isEnabled())

    def test_source_switches_are_greyed_but_keep_their_state_on_loopback(self) -> None:
        """置灰不等于清空：切回局域网时用户的偏好还得在。"""
        dlg = self.dialog(
            listen_host=LOOPBACK_HOST, block_global=True, block_private=True
        )
        self.assertFalse(dlg.block_global_check.isEnabled())
        self.assertFalse(dlg.block_private_check.isEnabled())
        self.assertTrue(dlg.get_block_global())
        self.assertTrue(dlg.get_block_private())

        dlg.host_combo.setCurrentIndex(1)
        self.assertTrue(dlg.block_global_check.isEnabled())
        self.assertTrue(dlg.block_private_check.isEnabled())
        self.assertTrue(dlg.get_block_global())
        self.assertTrue(dlg.get_block_private())

    def test_ineffective_hint_shows_only_on_loopback(self) -> None:
        dlg = self.dialog(listen_host=LOOPBACK_HOST)
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.source_hint.isVisible())
        self.assertTrue(dlg.source_hint.text())

        dlg.host_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertFalse(dlg.source_hint.isVisible())

    def test_restart_hint_only_when_the_kernel_is_running(self) -> None:
        dlg = self.dialog(is_running=False)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.restart_hint.isVisible())

        running = self.dialog(is_running=True)
        running.show()
        self.app.processEvents()
        self.assertTrue(running.restart_hint.isVisible())

    def test_copy_puts_host_and_port_on_the_clipboard(self) -> None:
        dlg = self.dialog(listen_host=ANY_HOST, current_port=8123)
        dlg.lan_copy_btn.click()
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.skipTest("离屏平台没有剪贴板")
        self.assertEqual(clipboard.text(), "192.168.1.9:8123")

    def test_copy_is_a_no_op_when_the_address_is_unknown(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.skipTest("离屏平台没有剪贴板")
        clipboard.setText("untouched")
        dlg = self.dialog(listen_host=ANY_HOST, lan_address=None)
        dlg.lan_copy_btn.click()
        self.assertEqual(clipboard.text(), "untouched")


if __name__ == "__main__":
    unittest.main()
