"""Tests for capture channel specs (core/mitm/modes.py).

三条通道的 spec 是界面与内核的唯一接口：这里守住拼装格式、坏值拦截与
WireGuard 客户端配置的生成格式（对齐上游 ``WireGuardServerInstance.client_conf``）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.core.mitm.modes import (
    WIREGUARD_PORT,
    capture_mode_specs,
    local_mode_spec,
    validate_local_spec,
    validate_mode_specs,
    wireguard_client_config,
    wireguard_mode_spec,
)


class CaptureModeSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])

    def test_regular_is_always_the_first_spec(self) -> None:
        """regular 是常驻底盘：通道全关时 mode 也必须只有它。"""
        self.assertEqual(
            capture_mode_specs(use_local=False, local_spec="", use_wireguard=False),
            ["regular"],
        )

    def test_enabled_channels_are_appended_in_order(self) -> None:
        self.assertEqual(
            capture_mode_specs(
                use_local=True, local_spec="curl", use_wireguard=True
            ),
            ["regular", "local:curl", wireguard_mode_spec()],
        )

    def test_local_spec_is_stripped_and_empty_means_everything(self) -> None:
        self.assertEqual(local_mode_spec(""), "local")
        self.assertEqual(local_mode_spec("  "), "local")
        self.assertEqual(local_mode_spec(" curl , python "), "local:curl , python")

    def test_wireguard_spec_binds_lan_reachable_udp_port(self) -> None:
        """VPN 端点必须绑 0.0.0.0：环回绑定让手机永远连不上。"""
        self.assertEqual(wireguard_mode_spec(), f"wireguard@0.0.0.0:{WIREGUARD_PORT}")

    def test_validate_accepts_the_full_trio(self) -> None:
        validate_mode_specs(
            ["regular", "local:curl,!1234", wireguard_mode_spec()]
        )

    def test_validate_rejects_unknown_modes_and_bad_local_filters(self) -> None:
        for bad in ("bogus:xyz", "local:a,,b", "local:!"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_mode_specs(["regular", bad])

    def test_validate_local_spec_is_the_dialog_entry_point(self) -> None:
        validate_local_spec("curl")
        with self.assertRaises(ValueError):
            validate_local_spec(",,,")


class WireGuardClientConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])
        from mitmproxy_rs import wireguard as rs_wireguard

        cls.rs_wireguard = rs_wireguard

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.conf_path = Path(self._dir.name) / "wireguard.conf"
        self.server_key = self.rs_wireguard.genkey()
        self.client_key = self.rs_wireguard.genkey()
        self.conf_path.write_text(
            json.dumps({"server_key": self.server_key, "client_key": self.client_key}),
            encoding="utf-8",
        )

    def test_generated_profile_mirrors_the_upstream_format(self) -> None:
        config = wireguard_client_config(self.conf_path, "192.168.1.9")
        self.assertIn(f"PrivateKey = {self.client_key}", config)
        self.assertIn("Address = 10.0.0.1/32", config)
        self.assertIn("DNS = 10.0.0.53", config)
        self.assertIn(
            f"PublicKey = {self.rs_wireguard.pubkey(self.server_key)}", config
        )
        self.assertIn("AllowedIPs = 0.0.0.0/0", config)
        self.assertIn(f"Endpoint = 192.168.1.9:{WIREGUARD_PORT}", config)

    def test_missing_lan_address_falls_back_to_loopback(self) -> None:
        """探测不到局域网地址时退回环回：同机测试仍可用，也不显示假地址。"""
        config = wireguard_client_config(self.conf_path, None)
        self.assertIn(f"Endpoint = 127.0.0.1:{WIREGUARD_PORT}", config)

    def test_missing_conf_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wireguard_client_config(Path(self._dir.name) / "nope.conf", None)

    def test_corrupt_conf_file_raises(self) -> None:
        self.conf_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            wireguard_client_config(self.conf_path, None)


if __name__ == "__main__":
    unittest.main()
