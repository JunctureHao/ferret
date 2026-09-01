"""Tests for capture channel specs (core/mitm/modes.py).

三条通道的 spec 是界面与内核的唯一接口：这里守住拼装格式、坏值拦截、
WireGuard 客户端配置的生成格式（对齐上游 ``WireGuardServerInstance.client_conf``），
以及 local 提权守护进程在内核停止/重启时的拆除时机。
"""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.bindings import LocalRedirectorInstance, ProxyMode
from ferret.core.mitm.modes import (
    WIREGUARD_PORT,
    capture_mode_specs,
    local_mode_spec,
    validate_local_spec,
    validate_mode_specs,
    wireguard_client_config,
    wireguard_mode_spec,
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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


class LocalRedirectorDisarmTests(unittest.TestCase):
    """守护进程是**进程外**服务：内核停止/重启时必须同步拔掉截流配置。

    正常解除走 ``Servers.update`` 的异步 stop 任务；但事件循环关闭会静默丢弃
    挂起任务（抓包中改端口/地址触发的 restart 必然如此），守护进程带着旧 spec
    继续截流，而新内核的 mode 列表里没有 local 时再没有谁会去清它——界面症状
    就是「关了本地重定向还能抓到本机流量」。这里用桩替换上游类属性
    ``LocalRedirectorInstance._server``（守护进程连接的真实接缝），确定性验证
    disarm 时机，全程不触碰真守护进程、不弹 UAC。
    """

    class _StubDaemon:
        def __init__(self) -> None:
            self.specs: list[str] = []

        def set_intercept(self, spec: str) -> None:
            self.specs.append(spec)

    @classmethod
    def setUpClass(cls) -> None:
        QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.stub = self._StubDaemon()
        self._original_server = LocalRedirectorInstance._server
        self._original_instance = LocalRedirectorInstance._instance
        # 测试桩只实现 set_intercept，disarm 路径也只调它——运行期鸭子类型足够。
        LocalRedirectorInstance._server = self.stub  # ty: ignore
        LocalRedirectorInstance._instance = None
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        LocalRedirectorInstance._server = self._original_server
        LocalRedirectorInstance._instance = self._original_instance

    def wait_for_signal(self, signal, timeout_ms: int = 5000):
        loop = QEventLoop()
        values = []

        def receive(*args):
            values.append(args)
            loop.quit()

        signal.connect(receive)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        signal.disconnect(receive)
        return values

    def _wait_for(self, predicate, timeout_s: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def _make_runtime(self) -> MitmRuntime:
        runtime = MitmRuntime(
            listen_port=free_port(), use_local=True, use_wireguard=False
        )
        self.addCleanup(runtime.stop)
        return runtime

    def _engage_and_wait(self, runtime: MitmRuntime) -> None:
        runtime.set_channels_engaged(True)
        # _start 在 loop 上异步执行；stub 收到 spec 即守护进程被接管。
        # data 为空时 spec 恒为「排除自身 PID」。
        self.assertTrue(
            self._wait_for(lambda: self.stub.specs),
            "daemon never received the intercept spec",
        )
        self.assertEqual(self.stub.specs[-1], f"!{os.getpid()}")

    def test_stop_disarms_the_daemon(self) -> None:
        runtime = self._make_runtime()
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))
        self._engage_and_wait(runtime)

        runtime.stop()

        self.assertEqual(self.stub.specs[-1], "")

    def test_disengage_clears_the_daemon_through_the_async_path(self) -> None:
        """内核存活时的正常解除：异步 stop 任务清 spec，guard 住这条正路。"""
        runtime = self._make_runtime()
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))
        self._engage_and_wait(runtime)

        runtime.set_channels_engaged(False)

        self.assertTrue(self._wait_for(lambda: self.stub.specs[-1:] == [""]))

    def test_restart_disarms_the_stale_daemon_before_the_new_master_boots(self) -> None:
        """复刻「抓包中改端口」：旧循环关闭丢掉挂起的清理任务，stop() 的同步
        disarm 必须在循环死前补上；随后新内核的 _start 重新接管。"""
        runtime = self._make_runtime()
        runtime.start()
        self.assertTrue(self.wait_for_signal(runtime.ready))
        self._engage_and_wait(runtime)

        runtime.restart(listen_port=free_port())
        self.assertTrue(self.wait_for_signal(runtime.ready))

        self.assertIn("", self.stub.specs)
        # local 意图仍在（restart 不动意图值）；新内核的 _start 是 running 之后
        # 的异步任务，等它完成接管。
        self.assertTrue(
            self._wait_for(lambda: self.stub.specs[-1] == f"!{os.getpid()}"),
            f"daemon was never re-armed after restart: {self.stub.specs}",
        )


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
