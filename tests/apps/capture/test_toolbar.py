from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel
from qfluentwidgets import PushButton

from ferret.apps.capture.controllers import CaptureState
from ferret.apps.capture.views import CaptureCommandBar, CaptureUiState


class CaptureCommandBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.bar = CaptureCommandBar()
        self.bar.resize(960, 44)
        self.bar.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.bar.close()
        self.bar.deleteLater()
        self.app.processEvents()

    @staticmethod
    def state(**overrides) -> CaptureUiState:
        values = {
            "capture_state": CaptureState.STOPPED,
            "endpoint": "127.0.0.1:8080",
            "total_count": 0,
            "shown_count": 0,
            "selected_count": 0,
            "active_filter_count": 0,
        }
        values.update(overrides)
        return CaptureUiState(**values)  # type: ignore

    def test_lifecycle_states_update_text_and_control(self) -> None:
        self.bar.set_state(self.state())
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "开始抓包")

        self.bar.set_state(self.state(capture_state=CaptureState.STARTING))
        self.assertFalse(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.RUNNING))
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "停止抓包")

        self.bar.set_state(self.state(capture_state=CaptureState.FAILED))
        self.assertTrue(self.bar.control_btn.isEnabled())

    def test_counts_and_clear_state_are_distinct(self) -> None:
        self.bar.set_state(
            self.state(
                total_count=43,
                shown_count=12,
                selected_count=2,
                active_filter_count=3,
            ),
        )
        self.assertEqual(self.bar.stats_label.text(), "12 / 43 条")
        self.assertTrue(self.bar.captures_delete_btn.isEnabled())
        self.assertIn("已选 2 条", self.bar.stats_label.toolTip())

    def test_cleanup_failure_offers_stop_retry_after_proxy_is_restored(self) -> None:
        self.bar.set_state(
            self.state(
                capture_state=CaptureState.FAILED,
                proxy_attached=False,
                stop_failed=True,
            )
        )
        self.assertEqual(self.bar.control_btn.text(), "重试停止")
        self.assertEqual(self.bar.control_btn.toolTip(), "重试停止")
        self.assertTrue(self.bar.control_btn.isEnabled())

    def test_narrow_width_preserves_status_and_overflow_actions(self) -> None:
        self.bar.set_state(
            self.state(
                capture_state=CaptureState.RUNNING,
                total_count=43,
                shown_count=12,
                lan_exposed=True,
                channels_summary="系统代理 · 本地重定向 · WireGuard · 反向代理 · SOCKS5",
                channel_issue="本地重定向启动失败",
            )
        )
        opened_settings = []
        self.bar.portRequested.connect(lambda: opened_settings.append(True))
        for width in (600, 400):
            with self.subTest(width=width):
                self.bar.resize(width, self.bar.height())
                self.app.processEvents()
                self.assertEqual(self.bar.width(), width)
                self.assertTrue(self.bar.control_btn.isVisible())
                self.assertTrue(self.bar.environment_btn.isVisible())
                self.assertTrue(self.bar.endpoint_btn.isVisible())
                self.assertIn("⚠", self.bar.endpoint_btn.text())
                self.assertIn("本地重定向启动失败", self.bar.endpoint_btn.toolTip())
                self.assertTrue(self.bar.exposure_label.isVisible())
                for button in (
                    self.bar.control_btn,
                    self.bar.captures_delete_btn,
                    self.bar.captures_delete_more_btn,
                    self.bar.environment_btn,
                ):
                    if button.isVisible():
                        self.assertTrue(
                            self.bar.rect().contains(
                                button.mapTo(self.bar, button.rect().topLeft())
                            )
                        )
                        self.assertTrue(
                            self.bar.rect().contains(
                                button.mapTo(self.bar, button.rect().bottomRight())
                            )
                        )
                menu = self.bar._build_more_menu()
                actions = {action.text(): action for action in menu.actions()}
                if not self.bar.proxy_setting_btn.isVisible():
                    actions["通道设置"].trigger()
                if not self.bar.captures_delete_btn.isVisible():
                    self.assertTrue(actions["清空当前流量"].isEnabled())
                    self.assertTrue(actions["删除未标记流量"].isEnabled())
                menu.deleteLater()
        self.assertTrue(opened_settings)

    def test_control_button_is_a_labelled_push_button(self) -> None:
        """主控开关是带文字的引导 PushButton，文案/可用态随抓包态切换。"""
        self.assertIsInstance(self.bar.control_btn, PushButton)

        self.bar.set_state(self.state())
        self.assertEqual(self.bar.control_btn.text(), "开始抓包")
        self.assertTrue(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.STARTING))
        self.assertEqual(self.bar.control_btn.text(), "启动中")
        self.assertFalse(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.RUNNING))
        self.assertEqual(self.bar.control_btn.text(), "停止抓包")
        self.assertTrue(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.STOPPING))
        self.assertEqual(self.bar.control_btn.text(), "停止中")
        self.assertFalse(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.FAILED))
        self.assertEqual(self.bar.control_btn.text(), "重试抓包")
        self.assertTrue(self.bar.control_btn.isEnabled())

    def test_endpoint_uses_application_font(self) -> None:
        endpoint_font = self.bar.endpoint_btn.font()
        bar_font = self.bar.font()
        self.assertEqual(endpoint_font.family(), bar_font.family())
        self.assertEqual(endpoint_font.pointSize(), bar_font.pointSize())

    def test_endpoint_is_plain_text(self) -> None:
        self.assertIsInstance(self.bar.endpoint_label, QLabel)
        self.assertIs(self.bar.endpoint_btn, self.bar.endpoint_label)
        self.assertEqual(self.bar.endpoint_label.toolTip(), "")

    def test_exposure_label_marks_an_open_listen_address(self) -> None:
        """绑定 0.0.0.0 是外部设备能连进来的唯一入口，必须在工具栏上看得见。"""
        self.bar.set_state(self.state(lan_exposed=True))
        self.assertTrue(self.bar.exposure_label.isVisible())

        self.bar.set_state(self.state(lan_exposed=False))
        self.assertFalse(self.bar.exposure_label.isVisible())

    def test_exposure_label_remains_visible_in_narrow_windows(self) -> None:
        self.bar.set_state(self.state(lan_exposed=True))
        self.bar.resize(400, self.bar.height())
        self.app.processEvents()
        self.assertTrue(self.bar.exposure_label.isVisible())

    def test_endpoint_tooltip_distinguishes_local_from_open(self) -> None:
        """端点文本恒为环回，所以「谁能连」这件事只能靠 tooltip 说清楚。"""
        self.bar.set_state(self.state(lan_exposed=False))
        local_tip = self.bar.endpoint_btn.toolTip()
        self.assertIn("127.0.0.1:8080", local_tip)

        self.bar.set_state(self.state(lan_exposed=True))
        open_tip = self.bar.endpoint_btn.toolTip()
        self.assertIn("127.0.0.1:8080", open_tip)
        self.assertNotEqual(local_tip, open_tip)

    def test_running_session_shows_the_channel_summary(self) -> None:
        """抓包会话开着时，端点位置改显通道并集 —— 那才是当下该关注的状态。"""
        self.bar.set_state(
            self.state(capture_state=CaptureState.RUNNING, channels_summary="Sys"),
        )
        self.assertEqual(self.bar.endpoint_btn.text(), "Sys")

        self.bar.set_state(self.state())
        self.assertEqual(self.bar.endpoint_btn.text(), "127.0.0.1:8080")

    def test_channel_issue_is_flagged_and_explained(self) -> None:
        self.bar.set_state(
            self.state(
                capture_state=CaptureState.RUNNING,
                channels_summary="Sys",
                channel_issue="Local redirect: approval needed",
            ),
        )
        self.assertIn("⚠", self.bar.endpoint_btn.text())
        self.assertIn("approval needed", self.bar.endpoint_btn.toolTip())

    def test_clear_commands_have_distinct_accessible_names(self) -> None:
        self.assertEqual(self.bar.captures_delete_btn.text(), "清空")
        self.assertEqual(self.bar.captures_delete_btn.accessibleName(), "清空当前流量")
        self.assertEqual(self.bar.captures_delete_more_btn.toolTip(), "更多删除操作")
        self.assertEqual(
            self.bar.captures_delete_more_btn.accessibleName(), "更多删除操作"
        )

    def test_the_arrow_button_tracks_the_clear_disable_gate(self) -> None:
        """空表两钮都灰（点了空转），有流量才启用（与清空同档）。"""
        self.bar.set_state(self.state(total_count=0))
        self.assertFalse(self.bar.captures_delete_btn.isEnabled())
        self.assertFalse(self.bar.captures_delete_more_btn.isEnabled())

        self.bar.set_state(self.state(total_count=5))
        self.assertTrue(self.bar.captures_delete_btn.isEnabled())
        self.assertTrue(self.bar.captures_delete_more_btn.isEnabled())

    def test_the_main_button_still_emits_clear(self) -> None:
        """主钮单击 = 清空，行为零变化（保住肌肉记忆）。"""
        self.bar.set_state(self.state(total_count=5))
        fired = []
        self.bar.clearRequested.connect(lambda: fired.append(True))
        self.bar.captures_delete_btn.click()
        self.assertEqual(fired, [True])

    def test_the_dropdown_only_offers_the_action_the_button_lacks(self) -> None:
        """下拉只放「删除未标记流量」；「清空当前流量」是主钮动作，不在下拉里重复。"""
        self.bar.set_state(self.state(total_count=5))
        unmarked = []
        self.bar.deleteUnmarkedRequested.connect(lambda: unmarked.append(True))

        menu = self.bar._build_delete_menu()
        actions = [a for a in menu.actions() if a.text()]
        self.assertEqual([a.text() for a in actions], ["删除未标记流量"])

        actions[0].trigger()
        self.assertEqual(unmarked, [True])
        menu.deleteLater()

    def test_more_menu_loads_flows_and_locates_the_selection(self) -> None:
        self.bar.set_state(self.state(total_count=5, selected_count=1))
        opened = []
        located = []
        self.bar.openRequested.connect(lambda: opened.append(True))
        self.bar.locateRequested.connect(lambda: located.append(True))

        menu = self.bar._build_more_menu()
        actions = {action.text(): action for action in menu.actions()}
        actions["加载 Flow 到当前列表"].trigger()
        self.assertTrue(actions["定位选中"].isEnabled())
        actions["定位选中"].trigger()
        self.assertEqual(opened, [True])
        self.assertEqual(located, [True])
        menu.deleteLater()

    def test_more_menu_disables_locating_without_a_selection(self) -> None:
        self.bar.set_state(self.state(total_count=5))
        located = []
        self.bar.locateRequested.connect(lambda: located.append(True))

        menu = self.bar._build_more_menu()
        locate = next(
            action for action in menu.actions() if action.text() == "定位选中"
        )
        self.assertFalse(locate.isEnabled())
        locate.trigger()
        self.assertEqual(located, [])
        menu.deleteLater()


if __name__ == "__main__":
    unittest.main()
