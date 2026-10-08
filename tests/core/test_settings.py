"""bool 配置项的坏值回落测试（issues #88）。

qfw 的 ``BoolValidator`` 是 ``OptionsValidator([True, False])``，`correct` 把不在
options 里的值修成 ``options[0]`` —— 恰好是 True：JSON 里的 null、``"false"``、0
加载时会把默认关闭的开关顶开（ssl_insecure 关掉上游 TLS 校验、scripts_enabled
直接装载脚本）。`BoolConfigItem` 收紧为「坏值回各项默认」，这里用真实
`QConfig.load` 链路钉住（``deserializeFrom`` → value setter → validator.correct）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
from qfluentwidgets import ConfigItem

from ferret.core.settings import BoolConfigItem, Config

from .mitm._qt import wait_until

app = QApplication.instance() or QApplication([])


class ConfigRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.config = Config()
        self.config.file = self.path
        self.original = self.config.toDict()
        self.addCleanup(self._restore_values)

    def _restore_values(self):
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.config.load(self.path)

    def test_partial_json_write_keeps_previous_config(self):
        self.config.save()
        before = self.path.read_bytes()

        def fail(data, stream, **kwargs):
            stream.write("{")
            raise OSError("disk full")

        with (
            patch("ferret.core.settings.json.dump", side_effect=fail),
            self.assertRaises(OSError),
        ):
            self.config.save()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".config*")), [])

    def test_failed_primary_replace_keeps_old_config_and_a_valid_backup(self) -> None:
        self.config.save()
        before = self.path.read_bytes()
        self.config.set(self.config.flow_columns, {"width": 315}, save=False)
        replace = Path.replace

        def fail_primary(source: Path, target):
            if target == self.path:
                raise PermissionError("replace denied")
            return replace(source, target)

        with (
            patch.object(Path, "replace", fail_primary),
            self.assertRaises(PermissionError),
        ):
            self.config.save()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob(".config*")), [])
        self.config.flush_pending_save()
        self.assertEqual(
            json.loads(self.path.read_text())["FlowList"]["Columns"], {"width": 315}
        )

    def test_damaged_primary_recovers_backup_and_preserves_evidence(self):
        self.config.save()
        self.config.save()
        before = self.path.read_bytes()
        self.path.write_bytes(b"{")
        self.config.load(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.config.load_warnings[0][0], "recovered")
        copies = list(self.path.parent.glob("config.json.corrupt-*"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), b"{")

    def test_no_backup_keeps_corrupt_source_before_next_save(self):
        self.path.write_bytes(b"{")
        self.config.load(self.path)
        self.assertEqual(self.path.read_bytes(), b"{")
        self.assertEqual(self.config.load_warnings[0][0], "defaults")
        self.config.save()
        self.assertIsInstance(json.loads(self.path.read_text(encoding="utf-8")), dict)
        self.assertEqual(
            next(self.path.parent.glob("config.json.corrupt-*")).read_bytes(), b"{"
        )

    def test_readable_backup_is_used_when_repairing_the_primary_fails(self) -> None:
        self.config.set(self.config.ssl_insecure, True, save=False)
        self.config.save()
        self.config.save()
        self.path.write_bytes(b"{")
        with patch.object(
            self.config, "_write", side_effect=PermissionError("read only")
        ):
            self.config.load(self.path)
        self.assertIs(self.config.ssl_insecure.value, True)
        self.assertEqual(self.config.load_warnings[0][0], "backup")
        self.assertEqual(self.path.read_bytes(), b"{")
        self.assertIs(
            json.loads(self.path.with_suffix(".json.bak").read_text())["Proxy"][
                "SslInsecure"
            ],
            True,
        )

    def test_missing_primary_uses_backup_but_first_launch_has_no_warning(self) -> None:
        self.config.load(self.path)
        self.assertEqual(self.config.load_warnings, [])
        self.config.set(self.config.ssl_insecure, True, save=False)
        self.config.save()
        self.config.save()
        self.path.unlink()
        self.config.load(self.path)
        self.assertIs(self.config.ssl_insecure.value, True)
        self.assertEqual(self.config.load_warnings[0][0], "recovered")
        self.assertTrue(self.path.exists())

    def test_invalid_enum_uses_default_and_reports_the_field_at_display_time(
        self,
    ) -> None:
        item = self.config.language
        self.path.write_text(
            json.dumps({item.group: {item.name: "bad-locale"}}), encoding="utf-8"
        )
        self.config.load(self.path)
        self.assertEqual(item.value, item.defaultValue)
        self.assertIn(("field", item.key), self.config.load_warnings)
        with patch.object(
            QCoreApplication, "translate", return_value="Translated field {}"
        ):
            self.assertIn(
                f"Translated field {item.key}", self.config.recovery_messages()
            )

    def test_continuous_deferred_changes_write_once(self) -> None:
        with patch.object(self.config, "save", wraps=self.config.save) as save:
            for width in range(20):
                self.config.set(self.config.flow_columns, {"width": width}, save=False)
                self.config.defer_save()
            timer = self.config._save_timer
            assert timer is not None
            save.assert_not_called()
            timer.setInterval(1)
            self.assertTrue(wait_until(self.path.exists))
            save.assert_called_once()
            self.assertFalse(timer.isActive())
            self.config.flush_pending_save()
            save.assert_called_once()
        self.assertEqual(
            json.loads(self.path.read_text())["FlowList"]["Columns"], {"width": 19}
        )

    def test_deferred_write_failure_is_retried_by_exit_flush(self) -> None:
        self.config.save()
        before = self.path.read_bytes()
        self.config.set(self.config.flow_columns, {"width": 500}, save=False)
        self.config.defer_save()
        timer = self.config._save_timer
        assert timer is not None
        timer.setInterval(1)
        with (
            patch.object(
                self.config, "_write", side_effect=PermissionError("disk full")
            ),
            patch("sys.excepthook") as exception_hook,
        ):
            self.assertTrue(wait_until(lambda: exception_hook.call_count > 0))
        self.assertFalse(timer.isActive())
        self.assertEqual(self.path.read_bytes(), before)
        self.config.flush_pending_save()
        self.assertEqual(
            json.loads(self.path.read_text())["FlowList"]["Columns"], {"width": 500}
        )

    def test_application_quit_flushes_before_the_debounce_deadline(self) -> None:
        program = """
