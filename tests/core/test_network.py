"""Tests for the listen-address vocabulary shared by kernel, proxy and UI."""

import ipaddress
import unittest
from unittest import mock

from ferret.core.network import (
    ANY_HOST,
    DEFAULT_PORT,
    LISTEN_HOSTS,
    LOOPBACK_HOST,
    PORT_MAX,
    PORT_MIN,
    detect_lan_address,
    is_lan_exposed,
    normalize_listen_host,
    normalize_listen_port,
)


class ListenHostTests(unittest.TestCase):
    def test_loopback_comes_first_so_bad_values_correct_to_the_safe_one(self) -> None:
        """OptionsValidator.correct 回落到 options[0]，顺序即安全默认值。"""
        self.assertEqual(LISTEN_HOSTS[0], LOOPBACK_HOST)
        self.assertEqual(set(LISTEN_HOSTS), {LOOPBACK_HOST, ANY_HOST})

    def test_only_any_host_exposes_the_lan(self) -> None:
        self.assertTrue(is_lan_exposed(ANY_HOST))
        self.assertFalse(is_lan_exposed(LOOPBACK_HOST))

    def test_unknown_hosts_fall_back_to_loopback(self) -> None:
        for value in ("192.168.1.5", "", "::", "0.0.0.1", None):
            with self.subTest(value=value):
                self.assertEqual(normalize_listen_host(value), LOOPBACK_HOST)

    def test_supported_hosts_pass_through(self) -> None:
        for value in LISTEN_HOSTS:
            with self.subTest(value=value):
                self.assertEqual(normalize_listen_host(value), value)


class ListenPortTests(unittest.TestCase):
    def test_in_range_ports_pass_through(self) -> None:
        for value in (PORT_MIN, 8080, PORT_MAX):
            with self.subTest(value=value):
                self.assertEqual(normalize_listen_port(value), value)

    def test_out_of_range_and_non_integers_fall_back_to_default(self) -> None:
        for value in (0, 80, PORT_MIN - 1, PORT_MAX + 1, "8080", 8080.0, None, []):
            with self.subTest(value=value):
                self.assertEqual(normalize_listen_port(value), DEFAULT_PORT)

    def test_bool_is_not_treated_as_a_port_number(self) -> None:
        """`True` 是 int 的子类，放过去会变成端口 1。"""
        self.assertEqual(normalize_listen_port(True), DEFAULT_PORT)
        self.assertEqual(normalize_listen_port(False), DEFAULT_PORT)


class DetectLanAddressTests(unittest.TestCase):
    def test_returns_a_usable_private_address_or_nothing(self) -> None:
        host = detect_lan_address()
        if host is None:
            self.skipTest("本机没有默认路由，探测不到局域网地址")
        address = ipaddress.ip_address(host)
        self.assertFalse(address.is_loopback)
        self.assertFalse(address.is_unspecified)

    def test_probe_sends_no_packets_to_a_routable_host(self) -> None:
        """探测用 RFC 5737 TEST-NET-1，只查路由表，不能变成真实流量。"""
        with mock.patch("socket.socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = ("192.168.1.9", 54321)
            self.assertEqual(detect_lan_address(), "192.168.1.9")
        target = probe.connect.call_args[0][0]
        self.assertTrue(ipaddress.ip_address(target[0]).is_private)
        probe.send.assert_not_called()
        probe.sendto.assert_not_called()

    def test_unreachable_network_reports_unknown_instead_of_guessing(self) -> None:
        with mock.patch("socket.socket") as factory:
            factory.return_value.__enter__.return_value.connect.side_effect = OSError
            self.assertIsNone(detect_lan_address())

    def test_loopback_result_is_treated_as_no_lan_address(self) -> None:
        with mock.patch("socket.socket") as factory:
            probe = factory.return_value.__enter__.return_value
            probe.getsockname.return_value = (LOOPBACK_HOST, 54321)
            self.assertIsNone(detect_lan_address())


if __name__ == "__main__":
    unittest.main()
