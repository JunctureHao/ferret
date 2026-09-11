"""Tests for capture channel specs (core/mitm/modes.py).

四条通道的 spec 是界面与内核的唯一接口：这里守住拼装格式、坏值拦截、首槽的
regular/upstream 二选一（上游代理不是第五条通道，见 .plans/upstream-mode.md）、
WireGuard 客户端配置的生成格式（对齐上游 ``WireGuardServerInstance.client_conf``），
以及 local 提权守护进程在内核停止/重启时的拆除时机。
"""

import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.bindings import LocalRedirectorInstance, ProxyMode
from ferret.core.mitm.modes import (
    REVERSE_DEFAULT_PORT,
    WIREGUARD_PORT,
    LocalTarget,
    capture_mode_specs,
    checked_tokens,
    list_local_targets,
    local_mode_spec,
    qr_matrix,
    reverse_mode_spec,
    split_spec,
    upstream_address,
    upstream_mode_spec,
    upstream_targets_self,
    validate_local_spec,
    validate_mode_specs,
    wireguard_client_config,
    wireguard_mode_spec,
    wireguard_qr_matrix,
)

from ._qt import start_runtime, wait_ready, wait_until


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class CaptureModeSpecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])

    def test_first_slot_is_always_present(self) -> None:
        """首槽是常驻底盘：通道全关时 mode 也必须只剩它一条。

        内容二选一 —— 默认 regular，开了上游代理整条换成 ``upstream:<目标>``
        （.plans/upstream-mode.md §3.3）。恒在的是**槽位**，不是 ``"regular"``
        这个字面量。
        """
        self.assertEqual(
            capture_mode_specs(use_local=False, local_spec="", use_wireguard=False),
            ["regular"],
        )
        self.assertEqual(
            capture_mode_specs(
                use_local=False,
                local_spec="",
                use_wireguard=False,
                use_upstream=True,
                upstream_target="http://proxy:8080",
            ),
            ["upstream:http://proxy:8080"],
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
        validate_mode_specs(["regular", "local:curl,!1234", wireguard_mode_spec()])

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

    def _wait_for(self, predicate, timeout_s: float = 15.0) -> bool:
        return wait_until(predicate, timeout_ms=int(timeout_s * 1000))

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
        start_runtime(runtime)
        self._engage_and_wait(runtime)

        runtime.stop()

        self.assertEqual(self.stub.specs[-1], "")

    def test_disengage_clears_the_daemon_through_the_async_path(self) -> None:
        """内核存活时的正常解除：异步 stop 任务清 spec，guard 住这条正路。"""
        runtime = self._make_runtime()
        start_runtime(runtime)
        self._engage_and_wait(runtime)

        runtime.set_channels_engaged(False)

        self.assertTrue(self._wait_for(lambda: self.stub.specs[-1:] == [""]))

    def test_restart_disarms_the_stale_daemon_before_the_new_master_boots(self) -> None:
        """复刻「抓包中改端口」：旧循环关闭丢掉挂起的清理任务，stop() 的同步
        disarm 必须在循环死前补上；随后新内核的 _start 重新接管。"""
        runtime = self._make_runtime()
        start_runtime(runtime)
        self._engage_and_wait(runtime)

        runtime.restart(listen_port=free_port())
        wait_ready(runtime)

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

    def test_config_encodes_to_a_square_matrix(self) -> None:
        matrix = wireguard_qr_matrix(self.conf_path, "192.168.1.9")
        rows, cols = len(matrix), len(matrix[0])
        self.assertEqual(rows, cols)
        # 实测配置 ~224B → 纠错 M 下 version 11 = 61×61（无静区）；留些余量
        # 容忍 Endpoint 长度变化，version 上限钉在 20（97×97）防内容意外膨胀。
        self.assertTrue(21 <= rows <= 97, rows)
        self.assertTrue(all(isinstance(v, bool) for row in matrix for v in row))

    def test_finder_patterns_present_in_three_corners(self) -> None:
        """三个定位角各有一个 7×7「外黑内白再黑」图案——扫码器靠它定向。"""
        matrix = wireguard_qr_matrix(self.conf_path, None)

        def has_finder(x: int, y: int) -> bool:
            for dy in range(7):
                for dx in range(7):
                    edge = dx in (0, 6) or dy in (0, 6)
                    core = 2 <= dx <= 4 and 2 <= dy <= 4
                    if matrix[y + dy][x + dx] is not (edge or core):
                        return False
            return True

        n = len(matrix)
        self.assertTrue(has_finder(0, 0))
        self.assertTrue(has_finder(n - 7, 0))
        self.assertTrue(has_finder(0, n - 7))

    def test_matrix_is_deterministic_and_text_derived(self) -> None:
        """码与文本框同源同刻的契约：同一配置两种入口的矩阵逐位一致。"""
        self.assertEqual(
            wireguard_qr_matrix(self.conf_path, "192.168.1.9"),
            qr_matrix(wireguard_client_config(self.conf_path, "192.168.1.9")),
        )
        self.assertEqual(qr_matrix("ferret-qr-smoke"), qr_matrix("ferret-qr-smoke"))

    def test_oversized_text_is_rejected(self) -> None:
        """编码失败显式抛 ValueError（界面据此只留手动复制退路）。"""
        with self.assertRaises(ValueError):
            qr_matrix("x" * 4000)


class LocalTargetTests(unittest.TestCase):
    """进程点选的数据层：spec 拆分/回显 + 真实枚举的形状。"""

    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])

    def test_split_spec_tolerates_messy_input(self) -> None:
        self.assertEqual(split_spec(" a,, b ,!123 "), ["a", "b", "!123"])
        self.assertEqual(split_spec(",,,"), [])
        self.assertEqual(split_spec(""), [])

    def test_checked_tokens_match_exactly_case_insensitively(self) -> None:
        targets = [
            LocalTarget("Chrome", r"C:\x\chrome.exe", None),
            LocalTarget("钉钉", r"D:\ding.exe", None),
        ]
        # 显示名 / exe 名 / 短写都能回显（判定沿用内核 contains 语义）；
        # 排除项 !123 不点亮任何目标。
        self.assertEqual(
            checked_tokens("CHROME.EXE, ding, !123", targets), {"Chrome", "ding.exe"}
        )
        # 完全无关的 token 不点亮任何目标。
        self.assertEqual(checked_tokens("word", targets), set())

    def test_checked_tokens_skip_empty_candidates(self) -> None:
        """空 display_name 的目标不得被任何 token 点亮。

        空串是一切串的子串，contains 语义下会被任意 spec 误勾选，拼接时产出
        尾逗号（"Notion.exe,"），被上游 describe_spec 拒收。
        """
        targets = [LocalTarget("", r"C:\x\noname.exe", None)]
        self.assertEqual(checked_tokens("notion.exe", targets), set())
        self.assertEqual(checked_tokens("noname", targets), {"noname.exe"})

    def test_list_local_targets_returns_real_processes(self) -> None:
        """真实枚举（真机冒烟）：系统服务仍被滤掉，且不含 ferret 自身。

        不断言 executables 都真实存在——上游会枚举出 ``Registry`` 这类伪路径。
        """
        targets = list_local_targets()
        self.assertTrue(targets, "至少应枚举到一个非系统进程")
        names = {target.display_name.lower() for target in targets}
        self.assertNotIn("svchost.exe", names)
        self.assertNotIn(
            str(Path(sys.executable).resolve()),
            {target.executable for target in targets},
        )

    def test_list_local_targets_include_system_expands_the_list(self) -> None:
        relaxed = list_local_targets(include_system=True)
        strict = list_local_targets()
        self.assertGreaterEqual(len(relaxed), len(strict))


