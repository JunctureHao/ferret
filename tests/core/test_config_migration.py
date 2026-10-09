from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core.settings import _migrate_legacy_config_dir


class ConfigMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.old = root / "Local" / "Ferret"
        self.new = root / "Roaming" / "Ferret"
        self.old.mkdir(parents=True)
        location = patch(
            "ferret.core.settings.QStandardPaths.writableLocation",
            return_value=str(self.old),
        )
        location.start()
        self.addCleanup(location.stop)

    def write_config(self, path: Path, entries: object) -> dict:
        data = {"Scripts": {"Scripts": entries}, "Unrelated": {"preserve": [1, 2]}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def write_script(self, path: Path, text: str = "value = 1\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_moved_scripts_are_relocated_without_changing_external_entries(self):
        managed = self.old / "scripts" / "demo.py"
        external = self.old.parent / "external.py"
        self.write_script(managed)
        self.write_script(external)
        entries = [
            {"origin": "new", "path": str(managed), "enabled": False},
            {"origin": "import", "path": str(external)},
            {"origin": "new", "path": str(external)},
            {"origin": "import", "path": str(managed)},
        ]
        expected = self.write_config(self.old / "config.json", entries)
        _migrate_legacy_config_dir(self.new)
        target = self.new / "scripts" / "demo.py"
        self.assertTrue(target.is_file())
        self.assertFalse(managed.exists())
        expected["Scripts"]["Scripts"][0]["path"] = str(target)
        path = self.new / "config.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
        before = path.read_bytes()
        _migrate_legacy_config_dir(self.new)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_target_script_does_not_replace_the_old_reference(self):
        source = self.old / "scripts" / "demo.py"
        target = self.new / "scripts" / "demo.py"
        self.write_script(source, "old = True\n")
        self.write_script(target, "new = True\n")
        expected = self.write_config(
            self.old / "config.json", [{"origin": "new", "path": str(source)}]
        )
        _migrate_legacy_config_dir(self.new)
        self.assertEqual(
            json.loads((self.new / "config.json").read_text(encoding="utf-8")),
            expected,
        )
        self.assertEqual(source.read_text(encoding="utf-8"), "old = True\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "new = True\n")

    def test_failed_script_move_keeps_its_reference(self):
        source = self.old / "scripts" / "demo.py"
        self.write_script(source)
        expected = self.write_config(
            self.old / "config.json", [{"origin": "new", "path": str(source)}]
        )
        move = shutil.move

        def fail_scripts(src: str, dst: str):
            if Path(src).name == "scripts":
                raise PermissionError("script directory is locked")
            return move(src, dst)

        with (
            patch("ferret.core.settings.shutil.move", side_effect=fail_scripts),
            self.assertLogs("ferret.settings", level="WARNING"),
        ):
            _migrate_legacy_config_dir(self.new)
        self.assertTrue(source.is_file())
        self.assertEqual(
            json.loads((self.new / "config.json").read_text(encoding="utf-8")),
            expected,
        )

    def test_atomic_write_failure_retries_after_the_old_directory_is_gone(self):
        source = self.old / "scripts" / "demo.py"
        self.write_script(source)
        self.write_config(
            self.old / "config.json", [{"origin": "new", "path": str(source)}]
        )
        before = (self.old / "config.json").read_bytes()
        with (
            patch.object(Path, "replace", side_effect=PermissionError("locked")),
            self.assertLogs("ferret.settings", level="WARNING"),
        ):
            _migrate_legacy_config_dir(self.new)
        path = self.new / "config.json"
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.new.glob(".config.json.*")), [])
        self.old.rmdir()
        _migrate_legacy_config_dir(self.new)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            data["Scripts"]["Scripts"][0]["path"],
            str(self.new / "scripts" / "demo.py"),
        )

    def test_existing_backup_is_repaired_while_corrupt_primary_is_preserved(self):
        source = self.old / "scripts" / "demo.py"
        target = self.new / "scripts" / "demo.py"
        self.write_script(target)
        primary = self.new / "config.json"
        primary.write_bytes(b"{")
        backup = self.new / "config.json.bak"
        expected = self.write_config(backup, [{"origin": "new", "path": str(source)}])
        _migrate_legacy_config_dir(self.new)
        self.assertEqual(primary.read_bytes(), b"{")
        expected["Scripts"]["Scripts"][0]["path"] = str(target)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), expected)

    def test_malformed_and_nonabsolute_entries_are_preserved(self):
        target = self.new / "scripts" / "demo.py"
        self.write_script(target)
        entries = [
            None,
            1,
            {"origin": "new", "path": None},
            {"origin": "new", "path": "scripts/demo.py"},
            {"origin": "new", "path": str(self.old / "scripts" / "bad\0.py")},
            {"origin": "new", "path": str(self.old / "scripts" / "missing.py")},
            {"origin": "new", "path": str(self.old / "scripts" / ".." / "demo.py")},
        ]
        path = self.new / "config.json"
        expected = self.write_config(path, entries)
        _migrate_legacy_config_dir(self.new)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
        for malformed in (None, {}, "bad"):
            with self.subTest(entries=malformed):
                expected = self.write_config(path, malformed)
                _migrate_legacy_config_dir(self.new)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
