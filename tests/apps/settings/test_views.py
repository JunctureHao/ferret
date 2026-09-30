import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.capture.views import CapturesInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.core.settings import CONFIG


class AutoSaveSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_exposes_auto_save_switch(self) -> None:
        settings = SettingsInterface()
        settings.deleteLater()

    def test_capture_toolbar_does_not_restore_removed_buttons(self) -> None:
        capture = CapturesInterface()
        self.assertFalse(hasattr(capture.toolbar, "auto_save_btn"))
        self.assertFalse(hasattr(capture.toolbar, "import_btn"))
        self.assertFalse(hasattr(capture.toolbar, "save_session_btn"))
        capture.deleteLater()


class ProtocolSwitchCardTests(unittest.TestCase):
    """协议层两开关的卡片绑定与热更接线（.plans/2-protocol-switches.md §2.4）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.settings = SettingsInterface()
        self.addCleanup(self.settings.deleteLater)

    def test_cards_are_bound_to_the_config_items(self) -> None:
        """卡片走 configItem 自动落盘，绑定错了开关就是摆设。"""
        self.assertIs(self.settings.http2_card.configItem, CONFIG.http2_enabled)
        self.assertIs(self.settings.http3_card.configItem, CONFIG.http3_enabled)

    def test_flipping_the_config_pushes_both_switches_at_once(self) -> None:
        """valueChanged → 整体重推两项（快照原子），内核没接时（_mitm None）静默。"""
        calls: list[dict[str, bool]] = []

        class _Recorder:
            def set_protocol_options(self, **kwargs: bool) -> None:
                calls.append(kwargs)

        original_http2 = CONFIG.get(CONFIG.http2_enabled)
        original_http3 = CONFIG.get(CONFIG.http3_enabled)
        # save=False：测试只验信号链，不把值落进仓库根的 config/config.json
        # （QConfig.set 默认 save=True，会往 CWD 写盘）。
        self.addCleanup(CONFIG.set, CONFIG.http2_enabled, original_http2, False)
        self.addCleanup(CONFIG.set, CONFIG.http3_enabled, original_http3, False)

        self.settings._mitm = _Recorder()  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, self.settings, "_mitm", None)

        CONFIG.set(CONFIG.http2_enabled, not bool(original_http2), False)

        self.assertEqual(
            calls,
            [{"http2": not bool(original_http2), "http3": bool(original_http3)}],
        )


class UpdateCardTests(unittest.TestCase):
    """「关于与更新」组的卡片绑定与自动检查闸门（.plans/3-auto-update.md §2）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.settings = SettingsInterface()
        self.addCleanup(self.settings.deleteLater)

    def test_auto_check_card_is_bound_to_the_config_item(self) -> None:
        self.assertIs(
            self.settings.auto_update_card.configItem, CONFIG.auto_check_update
        )

    def test_auto_check_is_blocked_in_dev_mode(self) -> None:
        """开发态 update_supported() 恒 False：自动入口必须不发起检查。"""
        calls: list = []
        original = self.settings.update_controller.check
        self.settings.update_controller.check = lambda: calls.append(True)  # ty: ignore[invalid-assignment]
        self.addCleanup(setattr, self.settings.update_controller, "check", original)

        self.settings.check_updates_auto()

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
