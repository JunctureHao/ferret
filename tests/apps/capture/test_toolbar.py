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
        return CaptureUiState(**values)

    def test_lifecycle_states_update_text_and_control(self) -> None:
        self.bar.set_state(self.state(), False)
        self.assertEqual(self.bar.state_label.text(), "未捕获系统流量")
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "开始捕获系统流量")

        self.bar.set_state(
            self.state(capture_state=CaptureState.STARTING), False
        )
        self.assertEqual(self.bar.state_label.text(), "启动中")
        self.assertFalse(self.bar.control_btn.isEnabled())

        self.bar.set_state(
            self.state(capture_state=CaptureState.RUNNING), False
        )
        self.assertEqual(self.bar.state_label.text(), "正在捕获")
        self.assertTrue(self.bar.control_btn.isEnabled())
        self.assertEqual(self.bar.control_btn.toolTip(), "停止捕获系统流量")

        self.bar.set_state(
            self.state(capture_state=CaptureState.FAILED), False
        )
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
        self.bar.set_state(
            self.state(total_count=43, shown_count=12), False
        )
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


if __name__ == "__main__":
    unittest.main()
