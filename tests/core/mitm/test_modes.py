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

from ferret.core.mitm.bindings import ProxyMode
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
            capture_mode_specs(use_local=True, local_spec="curl", use_wireguard=True),
            ["regular", "local:curl@127.0.0.1:0", wireguard_mode_spec()],
        )

    def test_local_spec_is_stripped_and_empty_means_everything(self) -> None:
        self.assertEqual(local_mode_spec(""), "local@127.0.0.1:0")
        self.assertEqual(local_mode_spec("  "), "local@127.0.0.1:0")
        self.assertEqual(
            local_mode_spec(" curl , python "), "local:curl , python@127.0.0.1:0"
        )

    def test_local_spec_carries_the_duplicate_address_dodge(self) -> None:
        """上游 #7063：查重把全局默认端口顶进 local（default_port=None 被覆盖），
        裸 ``local`` 会被算成与 regular 同听 ``*:8080`` 而拒启动。显式
        ``@127.0.0.1:0`` 让查重读到唯一地址；该地址永远不会被真正绑定
        （``LocalRedirectorInstance.listen_addrs = ()``）。这里用上游同款查重
        算法钉住回归。
        """
        from ferret.core.mitm.modes import LOCAL_DUP_DODGE

        for listen_host in ("127.0.0.1", "0.0.0.0"):
            for listen_port in (8080, 9123):
                specs = capture_mode_specs(
                    use_local=True, local_spec="curl,!1234", use_wireguard=True
                )
                addrs: list[tuple] = []
                for spec in specs:
                    mode = ProxyMode.parse(spec)
                    protocols = (
                        ["tcp", "udp"]
                        if mode.transport_protocol == "both"
                        else [mode.transport_protocol]
                    )
                    port = mode.listen_port(listen_port)
                    if port is None:
                        continue
                    addrs.extend(
                        (mode.listen_host(listen_host), port, proto)
                        for proto in protocols
                    )
                with self.subTest(listen_host=listen_host, listen_port=listen_port):
                    self.assertEqual(len(addrs), len(set(addrs)), addrs)
        # 占位符确实长在 local spec 上，且解析后 data 不受影响。
        mode = ProxyMode.parse(local_mode_spec("curl"))
        self.assertTrue(mode.full_spec.endswith(LOCAL_DUP_DODGE))
        self.assertEqual(mode.data, "curl")

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
