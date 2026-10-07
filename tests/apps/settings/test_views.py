from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.capture.views import CapturesInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.apps.update.coordinator import UpdateCoordinator
from ferret.core import update as update_core
from ferret.core.settings import CONFIG


class AutoSaveSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_exposes_auto_save_switch(self) -> None:
        settings = SettingsInterface(updates=Mock())
        settings.deleteLater()

    def test_capture_toolbar_does_not_restore_removed_buttons(self) -> None:
        capture = CapturesInterface()
        self.assertFalse(hasattr(capture.toolbar, "auto_save_btn"))
        self.assertFalse(hasattr(capture.toolbar, "import_btn"))
        self.assertFalse(hasattr(capture.toolbar, "save_session_btn"))
        capture.deleteLater()


class ProtocolSwitchCardTests(unittest.TestCase):
    """协议层两开关的卡片绑定与热更接线（docs/design.md#capture）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 这些用例不碰更新卡片；协调器本就必填（主窗口持有那个实例才是真的），
        # 给个 Mock 顶位即可。
        self.settings = SettingsInterface(updates=Mock())
        self.addCleanup(self.settings.deleteLater)

    def test_cards_are_bound_to_the_config_items(self) -> None:
        """卡片走 configItem 自动落盘，绑定错了开关就是摆设。"""
        self.assertIs(self.settings.http2_card.configItem, CONFIG.http2_enabled)
        self.assertIs(self.settings.http3_card.configItem, CONFIG.http3_enabled)

    def test_each_hot_apply_failure_is_visible_and_keeps_saved_intent(self) -> None:
        cases = (
            (
                "__on_sticky_session_changed",
                "set_sticky_session",
                CONFIG.sticky_session_enabled,
            ),
            (
                "__on_anticache_plaintext_changed",
                "set_anticache_plaintext",
                CONFIG.anticache_plaintext,
            ),
            ("__on_protocol_changed", "set_protocol_options", CONFIG.http2_enabled),
            (
                "__on_dns_use_hosts_changed",
                "set_dns_options",
                CONFIG.dns_use_hosts_file,
            ),
        )
        for callback, setter, item in cases:
            with self.subTest(callback=callback):
                value = CONFIG.get(item)
                facade = Mock()
                getattr(facade, setter).side_effect = RuntimeError("apply rejected")
                self.settings._mitm = facade
                with (
                    patch("ferret.apps.settings.views.show_warning") as warning,
                    self.assertLogs("ferret.settings", "WARNING") as logs,
                ):
                    getattr(self.settings, "_SettingsInterface" + callback)(value)
                self.assertEqual(
                    logs.output,
                    [
                        (
                            "WARNING:ferret.settings:"
                            "Failed to apply saved settings: apply rejected"
                        )
                    ],
                )
                warning.assert_called_once()
                self.assertIn("apply rejected", warning.call_args.args[1])
                self.assertEqual(CONFIG.get(item), value)
        self.settings._mitm = None

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
    """设置页只反映更新协调器状态，首次构造也要接上已有任务。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        self.updates = UpdateCoordinator(self.window)
        supported = patch.object(update_core, "update_supported", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)

    def create_settings(self) -> SettingsInterface:
        return SettingsInterface(self.window, updates=self.updates)

    def test_auto_check_card_is_bound_to_the_config_item(self) -> None:
        settings = self.create_settings()
        self.assertIs(settings.auto_update_card.configItem, CONFIG.auto_check_update)

    def test_constructing_and_showing_settings_does_not_check_for_updates(self) -> None:
        with (
            patch.object(self.updates, "check_auto") as auto,
            patch.object(self.updates, "check_manual") as manual,
            patch.object(self.updates.controller, "check") as check,
        ):
            settings = self.create_settings()
            for _ in range(2):
                settings.show()
                settings.hide()
            auto.assert_not_called()
            manual.assert_not_called()
            check.assert_not_called()

    def test_manual_button_uses_the_shared_coordinator(self) -> None:
        with patch.object(self.updates, "check_manual") as manual:
            settings = self.create_settings()
            settings.update_card.button.click()
        manual.assert_called_once_with()

    def test_page_created_during_check_displays_and_follows_current_state(self) -> None:
        self.updates.controller._busy = True
        self.updates.controller.check_started.emit()
        self.updates.controller.busy_changed.emit(True)
        settings = self.create_settings()

        self.assertFalse(settings.update_card.button.isEnabled())
        self.assertEqual(settings.update_card.contentLabel.text(), "正在检查更新…")

        self.updates.controller._busy = False
        self.updates.controller.busy_changed.emit(False)
        self.updates.controller.check_finished.emit()

        self.assertTrue(settings.update_card.button.isEnabled())
        self.assertEqual(
            settings.update_card.contentLabel.text(), "检查 GitHub 上是否有新版本"
        )

    def test_page_created_during_download_disables_the_check_button(self) -> None:
        self.updates.controller._busy = True
        settings = self.create_settings()

        self.assertFalse(settings.update_card.button.isEnabled())
        self.assertNotEqual(settings.update_card.contentLabel.text(), "正在检查更新…")

        self.updates.controller._busy = False
        self.updates.controller.busy_changed.emit(False)
        self.updates.controller.check_finished.emit()
        self.assertTrue(settings.update_card.button.isEnabled())

    def test_unsupported_installation_keeps_the_release_page_fallback(self) -> None:
        with patch.object(update_core, "update_supported", return_value=False):
            settings = self.create_settings()
        self.assertEqual(settings.update_card.button.text(), "发布页")
        self.assertFalse(settings.auto_update_card.isEnabled())


if __name__ == "__main__":
    unittest.main()
