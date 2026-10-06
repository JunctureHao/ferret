from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QtMsgType
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, qconfig

from ferret.core.application import (
    UI_FONT_FAMILY,
    Application,
    _log_unhandled_exception,
    _make_qt_message_filter,
)
from ferret.core.settings import CONFIG, Config

# qfluentwidgets 上游默认值，也是本优化要消掉的那个三族列表。
UPSTREAM_DEFAULT = ["Segoe UI", "Microsoft YaHei", "PingFang SC"]


class DiagnosticTests(unittest.TestCase):
    def test_default_qt_warning_reaches_stderr_and_log(self):
        output = io.StringIO()
        handler = _make_qt_message_filter(None)
        with (
            patch("sys.stderr", output),
            self.assertLogs("ferret", level="WARNING") as logs,
        ):
            handler(QtMsgType.QtWarningMsg, None, "visible warning")
            handler(
                QtMsgType.QtWarningMsg,
                None,
                "QFont::setPointSize: Point size <= 0 (-1)",
            )
        self.assertEqual(output.getvalue(), "visible warning\n")
        self.assertEqual(len(logs.output), 1)

    def test_qt_filter_preserves_real_default_output_in_a_process(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from ferret.core.application import Application; "
                    "from PySide6.QtCore import qWarning; app = Application()._create_qapp(); "
                    "qWarning('FERRET_VISIBLE_QT_WARNING')"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={
                **os.environ,
                "QT_QPA_PLATFORM": "offscreen",
                "QT_FORCE_STDERR_LOGGING": "1",
            },
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FERRET_VISIBLE_QT_WARNING", result.stderr)

    def test_exception_hook_logs_traceback_without_a_console(self):
        try:
            raise RuntimeError("slot failed")
        except RuntimeError:
            exception = sys.exc_info()
        assert exception[0] is not None and exception[1] is not None
        with (
            patch("sys.stderr", None),
            self.assertLogs("ferret", level="ERROR") as logs,
        ):
            _log_unhandled_exception(*exception)
        self.assertIn("RuntimeError: slot failed", logs.output[0])

    def test_non_target_qt_messages_are_forwarded_to_the_previous_handler(self):
        with patch("ferret.core.application.logger") as logger:
            events = []
            handler = _make_qt_message_filter(lambda *args: events.append(args))
            handler(
                QtMsgType.QtCriticalMsg,
                None,
                "QFont::setPointSize: Point size <= 0 (-1)",
            )
            handler(QtMsgType.QtWarningMsg, None, "different warning")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1][2], "different warning")
        logger.log.assert_not_called()

    def test_keyboard_interrupt_keeps_pythons_default_handler(self):
        interrupt = KeyboardInterrupt()
        with (
            patch("sys.__excepthook__") as default_hook,
            patch("ferret.core.application.logger") as logger,
        ):
            _log_unhandled_exception(KeyboardInterrupt, interrupt, None)
        default_hook.assert_called_once_with(KeyboardInterrupt, interrupt, None)
        logger.error.assert_not_called()


class ConfigBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_qfw_theme_updates_use_the_owned_item_and_atomic_save_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config()
            original = config.toDict()
            path.write_text(json.dumps(original), encoding="utf-8")
            themes = []
            qconfig.themeChanged.connect(themes.append)
            try:
                with (
                    patch("ferret.core.application.CONFIG", config),
                    patch("ferret.core.application.get_config_file", return_value=path),
                    patch.object(qconfig, "_cfg", qconfig),
                    patch.object(qconfig, "themeMode", qconfig.themeMode),
                    patch.object(qconfig, "save", qconfig.save),
                ):
                    application = Application()
                    application._init_config()
                    application._init_config()
                    target = (
                        Theme.DARK
                        if config.themeMode.value is not Theme.DARK
                        else Theme.LIGHT
                    )
                    with patch.object(config, "_write", wraps=config._write) as write:
                        qconfig.set(qconfig.themeMode, target)
                    self.assertIs(config.themeMode.value, target)
                    self.assertIs(config.theme, target)
                    self.assertEqual(themes, [target])
                    self.assertEqual(write.call_count, 2)
                    self.assertEqual(
                        json.loads(path.read_text())["QFluentWidgets"]["ThemeMode"],
                        target.value,
                    )
                    previous = path.read_bytes()
                    with (
                        patch.object(
                            Path,
                            "replace",
                            side_effect=PermissionError("replace denied"),
                        ),
                        self.assertRaises(PermissionError),
                    ):
                        qconfig.set(config.ssl_insecure, not config.ssl_insecure.value)
                    self.assertEqual(path.read_bytes(), previous)
            finally:
                qconfig.themeChanged.disconnect(themes.append)
                config.themeChanged.disconnect(qconfig.themeChanged)
                path.write_text(json.dumps(original), encoding="utf-8")
                config.load(path)


class InitFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # ConfigItem 是 QConfig 的共享类属性，改了会漏给同批次其他用例。
        self._original = CONFIG.get(CONFIG.fontFamilies)

    def tearDown(self) -> None:
        CONFIG.set(CONFIG.fontFamilies, self._original, save=False)

    def test_collapses_ui_font_to_single_family(self) -> None:
        CONFIG.set(CONFIG.fontFamilies, UPSTREAM_DEFAULT, save=False)
        Application()._init_font()
        self.assertEqual(CONFIG.get(CONFIG.fontFamilies), [UI_FONT_FAMILY])

    def test_does_not_write_config_file(self) -> None:
        """`save=False` 是刻意的：字体策略不落盘，方便以后改默认值。

        先塞一个不同的值，否则 `QConfig.set` 开头的 `if item.value == value: return`
        会让这条用例空跑通过。
        """
        CONFIG.set(CONFIG.fontFamilies, UPSTREAM_DEFAULT, save=False)
        with patch.object(CONFIG, "save") as save:
            Application()._init_font()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
