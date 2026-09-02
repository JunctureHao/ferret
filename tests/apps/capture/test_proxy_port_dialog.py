"""Tests for the proxy listen-settings dialog.

这里守住的是「三个地址不能混用」这条设计线（见 core/network.py 的模块注释）：
绑定地址可切，本机接入地址恒为环回，局域网地址只用来显示和复制。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.capture.views import (
    LocalSpecSelector,
    ProxyPortDialog,
    WireGuardConfigDialog,
)
from ferret.core.mitm.modes import LocalTarget
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, PORT_MAX, PORT_MIN


def _target(name: str) -> LocalTarget:
    return LocalTarget(display_name=name, executable=rf"C:\app\{name.lower()}.exe", icon_png=None)


class LocalSpecSelectorTests(unittest.TestCase):
    """本地重定向进程选择卡片：tokens 单一来源、勾选契约、通道显隐。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.selector = LocalSpecSelector()
        # 构造时枚举的是真实进程；注入测试桩后重建候选条目。
        self.selector._targets = [_target("Chrome"), _target("钉钉")]
        self.selector.set_items_for_testing()
        self.selector.resize(360, self.selector.height())
        self.addCleanup(self.selector.deleteLater)
        self.changes: list[list[str]] = []
        self.selector.tokensChanged.connect(self.changes.append)

    def _items(self) -> list:
        selector = self.selector
        return [selector._list.item(row) for row in range(selector._list.count())]

    def _find(self, text: str):
        for item in self._items():
            if item.text() == text:
                return item
        return None

    def test_set_tokens_checks_candidates_and_adds_manual(self) -> None:
        """初始 tokens 回显：对上候选的勾选，对不上的手输 token 也成一条。"""
        self.selector.set_tokens(["Chrome", "!123"])
        labels = {item.text() for item in self._items()}
        self.assertIn("Chrome", labels)
        self.assertIn("!123", labels)
        self.assertEqual(self.selector.tokens(), ["Chrome", "!123"])
        self.assertEqual(self.changes[-1], ["Chrome", "!123"])

    def test_toggle_emits_tokens(self) -> None:
        self.selector.set_tokens([])
        changes: list[list[str]] = []
        self.selector._list.itemChanged.connect(lambda _i: changes.append(self.selector.tokens()))

        item = self._find("Chrome")
        item.setCheckState(Qt.CheckState.Checked)

        self.assertEqual(changes[-1], ["Chrome"])

    def test_manual_commit_adds_and_checks_token(self) -> None:
        self.selector.set_tokens([])
        self.selector._add_edit.setText("!4321")
        self.selector._commit_manual()

        item = self._find("!4321")
        self.assertIsNotNone(item)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)
        self.assertEqual(self.selector.tokens(), ["!4321"])

    def test_manual_commit_checks_matching_candidate(self) -> None:
        """手输「钉」这种子串也应点亮候选（与内核 contains 语义一致）。"""
        self.selector.set_tokens([])
        self.selector._add_edit.setText("钉")
        self.selector._commit_manual()
        self.assertEqual(self.selector.tokens(), ["钉钉"])

    def test_search_filters_rows_but_keeps_checked(self) -> None:
        """搜索只是视图过滤：隐藏行的勾选不丢，tokens 仍包含它。"""
        self.selector.set_tokens(["Chrome", "钉钉"])
        self.selector._search.setText("chrome")
        chrome = self._find("Chrome")
        ding = self._find("钉钉")
        self.assertTrue(chrome is not None and not chrome.isHidden())
        self.assertTrue(ding is not None and ding.isHidden())
        self.assertEqual(self.selector.tokens(), ["Chrome", "钉钉"])

    def test_items_have_no_native_check_indicator(self) -> None:
        """原生勾选指示器必须清掉——否则与 delegate 自绘的青勾左右叠画。"""
        self.selector.set_tokens(["curl"])
        for item in self._items():
            self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_collapsed_by_default_and_summary_shows_empty_hint(self) -> None:
        """默认收起；空态摘要提示「留空截获全部」。"""
        self.assertFalse(self.selector.is_expanded())
        self.assertFalse(self.selector._body.isVisible())
        self.assertIn("capture every process", self.selector._summary.text())

    def test_toggle_expands_panel_and_updates_summary(self) -> None:
        """点按钮展开面板（搜索/列表/手输行可见）；勾选后摘要显示已选清单。"""
        self.selector.show()
        self.app.processEvents()
        self.addCleanup(self.selector.hide)

        self.selector.toggle_expanded()
        self.assertTrue(self.selector.is_expanded())
        self.assertTrue(self.selector._search.isVisible())

        self.selector.set_tokens(["Chrome"])
        self.assertIn("Chrome", self.selector._summary.text())
        self.assertIn("Selected", self.selector._summary.text())

        self.selector.toggle_expanded()
        self.assertFalse(self.selector.is_expanded())
        # 收起不丢勾选：tokens 与摘要仍在。
        self.assertEqual(self.selector.tokens(), ["Chrome"])
        self.assertIn("Chrome", self.selector._summary.text())


class WireGuardConfigDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(900, 600)
        self.host.show()
        self.app.processEvents()
        self.addCleanup(self._destroy_host)

    def _destroy_host(self) -> None:
        self.host.close()
        self.host.deleteLater()
        self.app.processEvents()

    def test_config_with_qr_renders_both_views(self) -> None:
        """合法配置 → QR 位图 + 文本同框；文本框保留手动复制退路。"""
        dlg = WireGuardConfigDialog("[Interface]\nPrivateKey = abc\n", self.host)
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(dlg.qr_label.pixmap() is not None)
        self.assertGreater(dlg.qr_label.pixmap().width(), 0)
        self.assertEqual(dlg.config_edit.toPlainText(), "[Interface]\nPrivateKey = abc\n")

    def test_copy_puts_the_config_on_the_clipboard(self) -> None:
        dlg = WireGuardConfigDialog("profile text", self.host)
        self.addCleanup(dlg.deleteLater)
        dlg.yesButton.click()
        clipboard = QApplication.clipboard()
        if clipboard is None:
            self.skipTest("离屏平台没有剪贴板")
        self.assertEqual(clipboard.text(), "profile text")


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
            "use_system_proxy": True,
            "use_local": True,
            "local_spec": "",
            "use_wireguard": True,
            "wireguard_config": lambda: "[Interface]",
        }
        values.update(overrides)
        dlg = ProxyPortDialog(**values)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_process_card_follows_the_local_channel_toggle(self) -> None:
        """进程选择卡片内嵌显示：勾上本地重定向才可见，取消即隐藏。"""
        dlg = self.dialog(use_local=True)
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.local_spec_edit.isVisible())

        dlg.local_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dlg.local_spec_edit.isVisible())

    def test_channel_getters_round_trip_the_incoming_values(self) -> None:
        dlg = self.dialog(
            use_system_proxy=False,
            use_local=True,
            local_spec="curl,python",
            use_wireguard=False,
        )
        self.assertFalse(dlg.get_use_system_proxy())
        self.assertTrue(dlg.get_use_local())
        self.assertEqual(dlg.get_local_spec(), "curl,python")
        self.assertFalse(dlg.get_use_wireguard())

    def test_wireguard_toggle_greys_out_block_private(self) -> None:
        """隧道客户端全在 10.0.0.x：block_private 开着会全杀，UI 必须置灰说明。"""
        dlg = self.dialog(listen_host=ANY_HOST)
        self.assertFalse(dlg.block_private_check.isEnabled())
        self.assertIn("WireGuard", dlg.source_hint.text())

        dlg.wireguard_check.setChecked(False)
        self.assertTrue(dlg.block_private_check.isEnabled())

    def test_wireguard_config_button_hidden_without_a_callback(self) -> None:
        dlg = self.dialog(wireguard_config=None)
        self.assertFalse(dlg.wireguard_config_btn.isVisible())

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
            listen_host=LOOPBACK_HOST,
            block_global=True,
            block_private=True,
            use_wireguard=False,
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

    def test_ineffective_hint_shows_only_when_block_is_moot(self) -> None:
        """提示只在该勾选「确实无效」时出现：环回监听、或 block_private 为隧道让路。"""
        dlg = self.dialog(listen_host=LOOPBACK_HOST)
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.source_hint.isVisible())
        self.assertTrue(dlg.source_hint.text())

        dlg.host_combo.setCurrentIndex(1)
        dlg.wireguard_check.setChecked(False)
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
