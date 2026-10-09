"""Device registry, independent credentials, and compatible mode migration."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core.mitm.bindings import ProxyMode
from ferret.core.mitm.modes import (
    capture_mode_specs,
    ensure_wireguard_conf,
    wireguard_client_config,
    wireguard_mode_spec,
)
from ferret.core.mitm.wireguard import (
    WireGuardDevice,
    validate_wireguard_devices,
    wireguard_devices_from_config,
    wireguard_devices_to_config,
)


class WireGuardDeviceTests(unittest.TestCase):
    def test_missing_setting_migrates_legacy_but_empty_stays_empty(self) -> None:
        legacy = wireguard_devices_from_config(None)
        self.assertEqual(legacy, [WireGuardDevice(id="default", name="")])
        self.assertEqual(legacy[0].port, 51820)
        self.assertEqual(legacy[0].display_name, "默认设备")
        self.assertEqual(
            legacy[0].key_path(Path("certs")), Path("certs/wireguard.conf")
        )
        self.assertEqual(wireguard_devices_from_config([]), [])

    def test_roundtrip_has_no_credentials_and_preserves_identity(self) -> None:
        devices = [
            WireGuardDevice("phone-a", "手机 A"),
            WireGuardDevice("phone-b", "手机 B", 51821, False, 2),
        ]
        saved = wireguard_devices_to_config(devices)
        self.assertEqual(
            wireguard_devices_from_config(json.loads(json.dumps(saved))), devices
        )
        self.assertEqual(
            set(saved[0]), {"id", "name", "port", "enabled", "key_revision"}
        )
        self.assertNotEqual(
            devices[0].key_path(Path("certs")), devices[1].key_path(Path("certs"))
        )
        with self.assertRaises(FrozenInstanceError):
            devices[0].port = 42  # ty: ignore[invalid-assignment]

    def test_key_rotation_changes_path_without_changing_device_id(self) -> None:
        for device in wireguard_devices_from_config(None) + [
            WireGuardDevice("phone-a", "A")
        ]:
            rotated = replace(device, key_revision=device.key_revision + 1)
            self.assertEqual(device.id, rotated.id)
            self.assertNotEqual(
                device.key_path(Path("certs")), rotated.key_path(Path("certs"))
            )
            self.assertEqual(
                rotated.key_path(Path("certs")).parent, Path("certs/wireguard")
            )

    def test_invalid_records_cannot_derive_paths(self) -> None:
        device = WireGuardDevice("phone-a", "A")
        invalid_fields = {
            "id": ["../escape", "..", "C:escape", "UPPER", "", "a" * 65, 4],
            "name": ["", "  ", None],
            "port": [0, 65536, True, "51820", 1.5],
            "enabled": [0, 1, "false", None],
            "key_revision": [-1, True, "0", 1.5],
        }
        for field, values in invalid_fields.items():
            for value in values:
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaises(ValueError),
                ):
                    replace(device, **{field: value}).key_path(Path("certs"))

    def test_disabled_devices_cannot_share_ids_or_ports(self) -> None:
        device = WireGuardDevice("phone-a", "A")
        for duplicate in (
            replace(device, enabled=False, port=51821),
            replace(device, enabled=False, id="phone-b"),
        ):
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError):
                validate_wireguard_devices([device, duplicate])

    def test_invalid_config_is_rejected_without_silently_skipping_entries(self) -> None:
        valid = WireGuardDevice("phone-a", "A").to_dict()
        for value in ({}, "[]", [None], [valid, {}], [valid, {**valid, "enabled": 1}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                wireguard_devices_from_config(value)


class WireGuardIndependentModeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="wg test @ ")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_multiple_profiles_have_distinct_keys_ports_and_native_modes(self) -> None:
        devices = [
            WireGuardDevice("phone-a", "A"),
            WireGuardDevice("phone-b", "B", 51821),
        ]
        specs = []
        keys = []
        for device in devices:
            path = device.key_path(self.directory)
            ensure_wireguard_conf(path)
            config = wireguard_client_config(path, "192.168.1.5", device.port)
            self.assertIn(f"Endpoint = 192.168.1.5:{device.port}", config)
            self.assertIn("Address = 10.0.0.1/32", config)
            keys.append(json.loads(path.read_text(encoding="utf-8")))
            spec = wireguard_mode_spec(path, device.port)
            parsed = ProxyMode.parse(spec)
            self.assertEqual(parsed.data, str(path.absolute()))
            self.assertEqual(parsed.listen_port(), device.port)
            specs.append(spec)
        self.assertNotEqual(keys[0]["server_key"], keys[1]["server_key"])
        self.assertNotEqual(keys[0]["client_key"], keys[1]["client_key"])
        self.assertEqual(
            capture_mode_specs(
                use_local=False,
                local_spec="",
                use_wireguard=True,
                wireguard_specs=specs,
            ),
            ["regular", *specs],
        )

    def test_explicit_empty_specs_do_not_recreate_legacy_listener(self) -> None:
        self.assertEqual(
            capture_mode_specs(
                use_local=False, local_spec="", use_wireguard=True, wireguard_specs=[]
            ),
            ["regular"],
        )
        self.assertEqual(
            capture_mode_specs(
                use_local=False,
                local_spec="",
                use_wireguard=False,
                wireguard_specs=[wireguard_mode_spec()],
            ),
            ["regular"],
        )

    def test_failed_key_write_never_publishes_partial_credentials(self) -> None:
        path = self.directory / "new.conf"

        def fail_write(data, stream, **kwargs):
            stream.write("{")
            raise OSError("disk full")

        with (
            patch("ferret.core.mitm.modes.json.dump", side_effect=fail_write),
            self.assertRaises(OSError),
        ):
            ensure_wireguard_conf(path)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_existing_key_wins_atomic_publish_race(self) -> None:
        path = self.directory / "race.conf"
        winner = self.directory / "winner.conf"
        ensure_wireguard_conf(winner)
        existing = winner.read_bytes()
        link = os.link

        def race(source, destination):
            path.write_bytes(existing)
            link(source, destination)

        with patch("ferret.core.mitm.modes.os.link", side_effect=race):
            ensure_wireguard_conf(path)
        self.assertEqual(path.read_bytes(), existing)
        self.assertEqual(
            sorted(p.name for p in self.directory.iterdir()),
            ["race.conf", "winner.conf"],
        )

    def test_corrupt_client_key_is_not_exported_or_replaced(self) -> None:
        path = self.directory / "corrupt.conf"
        ensure_wireguard_conf(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["client_key"] = "secret-malformed-client-key"
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        for operation in (
            lambda: ensure_wireguard_conf(path),
            lambda: wireguard_client_config(path, None),
        ):
            with self.assertRaises(ValueError) as raised:
                operation()
            self.assertNotIn(data["client_key"], str(raised.exception))
            self.assertNotIn(data["server_key"], str(raised.exception))
        self.assertEqual(path.read_bytes(), before)