class ReverseModeSpecTests(unittest.TestCase):
    """反向代理（.plans/reverse-mode.md）spec 组装与排他性的钉桩。"""

    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])

    def test_reverse_spec_always_carries_explicit_listen_address(self) -> None:
        """``@`` 必须显式：不带时上游回退全局 listen_host/listen_port，
        与 regular 撞同地址被 ``proxyserver.configure`` 查重拒。
        计划定则 2 用一个回归测试钉死「组装函数恒带 ``@``」。
        """
        spec = reverse_mode_spec(
            "https://example.com", "127.0.0.1", REVERSE_DEFAULT_PORT
        )
        self.assertEqual(
            spec, f"reverse:https://example.com@127.0.0.1:{REVERSE_DEFAULT_PORT}"
        )
        # 原生解析能过、scheme 与目标解析正确。
        mode = ProxyMode.parse(spec)
        self.assertEqual(mode.scheme, "https")
        self.assertEqual(mode.address, ("example.com", 443))
        self.assertEqual(mode.custom_listen_host, "127.0.0.1")
        self.assertEqual(mode.custom_listen_port, REVERSE_DEFAULT_PORT)

    def test_reverse_spec_uses_callsite_listen_host(self) -> None:
        """spec 的 @ 段必须与 regular 的 listen_host 同源（plan §3「一处管」语义）。

        这里直接比对函数输出：用户选「仅本机」时 reverse 也绑 127.0.0.1，
        选「局域网可访问」时绑 0.0.0.0——两处一处管，避免「勾了 reverse 但
        实际绑了 0.0.0.0」这类安全语义偏差。
        """
        for host in ("127.0.0.1", "0.0.0.0"):
            spec = reverse_mode_spec("https://example.com", host, 8081)
            mode = ProxyMode.parse(spec)
            self.assertEqual(mode.custom_listen_host, host, spec)

    def test_reverse_target_with_port_preserves_port(self) -> None:
        spec = reverse_mode_spec("https://example.com:8443", "127.0.0.1", 8081)
        mode = ProxyMode.parse(spec)
        self.assertEqual(mode.address, ("example.com", 8443))

    def test_reverse_target_stripped_of_surrounding_whitespace(self) -> None:
        spec = reverse_mode_spec("  https://example.com  ", "127.0.0.1", 8081)
        self.assertEqual(spec, "reverse:https://example.com@127.0.0.1:8081")

    def test_capture_mode_specs_appends_reverse_after_regular(self) -> None:
        """reverse 排在 regular 之后、local/wireguard 之前（plan §3）。"""
        specs = capture_mode_specs(
            use_local=True,
            local_spec="curl",
            use_wireguard=True,
            use_reverse=True,
            reverse_target="https://example.com",
            reverse_port=8081,
            listen_host="127.0.0.1",
        )
        self.assertEqual(
            specs[0],
            "regular",
        )
        self.assertTrue(specs[1].startswith("reverse:"), specs)
        self.assertIn("local:curl@127.0.0.1:0", specs)
        self.assertIn(wireguard_mode_spec(), specs)

    def test_empty_reverse_target_does_not_emit_a_spec(self) -> None:
        """目标空时不发 reverse spec：避免「勾上但目标没填」误把空 spec
        推进内核（``ProxyMode.parse`` 会拒）。这是 capture_mode_specs 的内建
        护栏，对应计划 §3 端口撞车之外的另一道前置。
        """
        specs = capture_mode_specs(
            use_local=False,
            local_spec="",
            use_wireguard=False,
            use_reverse=True,
            reverse_target="",
            reverse_port=8081,
        )
        self.assertEqual(specs, ["regular"])

    def test_validate_accepts_well_formed_reverse(self) -> None:
        validate_mode_specs(["regular", "reverse:https://example.com@127.0.0.1:8081"])

    def test_validate_rejects_reverse_without_target(self) -> None:
        # ``reverse:@127.0.0.1:8081`` 的目标段为空、ProxyMode.parse 会因
        # 主机名校验失败而拒。spec 必须恒带非空 target 段（计划定则 2）。
        with self.assertRaises(ValueError):
            validate_mode_specs(["regular", "reverse:@127.0.0.1:8081"])

    def test_reverse_https_binds_both_tcp_and_udp(self) -> None:
        """https scheme 在 ``ReverseMode.__post_init__`` 置 ``transport_protocol=BOTH``，
        顶端 ReverseProxy 因此既拉 TCP 也拉 UDP 监听（plan §0/§5）。
        这一点是 reverse 通道防 QUIC 旁路的关键：UDP 监听由 native 拉起，
        h3 打回监听口走原生终结路径。"""
        spec = reverse_mode_spec("https://example.com", "127.0.0.1", 8081)
        mode = ProxyMode.parse(spec)
        self.assertEqual(mode.transport_protocol, "both")


