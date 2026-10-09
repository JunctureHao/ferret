from __future__ import annotations

import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmFacade, MitmRuntime, WireGuardDevice

from ._qt import start_runtime, wait_until
from .test_runtime import free_port


def free_udp_ports(count: int) -> list[int]:
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class WireGuardRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.certs = Path(directory.name)
        for module in ("runtime", "facade", "certificate"):
            patcher = patch(
                f"ferret.core.mitm.{module}.get_certs_dir", return_value=self.certs
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        a, b, self.extra_port = free_udp_ports(3)
        self.devices = [
            WireGuardDevice("phone-a", "Phone A", port=a),
            WireGuardDevice("phone-b", "Phone B", port=b),
        ]
        self.runtime = MitmRuntime(
            listen_port=free_port(),
            use_wireguard=True,
            wireguard_devices=self.devices,
            block_private=True,
        )
        self.addCleanup(self.runtime.stop)
        self.facade = MitmFacade(self.runtime)
        start_runtime(self.runtime)

    def engage(self) -> None:
        self.runtime.set_channels_engaged(True)
        self.assertTrue(
            wait_until(lambda: self.facade.channel_health().get("wireguard") is True)
        )

    def instances(self) -> dict[str, int]:
        def snapshot():
            master = self.runtime.master
            assert master is not None
            devices = self.runtime._wireguard_specs()
            return {
                devices[server.mode.full_spec].id: id(server)
                for server in master.proxyserver.servers
                if server.mode.full_spec in devices
            }

        return self.runtime.call(snapshot)

    def assert_port_released(self, port: int) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("0.0.0.0", port))

    def test_two_listeners_start_only_on_capture_and_export_distinct_keys(self) -> None:
        self.assertEqual(self.instances(), {})
        profiles = [self.facade.wireguard_client_config(d.id) for d in self.devices]
        for profile, device in zip(profiles, self.devices, strict=True):
            self.assertIn(f":{device.port}", profile)
            self.assertIn("Address = 10.0.0.1/32", profile)
            self.assert_port_released(device.port)
        private_lines = [
            next(line for line in p.splitlines() if line.startswith("PrivateKey"))
            for p in profiles
        ]
        self.assertNotEqual(*private_lines)
        self.engage()
        self.assertEqual(
            self.facade.wireguard_device_health(), {"phone-a": True, "phone-b": True}
        )
        self.assertEqual(len(set(self.instances().values())), 2)
        self.runtime.set_channels_engaged(False)
        for device in self.devices:
            self.assert_port_released(device.port)
        self.assertEqual(self.instances(), {})
        master = self.runtime.master
        assert master is not None
        self.assertTrue(self.runtime.call(lambda: master.options.block_private))

    def test_add_rename_rotate_and_remove_preserve_other_instance(self) -> None:
        self.engage()
        original = self.instances()
        new = WireGuardDevice("phone-c", "Phone C", port=self.extra_port)
        self.facade.set_wireguard_devices([*self.devices, new])
        after_add = self.instances()
        self.assertEqual({key: after_add[key] for key in original}, original)
        a = replace(self.devices[0], name="Renamed A")
        self.facade.set_wireguard_devices([a, self.devices[1], new])
        self.assertEqual(self.instances(), after_add)
        before_key = self.facade.wireguard_client_config(a.id)
        self.facade.rotate_wireguard_device(a.id)
        after_rotate = self.instances()
        self.assertEqual(after_rotate["phone-b"], original["phone-b"])
        self.assertEqual(after_rotate["phone-c"], after_add["phone-c"])
        self.assertNotEqual(self.facade.wireguard_client_config(a.id), before_key)
        self.assertEqual(self.facade.wireguard_devices[0].key_revision, 1)
        self.facade.set_wireguard_devices([self.devices[1], new])
        self.assertEqual(self.instances()["phone-b"], original["phone-b"])
        self.assert_port_released(a.port)

    def test_occupied_new_port_rolls_back_registry_options_and_preserves_peers(
        self,
    ) -> None:
        self.engage()
        original = self.instances()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as blocker:
            blocker.bind(("0.0.0.0", 0))
            bad = WireGuardDevice("blocked", "Blocked", port=blocker.getsockname()[1])
            with self.assertRaisesRegex(ValueError, "Blocked"):
                self.facade.set_wireguard_devices([*self.devices, bad])
        self.assertEqual(self.facade.wireguard_devices, self.devices)
        self.assertEqual(self.instances(), original)
        self.assertEqual(self.facade.channel_health()["wireguard"], True)

    def test_empty_devices_reinstate_auth_and_do_not_recreate_default(self) -> None:
        self.runtime.apply_proxy_auth(enabled=True, username="user", password="pass")
        self.engage()
        self.facade.set_wireguard_devices([])
        master = self.runtime.master
        assert master is not None
        self.assertEqual(
            self.runtime.call(lambda: master.options.proxyauth), "user:pass"
        )
        self.assertTrue(self.runtime.call(lambda: master.options.block_private))
        self.assertEqual(self.instances(), {})
        with self.assertRaises(ValueError):
            self.facade.wireguard_client_config()
        self.runtime.set_channels_engaged(False)
        self.runtime.set_channels_engaged(True)
        self.assertEqual(self.instances(), {})

    def test_global_sticky_session_cannot_be_enabled_with_multiple_devices(
        self,
    ) -> None:
        self.engage()
        with self.assertRaises(ValueError):
            self.runtime.apply_sticky_session(True)
        self.assertFalse(self.runtime.sticky_session_enabled)
        self.facade.set_wireguard_devices(self.devices[:1])
        self.runtime.apply_sticky_session(True)
        with self.assertRaises(ValueError):
            self.facade.set_wireguard_devices(self.devices)
        self.assertEqual(self.facade.wireguard_devices, self.devices[:1])
        self.assertTrue(self.facade.wireguard_device_health()["phone-a"])

    def test_missing_expected_instance_is_reported_in_aggregate_health(self) -> None:
        self.engage()
        master = self.runtime.master
        assert master is not None
        original = master.proxyserver.servers
        running = list(original)
        expected = self.runtime._wireguard_specs()

        # Evaluate the immutable observation on the owning thread without
        # disturbing the live listeners or relying on a race in native startup.
        devices = self.devices

        class Snapshot:
            is_updating = False

            def __iter__(self):
                return iter(
                    s for s in running if expected.get(s.mode.full_spec) != devices[1]
                )

        def inspect():
            with patch.object(master.proxyserver, "servers", Snapshot()):
                return self.runtime.channel_health()

        health = self.runtime.call(inspect)
        self.assertIsInstance(health["wireguard"], str)
        self.assertIn("Phone B", str(health["wireguard"]))
