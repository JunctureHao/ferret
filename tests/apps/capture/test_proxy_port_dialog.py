"""Tests for the proxy listen-settings dialog.

这里守住的是「三个地址不能混用」这条设计线（见 core/network.py 的模块注释）：
绑定地址可切，本机接入地址恒为环回，局域网地址只用来显示和复制。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from ferret.apps.capture.views import (
    LocalSpecSelector,
    ProxyPortDialog,
    WireGuardConfigDialog,
)
from ferret.core.mitm.modes import WIREGUARD_PORT, LocalTarget
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, PORT_MAX, PORT_MIN


def _target(name: str) -> LocalTarget:
    return LocalTarget(
        display_name=name, executable=rf"C:\app\{name.lower()}.exe", icon_png=None
    )


class LocalSpecSelectorTests(unittest.TestCase):
    """本地重定向进程勾选列表：tokens 单一来源、勾选契约、显隐由对话框驱动。"""

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
        return [self.selector.item(row) for row in range(self.selector.count())]

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
        self.selector.itemChanged.connect(
            lambda _i: changes.append(self.selector.tokens())
        )

        item = self._find("Chrome")
        item.setCheckState(Qt.CheckState.Checked)

        self.assertEqual(changes[-1], ["Chrome"])

    def test_empty_selection_means_capture_everything(self) -> None:
        """全不勾 = 全部：tokens 为空串，过滤串语义由上游处理。"""
        self.selector.set_tokens(["Chrome"])
        self._find("Chrome").setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(self.selector.tokens(), [])

    def test_clicking_row_toggles_check_state(self) -> None:
        """点行任意处切换勾选（itemClicked → _toggle_item 路径）。"""
        self.selector.set_tokens([])
        item = self._find("Chrome")
        item.setSelected(True)
        self.selector._toggle_item(item)
        self.assertEqual(item.checkState(), Qt.CheckState.Checked)
        self.selector._toggle_item(item)
        self.assertEqual(item.checkState(), Qt.CheckState.Unchecked)

    def _indicator_pixel(self, item) -> QColor:
        """渲染整列，取指定行左侧原生勾选框内的采样点颜色。"""
        pm = QPixmap(self.selector.size())
        pm.fill(Qt.GlobalColor.transparent)
        self.selector.render(pm)
        rect = self.selector.visualItemRect(item)
        vp = self.selector.viewport().geometry()
        # qfw TableItemDelegate 在左侧 x+15 处画 19px 勾选框；采样点避开中央
        # 对勾字形的镂空（白勾间隙会透出底色），取框右下内侧。
        x = vp.x() + rect.left() + 15 + 15
        y = vp.y() + rect.center().y() + 6
        return pm.toImage().pixelColor(x, y)

    def test_checked_row_paints_filled_indicator(self) -> None:
        """勾选状态必须画出实色指示器：勾选框由 qfw 原生 delegate 按
        CheckStateRole 绘制（跟随主题色），勾选态填充不透明、未勾选态近透明。"""
        self.selector.set_tokens([])
        item = self._find("Chrome")
        unchecked = self._indicator_pixel(item)
        item.setCheckState(Qt.CheckState.Checked)
        checked = self._indicator_pixel(item)
        self.assertNotEqual(checked.name(), unchecked.name())
        self.assertGreater(checked.alpha(), 200)
        self.assertLess(unchecked.alpha(), 200)

    def test_items_have_no_native_check_indicator(self) -> None:
        """ItemIsUserCheckable 必须保持摘除：勾选框仅作显示（delegate 按
        CheckStateRole 绘制），切换统一走 itemClicked→_toggle_item，避免
        点击勾选框区域时原生切换与 _toggle_item 双重翻转。"""
        self.selector.set_tokens(["curl"])
        for item in self._items():
            self.assertFalse(item.flags() & Qt.ItemFlag.ItemIsUserCheckable)

    def test_visibility_is_driven_by_the_dialog(self) -> None:
        """列表显隐由对话框驱动：set_expanded/is_expanded 语义。"""
        self.selector.set_expanded(True)
        self.assertTrue(self.selector.is_expanded())
        self.selector.set_expanded(False)
        self.assertFalse(self.selector.is_expanded())
        # 显隐不影响 tokens。
        self.selector.set_tokens(["Chrome"])
        self.assertEqual(self.selector.tokens(), ["Chrome"])


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

    def test_config_renders_qr_bitmap(self) -> None:
        """合法配置 → QR 位图渲染进对话框。"""
        dlg = WireGuardConfigDialog("[Interface]\nPrivateKey = abc\n", self.host)
        self.addCleanup(dlg.deleteLater)
        self.assertTrue(dlg.qr_label.pixmap() is not None)
        self.assertGreater(dlg.qr_label.pixmap().width(), 0)


class UpstreamProxyBlockTests(unittest.TestCase):
    """上游代理那一块（.plans/upstream-mode.md §7 第 12 条）。

    它长在 Card ① 系统代理卡片**内部**，不是第五张卡片 —— 这是刻意的：上游是
    「系统代理这条通道的出口属性」，与监听地址端口同卡才不会被读成第五条抓包
    通道。四条通道的出口里只有它受影响，另外三条仍是直连，提示文案得说清楚。
    """

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

    def dialog(self, **overrides) -> ProxyPortDialog:
        values: dict[str, object] = {
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
            "use_upstream": False,
            "upstream_target": "",
            "upstream_username": "",
            "upstream_password": "",
        }
        values.update(overrides)
        dlg = ProxyPortDialog(**values)  # ty: ignore[invalid-argument-type]
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_all_four_values_are_backfilled(self) -> None:
        """回填：四个值都要原样出现在控件里，密码也不例外。"""
        dlg = self.dialog(
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertTrue(dlg.get_use_upstream())
        self.assertEqual(dlg.get_upstream_target(), "http://proxy.corp:8080")
        self.assertEqual(dlg.get_upstream_username(), "alice")
        self.assertEqual(dlg.get_upstream_password(), "secret")

    def test_the_block_is_hidden_until_checked(self) -> None:
        """联动显隐：没勾时整块收起来，卡片保持紧凑（短窗口下也要塞得下）。"""
        dlg = self.dialog(use_upstream=False)
        dlg.show()
        self.app.processEvents()

        self.assertFalse(dlg.upstream_target_row.isVisible())
        self.assertFalse(dlg.upstream_cred_row.isVisible())

        dlg.upstream_check.setChecked(True)
        self.app.processEvents()

        self.assertTrue(dlg.upstream_target_row.isVisible())
        self.assertTrue(dlg.upstream_cred_row.isVisible())
        # 勾上并不带出一行常驻说明：hint 只报异常（重排规则 R3）。
        self.assertFalse(dlg.upstream_hint.isVisible())

    def test_unchecking_hides_the_block_again(self) -> None:
        dlg = self.dialog(use_upstream=True, upstream_target="http://proxy:8080")
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.upstream_target_row.isVisible())

        dlg.upstream_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dlg.upstream_target_row.isVisible())

    def test_the_tooltip_states_the_direct_egress_boundary(self) -> None:
        """边界必须写下来，不许含糊：另外三条通道的出口**不受影响**。

        推论是反直觉的 —— 在「直连出网被封」的企业环境里开了上游之后，
        local/wireguard/reverse 抓到的流量会连不出去。那不是 bug，是这三条
        通道没有上游（计划 §1）。重排后这句话从常驻 hint 降级成勾选框的
        tooltip（R3：界面上只留异常），但一个字都不能少。
        """
        dlg = self.dialog(use_upstream=True, upstream_target="http://proxy:8080")
        dlg.show()
        self.app.processEvents()

        # 常态零 hint：说明进 tooltip，界面那一行留给真正的异常。
        self.assertFalse(dlg.upstream_hint.isVisible())

        text = dlg.upstream_check.toolTip()
        self.assertIn("直连", text)
        for channel in ("本地重定向", "WireGuard", "反向代理"):
            self.assertIn(channel, text)

    def test_the_hint_appears_only_for_the_credential_warning(self) -> None:
        """凭证 + 反代同开时，那一行 hint 才出现，说的是串台警告。

        原生 ``UpstreamAuth`` 一个 addon 同时服务 upstream 与 reverse，
        ``upstream_auth`` 非空时反代目标也会收到 ``Authorization``（分不开，
        见 tests/core/mitm/test_upstream.py 里那条钉子）。没填用户名就没有凭证
        可串，hint 整行收起来。
        """
        dlg = self.dialog(
            use_upstream=True,
            upstream_target="http://proxy:8080",
            use_reverse=True,
            reverse_target="https://example.com",
        )
        dlg.show()
        self.app.processEvents()
        # 还没填用户名：没有凭证可串，这一行不该占位。
        self.assertFalse(dlg.upstream_hint.isVisible())

        dlg.upstream_user_edit.setText("alice")
        self.app.processEvents()
        self.assertTrue(dlg.upstream_hint.isVisible())
        self.assertIn("反代目标", dlg.upstream_hint.text())

        dlg.upstream_user_edit.setText("")
        self.app.processEvents()
        self.assertFalse(dlg.upstream_hint.isVisible())

    def test_target_is_stripped_but_the_password_is_not(self) -> None:
        """密码**不能** strip：前后空格是密码的合法组成部分，地址和用户名则
        几乎必然是误粘贴。"""
        dlg = self.dialog(
            use_upstream=True,
            upstream_target="  http://proxy:8080  ",
            upstream_username="  alice  ",
            upstream_password="  se cret  ",
        )
        self.assertEqual(dlg.get_upstream_target(), "http://proxy:8080")
        self.assertEqual(dlg.get_upstream_username(), "alice")
        self.assertEqual(dlg.get_upstream_password(), "  se cret  ")

    def test_there_is_no_port_spinbox(self) -> None:
        """端口在地址里，上游没有独立监听口 —— 它占的就是 regular 那条监听
        （spec 不带 ``@``）。有个端口框只会让人以为这是第五条通道。"""
        dlg = self.dialog(use_upstream=True)
        self.assertFalse(hasattr(dlg, "upstream_port_spin"))

    def test_the_password_field_is_masked(self) -> None:
        dlg = self.dialog(use_upstream=True, upstream_password="secret")
        self.assertEqual(
            dlg.upstream_password_edit.echoMode(), QLineEdit.EchoMode.Password
        )


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
        # 用 dict+** 解包便于覆盖式覆盖默认参数；ty 推不出 dict 值类型是预期的。
        values: dict[str, object] = {
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
            "use_upstream": False,
            "upstream_target": "",
            "upstream_username": "",
            "upstream_password": "",
        }
        values.update(overrides)
        dlg = ProxyPortDialog(**values)  # ty: ignore[invalid-argument-type]
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_process_list_visibility_follows_the_expand_button(self) -> None:
        """勾上通道后「选择」按钮才出现，列表再看展开态（重排规则 R1）。"""
        dlg = self.dialog(use_local=False)
        dlg.show()
        self.app.processEvents()
        # 没勾通道：整张卡就是一行 header，按钮和列表都不占位。
        self.assertFalse(dlg.local_fold_btn.isVisible())
        self.assertFalse(dlg.local_spec_edit.isVisible())

        dlg.local_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(dlg.local_fold_btn.isVisible())
        self.assertFalse(dlg.local_spec_edit.isVisible())

        dlg.local_fold_btn.click()
        self.app.processEvents()
        self.assertTrue(dlg.local_spec_edit.isVisible())

        dlg.local_fold_btn.click()
        self.app.processEvents()
        self.assertFalse(dlg.local_spec_edit.isVisible())

        # 取消勾选通道：按钮与列表一起收走，卡片回到一行。
        dlg.local_fold_btn.click()
        dlg.local_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dlg.local_fold_btn.isVisible())
        self.assertFalse(dlg.local_spec_edit.isVisible())

    def test_unchecked_channels_collapse_to_a_single_row(self) -> None:
        """默认姿态（只勾系统代理）下，其余三张卡各自只剩一行 header。"""
        dlg = self.dialog(
            use_local=False, use_wireguard=False, use_reverse=False, use_upstream=False
        )
        dlg.show()
        self.app.processEvents()
        for widget in (
            dlg.local_summary,
            dlg.local_fold_btn,
            dlg.local_spec_edit,
            dlg.wireguard_port_label,
            dlg.wireguard_config_btn,
            dlg.reverse_params,
            dlg.upstream_target_row,
            dlg.upstream_cred_row,
        ):
            self.assertFalse(widget.isVisible(), widget.objectName() or widget)
        # 勾着的那条反过来：参数区必须在。
        self.assertTrue(dlg.system_params.isVisible())

    def test_unchecking_the_system_proxy_collapses_its_params(self) -> None:
        """系统代理也守同一条规则：取消勾选后地址/端口/上游整块收走。"""
        dlg = self.dialog(use_system_proxy=True)
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.system_params.isVisible())

        dlg.system_proxy_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dlg.system_params.isVisible())
        self.assertFalse(dlg.host_combo.isVisible())

    def test_checking_local_keeps_the_process_list_collapsed(self) -> None:
        """勾上本地重定向只多出一行摘要，不把 6 行列表顶出来（§4.4）。"""
        dlg = self.dialog(use_local=False)
        dlg.show()
        self.app.processEvents()

        dlg.local_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(dlg.local_summary.isVisible())
        self.assertFalse(dlg.local_spec_edit.isVisible())

    def test_local_summary_says_everything_when_no_token(self) -> None:
        """「留空 = 全部进程」这条语义从 hint 搬进了 header 摘要。"""
        dlg = self.dialog(use_local=True, local_spec="")
        self.assertIn("全部", dlg.local_summary.text())

    def test_local_summary_counts_the_extra_tokens(self) -> None:
        """选了多个时摘要报「首个 +N 个」，不铺开整串。"""
        dlg = self.dialog(use_local=True)
        dlg.local_spec_edit._targets = [_target("Chrome"), _target("钉钉")]
        dlg.local_spec_edit.set_items_for_testing()

        dlg.local_spec_edit.set_tokens(["Chrome"])
        self.assertEqual(dlg.local_summary.text(), "Chrome")

        dlg.local_spec_edit.set_tokens(["Chrome", "curl", "python"])
        self.assertIn("Chrome", dlg.local_summary.text())
        self.assertIn("+2", dlg.local_summary.text())

    def test_channel_titles_carry_their_explanation_in_a_tooltip(self) -> None:
        """R2/R4：标题只留名词，「干什么用」的括号注解一律降级为 tooltip。

        同一句话只能出现在一处 —— tooltip 里的解释不许再回到标题文字里，
        否则重排的收益全部抵消掉。
        """
        dlg = self.dialog()
        for check in (
            dlg.system_proxy_check,
            dlg.local_check,
            dlg.wireguard_check,
            dlg.reverse_check,
            dlg.upstream_check,
        ):
            with self.subTest(title=check.text()):
                self.assertTrue(check.toolTip())
                self.assertNotIn("（", check.text())
                self.assertNotIn(check.toolTip(), check.text())

    def test_inline_inputs_keep_an_accessible_name(self) -> None:
        """行内 BodyLabel 删了（R4），屏幕阅读器不读 placeholder —— 名字得补回来。"""
        dlg = self.dialog()
        for widget in (
            dlg.host_combo,
            dlg.port_spin,
            dlg.upstream_target_edit,
            dlg.upstream_user_edit,
            dlg.upstream_password_edit,
            dlg.reverse_target_edit,
            dlg.reverse_port_spin,
        ):
            with self.subTest(widget=type(widget).__name__):
                self.assertTrue(widget.accessibleName())

    def _expanded_dialog_at(
        self, width: int, height: int, rows: int
    ) -> ProxyPortDialog:
        """矮窗口 + 展开的进程列表：复现「窗口不够高」那一档真实布局。"""
        self.host.resize(width, height)
        self.app.processEvents()
        dlg = self.dialog()
        dlg.local_spec_edit._targets = [_target(f"P{i}") for i in range(rows)]
        dlg.local_spec_edit.set_items_for_testing()
        dlg.show()
        self.app.processEvents()
        dlg.local_fold_btn.click()
        self.app.processEvents()
        return dlg

    def test_short_window_shrinks_the_list_instead_of_overlapping(self) -> None:
        """窗口不够高时列表必须可压矮：固定高度会被布局分配不足后由 widget
        钳回，下方兄弟件却按未钳回位置摆放——列表与文案叠画。

        列表默认收起之后这条更容易满足，但展开态仍得成立 —— 用户挑进程的时候
        正是卡片最高的那一刻。
        """
        dlg = self._expanded_dialog_at(962, 600, 8)
        lst = dlg.local_spec_edit
        # 列表与下方控件分属不同卡片，geometry() 的参考系不同，统一换算到全局。
        lst_rect = QRect(lst.mapToGlobal(QPoint(0, 0)), lst.size())
        for other in (dlg.wireguard_check, dlg.reverse_check):
            other_rect = QRect(other.mapToGlobal(QPoint(0, 0)), other.size())
            self.assertFalse(lst_rect.intersects(other_rect))
        self.assertLess(lst.height(), lst._full_height)

    def test_every_visible_row_receives_the_click(self) -> None:
        """被透明文案盖住的行点不到（点击被上层兄弟件吞掉）：断言矮窗口下
        每个可见行中心的最高层控件仍是列表视口。

        680px 这一档是「压矮了但四行还都在视口里」，正好能逐行断言。
        """
        dlg = self._expanded_dialog_at(962, 680, 4)
        lst = dlg.local_spec_edit
        for row in range(lst.count()):
            rect = lst.visualItemRect(lst.item(row))
            self.assertTrue(rect.isValid(), f"row {row}")
            receiver = QApplication.widgetAt(lst.viewport().mapToGlobal(rect.center()))
            self.assertIs(receiver, lst.viewport(), f"row {row}")

    def test_wireguard_row_has_the_qr_button_inline(self) -> None:
        """二维码按钮与勾选框同行：wireguard_check 与按钮在同一 parent 行布局。"""
        dlg = self.dialog()
        self.assertIs(
            dlg.wireguard_config_btn.parentWidget(), dlg.wireguard_check.parentWidget()
        )

    def test_channel_getters_round_trip_the_incoming_values(self) -> None:
        dlg = self.dialog(
            use_system_proxy=False,
            use_local=True,
            local_spec="curl,python",
            use_wireguard=False,
        )
        # 注入确定性的候选集：真实枚举可能把 "python" 规范化成 "Python"。
        dlg.local_spec_edit._targets = [_target("Chrome"), _target("钉钉")]
        dlg.local_spec_edit.set_items_for_testing()
        dlg.local_spec_edit.set_tokens(["curl", "python"])
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
        dlg = self.dialog(use_wireguard=True, wireguard_config=None)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.wireguard_config_btn.isVisible())

    def test_qr_button_appears_with_the_checkbox(self) -> None:
        """勾上那一刻就该能拿到码：按钮随勾选出现（生成时机见 facade）。

        旧行为是常驻显示 + ``setEnabled(勾选)``，而内核没起过时点下去必炸
        ``FileNotFoundError``。现在按钮与勾选同生共死，勾上即可出码。
        """
        dlg = self.dialog(use_wireguard=False)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.wireguard_config_btn.isVisible())
        self.assertFalse(dlg.wireguard_port_label.isVisible())

        dlg.wireguard_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(dlg.wireguard_config_btn.isVisible())
        # 端口从一行 hint 降级成 header 行内小标签。
        self.assertIn(str(WIREGUARD_PORT), dlg.wireguard_port_label.text())

        dlg.wireguard_check.setChecked(False)
        self.app.processEvents()
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
        """提示只在 block_private 真的被让路时出现（R3：常态零 hint）。

        「仅本机监听时来源限制不生效」那一句删掉了 —— 两个勾选框已经置灰，
        再配一句话是重复（§4.3）。
        """
        dlg = self.dialog(listen_host=LOOPBACK_HOST)
        dlg.show()
        self.app.processEvents()
        self.assertFalse(dlg.source_hint.isVisible())

        # 切到 0.0.0.0 且 WireGuard 勾着：block_private 被强制放行，必须说出来。
        dlg.host_combo.setCurrentIndex(1)
        self.app.processEvents()
        self.assertTrue(dlg.source_hint.isVisible())
        self.assertIn("WireGuard", dlg.source_hint.text())

        dlg.wireguard_check.setChecked(False)
        self.app.processEvents()
        self.assertFalse(dlg.source_hint.isVisible())

    def test_unknown_lan_address_is_reported_as_an_anomaly(self) -> None:
        """探测失败是「设置有问题」那一类，留一行 `!`（§4.5 第四种情况）。"""
        dlg = self.dialog(listen_host=ANY_HOST, lan_address=None, use_wireguard=False)
        dlg.show()
        self.app.processEvents()
        self.assertTrue(dlg.source_hint.isVisible())
        self.assertIn("局域网", dlg.source_hint.text())

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