import sys
from pathlib import Path
from PySide6.QtCore import QCoreApplication, QTimer
from ferret.core.settings import Config
app = QCoreApplication([])
config = Config()
config.file = Path(sys.argv[1])
config.set(config.flow_columns, {"width": 725}, save=False)
config.defer_save()
QTimer.singleShot(0, app.quit)
app.exec()
"""
        result = subprocess.run(
            [sys.executable, "-c", program, str(self.path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(self.path.read_text())["FlowList"]["Columns"], {"width": 725}
        )


class BoolConfigItemLoadTests(unittest.TestCase):
    def _load(self, overrides: dict) -> Config:
        config = Config()
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(overrides, stream)
        config.load(path)
        return config

    def test_null_falls_back_to_each_items_default(self) -> None:
        config = self._load(
            {"Proxy": {"SslInsecure": None, "SystemProxyEnabled": None}}
        )
        self.assertIs(config.ssl_insecure.value, False)
        # 默认开的项坏值回 True（各项默认），不是一刀切 False。
        self.assertIs(config.system_proxy_enabled.value, True)

    def test_wrong_typed_values_fall_back_to_defaults(self) -> None:
        config = self._load(
            {
                "Proxy": {"LocalEnabled": "false", "BlockPrivate": 0},
                "Scripts": {"Enabled": []},
            }
        )
        self.assertIs(config.local_enabled.value, False)
        self.assertIs(config.block_private.value, False)
        self.assertIs(config.scripts_enabled.value, False)

    def test_legal_bool_values_pass_through(self) -> None:
        config = self._load({"Proxy": {"SslInsecure": True}, "Mock": {"Reuse": False}})
        self.assertIs(config.ssl_insecure.value, True)
        self.assertIs(config.mock_reuse.value, False)

    def test_reset_to_defaults_restores_every_item_and_persists(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = Config()
        config.file = Path(directory.name) / "config.json"
        config.set(config.ssl_insecure, True, save=False)
        config.set(config.dns_name_servers, ["1.1.1.1"], save=False)
        config.set(config.flow_columns, {"width": 315}, save=False)
        config.reset_to_defaults()
        for name in dir(Config):
            item = getattr(Config, name)
            if not isinstance(item, ConfigItem):
                continue
            with self.subTest(item=name):
                self.assertEqual(item.value, item.defaultValue)
                if isinstance(item.defaultValue, (list, dict)):
                    self.assertIsNot(item.value, item.defaultValue)
        on_disk = Config()
        on_disk.load(config.file)
        self.assertIs(on_disk.ssl_insecure.value, False)
        self.assertEqual(on_disk.dns_name_servers.value, [])

    def test_reset_to_defaults_emits_value_changed_for_changed_items(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = Config()
        config.file = Path(directory.name) / "config.json"
        config.set(config.http2_enabled, False, save=False)
        seen: list[bool] = []
        config.http2_enabled.valueChanged.connect(seen.append)
        config.reset_to_defaults()
        self.assertEqual(seen, [True])

    def test_every_bool_item_declares_a_bool_default(self) -> None:
        """防止再有人用 `ConfigItem` + 裸 validator 定义 bool 项绕过收紧。"""
        for name in dir(Config):
            item = getattr(Config, name)
            if not isinstance(item, BoolConfigItem):
                continue
            with self.subTest(item=name):
                self.assertIsInstance(item.defaultValue, bool)
