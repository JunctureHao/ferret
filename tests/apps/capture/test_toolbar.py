import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

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
        self.bar.set_state(self.state(), False)
        self.assertEqual(self.bar.state_label.text(), "未捕获系统流量")
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "开始抓包")

        self.bar.set_state(self.state(capture_state=CaptureState.STARTING), False)
        self.assertEqual(self.bar.state_label.text(), "启动中")
        self.assertFalse(self.bar.control_btn.isEnabled())

        self.bar.set_state(self.state(capture_state=CaptureState.RUNNING), False)
        self.assertEqual(self.bar.state_label.text(), "正在捕获")
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "停止抓包")

        self.bar.set_state(self.state(capture_state=CaptureState.FAILED), False)
        self.assertEqual(self.bar.state_label.text(), "启动失败")
        self.assertTrue(self.bar.control_btn.isEnabled())

    def test_counts_filter_badge_and_clear_state_are_distinct(self) -> None:
        self.bar.set_state(
            self.state(
                total_count=43,
                shown_count=12,
                selected_count=2,
                active_filter_count=3,
            ),
            True,
        )
        self.assertEqual(self.bar.stats_label.text(), "12 / 43 条")
        self.assertEqual(self.bar.filter_badge.text(), "3")
        self.assertTrue(self.bar.filter_badge.isVisible())
        self.assertTrue(self.bar.captures_delete_btn.isEnabled())
        self.assertIn("已选 2 条", self.bar.stats_label.toolTip())

    def test_compact_mode_shortens_endpoint_and_count(self) -> None:
        self.bar.set_state(self.state(total_count=43, shown_count=12), False)
        self.bar.resize(800, 44)
        self.app.processEvents()
        self.assertEqual(self.bar.endpoint_btn.text(), ":8080")
        self.assertEqual(self.bar.stats_label.text(), "12/43")

        self.bar.resize(600, 44)
        self.app.processEvents()
        self.assertFalse(self.bar.endpoint_btn.isVisible())
        self.assertFalse(self.bar.proxy_setting_btn.isVisible())
        self.assertTrue(self.bar.environment_btn.isVisible())
        self.assertLessEqual(
            self.bar.captures_delete_btn.geometry().right(), self.bar.width()
        )

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
        self.bar.set_state(self.state(lan_exposed=True), False)
        self.assertTrue(self.bar.exposure_label.isVisible())

        self.bar.set_state(self.state(lan_exposed=False), False)
        self.assertFalse(self.bar.exposure_label.isVisible())

    def test_exposure_label_yields_to_very_compact_mode(self) -> None:
        self.bar.set_state(self.state(lan_exposed=True), False)
        self.bar.resize(600, 44)
        self.app.processEvents()
        self.assertFalse(self.bar.exposure_label.isVisible())

    def test_endpoint_tooltip_distinguishes_local_from_open(self) -> None:
        """端点文本恒为环回，所以「谁能连」这件事只能靠 tooltip 说清楚。"""
        self.bar.set_state(self.state(lan_exposed=False), False)
        local_tip = self.bar.endpoint_btn.toolTip()
        self.assertIn("127.0.0.1:8080", local_tip)

        self.bar.set_state(self.state(lan_exposed=True), False)
        open_tip = self.bar.endpoint_btn.toolTip()
        self.assertIn("127.0.0.1:8080", open_tip)
        self.assertNotEqual(local_tip, open_tip)

    def test_running_session_shows_the_channel_summary(self) -> None:
        """抓包会话开着时，端点位置改显通道并集 —— 那才是当下该关注的状态。"""
        self.bar.set_state(
            self.state(capture_state=CaptureState.RUNNING, channels_summary="Sys"), False
        )
        self.assertEqual(self.bar.endpoint_btn.text(), "Sys")

        self.bar.set_state(self.state(), False)
        self.assertEqual(self.bar.endpoint_btn.text(), "127.0.0.1:8080")

    def test_channel_issue_is_flagged_and_explained(self) -> None:
        self.bar.set_state(
            self.state(
                capture_state=CaptureState.RUNNING,
                channels_summary="Sys",
                channel_issue="Local redirect: approval needed",
            ),
            False,
        )
        self.assertIn("⚠", self.bar.endpoint_btn.text())
        self.assertIn("approval needed", self.bar.endpoint_btn.toolTip())

    def test_the_delete_button_is_split_with_an_arrow(self) -> None:
        """拆分按钮：主钮维持 32×32、箭头 24×32，两钮紧挨视觉一枚
        （`.plans/0-mark-filter-polish.md` §2.2）。"""
        self.assertEqual(self.bar.captures_delete_btn.size().width(), 32)
        self.assertEqual(self.bar.captures_delete_more_btn.size().width(), 24)
        self.assertEqual(self.bar.captures_delete_more_btn.toolTip(), "更多删除操作")
        self.assertEqual(
            self.bar.captures_delete_more_btn.accessibleName(), "更多删除操作"
        )

    def test_the_arrow_button_tracks_the_clear_disable_gate(self) -> None:
        """空表两钮都灰（点了空转），有流量才启用（与清空同档）。"""
        self.bar.set_state(self.state(total_count=0), False)
        self.assertFalse(self.bar.captures_delete_btn.isEnabled())
        self.assertFalse(self.bar.captures_delete_more_btn.isEnabled())

        self.bar.set_state(self.state(total_count=5), False)
        self.assertTrue(self.bar.captures_delete_btn.isEnabled())
        self.assertTrue(self.bar.captures_delete_more_btn.isEnabled())

    def test_the_main_button_still_emits_clear(self) -> None:
        """主钮单击 = 清空，行为零变化（保住肌肉记忆）。"""
        fired = []
        self.bar.clearRequested.connect(lambda: fired.append(True))
        self.bar.captures_delete_btn.click()
        self.assertEqual(fired, [True])

    def test_the_dropdown_only_offers_the_action_the_button_lacks(self) -> None:
        """下拉只放「删除未标记流量」；「清空当前流量」是主钮动作，不在下拉里重复。"""
        unmarked = []
        self.bar.deleteUnmarkedRequested.connect(lambda: unmarked.append(True))

        menu = self.bar._build_delete_menu()
        actions = [a for a in menu.actions() if a.text()]
        self.assertEqual([a.text() for a in actions], ["删除未标记流量"])

        actions[0].trigger()
        self.assertEqual(unmarked, [True])
        menu.deleteLater()


if __name__ == "__main__":
    unittest.main()