class UpstreamModeSpecTests(unittest.TestCase):
    """上游代理（.plans/upstream-mode.md）：首槽替换而非追加通道。"""

    @classmethod
    def setUpClass(cls) -> None:
        QApplication.instance() or QApplication([])

    def test_upstream_replaces_the_first_slot_instead_of_adding_one(self) -> None:
        """开启后 ``mode[0]`` 变 upstream，而且列表里**不能**再有 ``"regular"``。

        二者绝不并存：都回退全局 listen_port，同时在场必被
        ``proxyserver.configure`` 的地址查重拒（计划 §3.3 的硬约束）。
        """
        off = capture_mode_specs(use_local=True, local_spec="curl", use_wireguard=False)
        self.assertEqual(off[0], "regular")

        on = capture_mode_specs(
            use_local=True,
            local_spec="curl",
            use_wireguard=False,
            use_upstream=True,
            upstream_target="http://proxy:8080",
        )
        self.assertEqual(on[0], "upstream:http://proxy:8080")
        self.assertNotIn("regular", on)
        # 后面的通道一个不少，只是首槽换了内容。
        self.assertEqual(on[1:], off[1:])

    def test_empty_target_keeps_regular(self) -> None:
        """勾上但没填地址不换槽位：空目标换过去只会让系统代理这条主通道整条
        死掉，比不生效糟得多（与 ``reverse_target`` 空值同款防误）。
        """
        self.assertEqual(
            capture_mode_specs(
                use_local=False,
                local_spec="",
                use_wireguard=False,
                use_upstream=True,
                upstream_target="   ",
            ),
            ["regular"],
        )

    def test_upstream_coexists_with_the_other_three_channels(self) -> None:
        """上游 + local + wireguard + reverse 的完整列表能过原生校验，且
        ``(host, port, proto)`` 三元组无重复（姿势照抄
        ``test_local_spec_carries_the_duplicate_address_dodge``）。

        首槽不带 ``@``，回退的正是全局 ``listen_host:listen_port`` —— 与 regular
        占的是同一个地址，所以这条用例真正验的是「换掉之后查重依然干净」。
        """
        for listen_host in ("127.0.0.1", "0.0.0.0"):
            for listen_port in (8080, 9123):
                specs = capture_mode_specs(
                    use_local=True,
                    local_spec="curl",
                    use_wireguard=True,
                    use_reverse=True,
                    reverse_target="https://example.com",
                    reverse_port=8081,
                    use_upstream=True,
                    upstream_target="http://proxy.corp:3128",
                    listen_host=listen_host,
                )
                with self.subTest(listen_host=listen_host, listen_port=listen_port):
                    self.assertEqual(len(specs), 4, specs)
                    validate_mode_specs(specs)
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
                    self.assertEqual(len(addrs), len(set(addrs)), addrs)

    def test_upstream_spec_never_carries_a_listen_segment(self) -> None:
        """首槽必须继承全局监听地址：带了 ``@`` 就成了「另开一个端口的第五条
        通道」，客户端得再配一次代理，而系统代理注册表写的仍是旧端口
        （计划 §3.1 被否 A）。
        """
        spec = upstream_mode_spec("http://proxy.corp:8080")
        self.assertEqual(spec, "upstream:http://proxy.corp:8080")
        mode = ProxyMode.parse(spec)
        self.assertIsNone(mode.custom_listen_host)
        self.assertIsNone(mode.custom_listen_port)
        # 没有自定义端口 → 回退调用方给的全局端口，与 regular 同一个。
        self.assertEqual(mode.listen_port(8080), 8080)
        self.assertEqual(mode.listen_host("127.0.0.1"), "127.0.0.1")

    def test_upstream_target_is_stripped(self) -> None:
        self.assertEqual(
            upstream_mode_spec("  http://proxy:8080  "), "upstream:http://proxy:8080"
        )

    def test_upstream_address_applies_native_scheme_and_port_defaults(self) -> None:
        """同一个上游有一大把写法，端口/scheme 都能省 —— 所以自环判定必须过
        解析器，不能拿字符串比对糊弄（``UpstreamMode.__post_init__``）。
        """
        self.assertEqual(upstream_address("http://proxy:8080"), ("proxy", 8080))
        self.assertEqual(upstream_address("https://proxy.corp"), ("proxy.corp", 443))
        self.assertEqual(upstream_address("proxy.corp"), ("proxy.corp", 80))
        # 裸 host:port 走 default_scheme="http"，是原生就接受的合法写法。
        self.assertEqual(upstream_address("proxy:8080"), ("proxy", 8080))

    def test_credentials_in_target_are_rejected(self) -> None:
        """凭证不进 spec：原生 host 段是 ``[^:/]+``，且 ``ProxyMode.parse`` 先按
        最后一个 ``@`` 切监听段。凭证走 ``upstream_auth`` 选项那条正交的线。
        """
        with self.assertRaises(ValueError):
            validate_mode_specs(
                [upstream_mode_spec("http://user:pass@proxy.corp:8080")]
            )

    def test_bad_scheme_is_rejected(self) -> None:
        """``UpstreamMode`` 只认 http/https —— SOCKS 上游不支持（计划 §9）。"""
        for bad in ("ftp://proxy:8080", "socks5://proxy:1080", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_mode_specs([upstream_mode_spec(bad)])

    def test_upstream_targets_self_detects_the_loopback_ring(self) -> None:
        """自环判据照抄原生 ``proxyserver.server_connect``：端口相同 + host 落在
        localhost/127.0.0.1/::1/监听地址 之内。前置拦一道，免得用户只看到
        每条流量上那句 "Request destination unknown"。
        """
        for target in (
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "127.0.0.1:8080",
        ):
            with self.subTest(target=target):
                self.assertTrue(
                    upstream_targets_self(
                        target, listen_host="127.0.0.1", listen_port=8080
                    )
                )
        # 端口不同 / 主机不同都不算自环。
        self.assertFalse(
            upstream_targets_self(
                "http://127.0.0.1:8888", listen_host="127.0.0.1", listen_port=8080
            )
        )
        self.assertFalse(
            upstream_targets_self(
                "http://proxy.corp:8080", listen_host="127.0.0.1", listen_port=8080
            )
        )

    def test_upstream_targets_self_honours_the_lan_listen_host(self) -> None:
        """监听地址本身也在判据里：选「局域网可访问」时 0.0.0.0 就是自环。"""
        self.assertTrue(
            upstream_targets_self(
                "http://0.0.0.0:8080", listen_host="0.0.0.0", listen_port=8080
            )
        )
        self.assertFalse(
            upstream_targets_self(
                "http://0.0.0.0:8080", listen_host="127.0.0.1", listen_port=8080
            )
        )


if __name__ == "__main__":
    unittest.main()
