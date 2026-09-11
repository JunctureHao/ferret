"""上游代理出口的内核侧验收（.plans/upstream-mode.md §7 第 10、11 条）。

分三组，按「越靠近原生越不需要内核」排：

* `UpstreamAuthAddonTests` —— 凭证注入的三条分支，直接驱动原生 addon 的钩子。
  这里钉的是**串台语义**：``upstream_auth`` 非空时 reverse 通道的请求也会被补上
  ``Authorization`` 头。那不是 bug，是原生 ``UpstreamAuth`` 的设计（它一个 addon
  同时服务 upstream 与 reverse 两种模式），ferret 分不开，只能把闸门放在选项侧。
  **这组用例存在的意义就是防止将来有人把它当 bug「修」掉。**
* `UpstreamViaTests` —— ``server_conn.via`` 是「这条连接走上游」的结构性标记，
  ``HttpUpstreamProxy.make`` 断言它非空并从中取出口地址。重放/compose 这条路由
  ``ClientPlayback`` 自己按 ``options.mode[0]`` 前缀设它（`clientplayback.py:97-105`）
  —— 换槽位而不是追加通道，图的正是这个自动成立。
* `UpstreamKernelTests` —— 跑真内核 + 一个假上游代理，走完整条链路：真客户端连
  ferret 的监听口 → 内核 → 上游。验明文 HTTP 确实以 absolute-form 发给上游并带
  ``Proxy-Authorization``（计划 §2 的两条出口路径之一，也是 §8 手工验证那步的自动化）。

CONNECT（HTTPS）那条路径这里不跑端到端：``handle_connect_finish`` 先把
"200 Connection established" 回给客户端，真正的上游连接要等隧道里有数据才发起
（懒建），拿它断言等于赌时序。它的结构性保证由 `UpstreamViaTests` 覆盖 ——
``handle_connect_upstream`` 无条件构造 ``HttpUpstreamProxy``，而后者在
``make`` 里就断言 ``via`` 非空。
"""

import asyncio
import base64
import os
import socket
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.clientplayback import ClientPlayback
from mitmproxy.proxy.mode_specs import ProxyMode
from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.bindings import Options, UpstreamAuth
from ferret.core.mitm.gateway import GatewayLayer, GatewayPolicy, GatewayRule
from ferret.core.mitm.master import FerretMaster

from ._qt import start_runtime, wait_until

BASIC = b"Basic " + base64.b64encode(b"alice:secret")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def flow_in_mode(spec: str, *, scheme: str = "http"):
    """一条 ``client_conn.proxy_mode`` 指定为 ``spec`` 的 flow。

    ``UpstreamAuth.requestheaders`` 的分支判据就是这个字段的类型
    （``upstream_auth.py:52-58``），所以驱动它必须换掉 tflow 的默认 regular。
    """
    flow = tflow.tflow()
    flow.client_conn.proxy_mode = ProxyMode.parse(spec)
    flow.request.scheme = scheme
    return flow


class UpstreamAuthAddonTests(unittest.TestCase):
    """原生 ``UpstreamAuth`` 的三条注入分支，以及它在 ferret 链上的位置。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def _addon(self) -> UpstreamAuth:
        addon = self.master.addons.get("upstreamauth")
        assert isinstance(addon, UpstreamAuth)
        return addon

    def test_the_addon_is_mounted(self) -> None:
        self.assertIn(self._addon(), self.master.addons.chain)

    def test_the_option_exists_only_after_the_addon_is_added(self) -> None:
        """所以凭证只能等 Master 建好再写 —— `_apply_upstream_auth` 的存在理由
        （与 anticache 那组完全同一个约束）。"""
        self.assertNotIn("upstream_auth", Options().keys())
        self.assertIn("upstream_auth", self.master.options)

    def test_the_default_is_none_not_empty_string(self) -> None:
        self.assertIsNone(self.master.options.upstream_auth)

    def test_empty_string_is_rejected_by_the_native_validator(self) -> None:
        """``parse_upstream_auth`` 的正则是 ``.+:`` —— 空串抛 OptionsError。

        这就是 `MitmRuntime._upstream_auth()` 必须回 ``None`` 而不是 ``""`` 的
        原因；「只填地址不填凭证」是最常走的一条路径。
        """
        from ferret.core.mitm.bindings import OptionsError

        with self.assertRaises(OptionsError):
            self.master.options.update(upstream_auth="")

    def test_upstream_http_request_gets_proxy_authorization(self) -> None:
        """明文 HTTP 走 absolute-form + ``Proxy-Authorization``（计划 §2）。"""
        self.master.options.update(upstream_auth="alice:secret")
        flow = flow_in_mode("upstream:http://proxy.corp:8080")
        self._addon().requestheaders(flow)
        self.assertEqual(flow.request.headers["Proxy-Authorization"], BASIC.decode())

    def test_upstream_connect_gets_proxy_authorization(self) -> None:
        """HTTPS 那一路由 ``http_connect_upstream`` 钩子补头。"""
        self.master.options.update(upstream_auth="alice:secret")
        flow = flow_in_mode("upstream:http://proxy.corp:8080", scheme="https")
        self._addon().http_connect_upstream(flow)
        self.assertEqual(flow.request.headers["Proxy-Authorization"], BASIC.decode())

    def test_reverse_requests_also_get_an_authorization_header(self) -> None:
        """**串台语义的钉子，不要把它当 bug 修掉。**

        ``upstream_auth`` 非空时，reverse 通道的请求会被补上 ``Authorization``
        （注意不是 ``Proxy-Authorization``）—— 即企业代理的密码会被发给反代目标。
        原生一个 addon 同时服务两种模式，分不开。ferret 的处理是三层：
        ①`MitmRuntime._upstream_auth()` 在上游关闭时恒回 None（见下一条用例与
        `test_runtime.py::test_upstream_auth_is_none_when_upstream_is_off`）；
        ②对话框在两者同开时给明确警告（可继续）；③就是这条用例。
        """
        self.master.options.update(upstream_auth="alice:secret")
        flow = flow_in_mode("reverse:https://example.com@127.0.0.1:8081")
        self._addon().requestheaders(flow)
        self.assertEqual(flow.request.headers["Authorization"], BASIC.decode())

    def test_no_auth_means_no_header_anywhere(self) -> None:
        """闸门在选项侧：auth 为 None 时全钩子空转，reverse 那条注入随之消失。"""
        self.master.options.update(upstream_auth=None)
        for spec in (
            "upstream:http://proxy.corp:8080",
            "reverse:https://example.com@127.0.0.1:8081",
        ):
            with self.subTest(spec=spec):
                flow = flow_in_mode(spec)
                self._addon().requestheaders(flow)
                self.assertNotIn("Proxy-Authorization", flow.request.headers)
                self.assertNotIn("Authorization", flow.request.headers)

    def test_regular_mode_is_never_touched(self) -> None:
        """regular 通道不注入：它没有上游，补了头只会泄给目标服务器。"""
        self.master.options.update(upstream_auth="alice:secret")
        flow = flow_in_mode("regular")
        self._addon().requestheaders(flow)
        self.assertNotIn("Proxy-Authorization", flow.request.headers)
        self.assertNotIn("Authorization", flow.request.headers)


class UpstreamViaTests(unittest.TestCase):
    """``server_conn.via`` —— 「这条连接走上游」的结构性标记。

    ``HttpUpstreamProxy.make``（`_upstream_proxy.py:33-34`）断言它非空，并从里面
    取出口 ``(scheme, address)``。所以 via 非空 ⟺ 出口是上游代理。
    """

    def test_client_playback_follows_the_head_slot(self) -> None:
        """重放 / compose 自动跟着走上游：``ClientPlayback`` 只看 ``mode[0]``
        是不是 ``upstream:`` 前缀（`clientplayback.py:97-105`）。**这正是「替换
        首槽」而不是「追加第五条通道」换来的白捡好处**（计划 §3.3）。
        """
        from mitmproxy.addons.clientplayback import ReplayHandler

        options = Options()
        options.update(mode=["upstream:http://proxy.corp:8080"])
        flow = tflow.tflow(resp=True)
        # 只构造不 replay：via 在 __init__ 里就设好了，而 ConnectionHandler
        # 的构造不起任何任务（watchdog 要等 watch() 被 await 才转），没有要拆的东西。
        ReplayHandler(flow, options)

        self.assertEqual(flow.server_conn.via, ("http", ("proxy.corp", 8080)))

    def test_client_playback_leaves_via_unset_without_upstream(self) -> None:
        """没开上游时重放直连，via 保持 None。"""
        from mitmproxy.addons.clientplayback import ReplayHandler

        options = Options()
        options.update(mode=["regular"])
        flow = tflow.tflow(resp=True)
        ReplayHandler(flow, options)

        self.assertIsNone(flow.server_conn.via)

    def test_the_addon_is_mounted_and_sees_the_mode(self) -> None:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        master = FerretMaster(event_loop=loop)
        self.assertIsInstance(master.addons.get("clientplayback"), ClientPlayback)


class UpstreamKernelTests(unittest.TestCase):
    """跑真内核 + 假上游：完整链路的端到端验收。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _fake_upstream(self) -> "FakeUpstream":
        upstream = FakeUpstream()
        self.addCleanup(upstream.close)
        return upstream

    def _engaged_runtime(self, upstream: "FakeUpstream", **kwargs) -> MitmRuntime:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target=f"http://127.0.0.1:{upstream.port}",
            **kwargs,
        )
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        runtime.set_channels_engaged(True)
        return runtime

    def test_plaintext_http_reaches_the_upstream_in_absolute_form(self) -> None:
        """明文 HTTP **不**被包成 CONNECT，而是 absolute-form 直接发给上游。

        这是选中「槽位替换」而非自写 ``via`` addon 的关键理由之一（计划 §3.2）：
        企业 Squid 默认只允许 CONNECT 到 SSL_ports，``CONNECT :80`` 会被拒，
        而那正是本功能的主场景。凭证也在同一条报文里。
        """
        upstream = self._fake_upstream()
        runtime = self._engaged_runtime(
            upstream, upstream_username="alice", upstream_password="secret"
        )

        response = http_through(runtime.listen_port, "http://example.com/probe")
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"), response)

        self.assertTrue(wait_until(lambda: bool(upstream.requests)))
        seen = upstream.requests[0]
        # absolute-form：请求行里带完整 URL，不是 origin-form 的 /probe。
        self.assertTrue(seen.startswith(b"GET http://example.com/probe HTTP/1.1"), seen)
        self.assertIn(b"Proxy-Authorization: " + BASIC, seen)

    def test_without_credentials_no_authorization_header_is_sent(self) -> None:
        """只填地址不填凭证是最常见的用法，必须走得通（auth 为 None）。"""
        upstream = self._fake_upstream()
        runtime = self._engaged_runtime(upstream)

        master = runtime._master
        assert master is not None
        self.assertIsNone(runtime.call(lambda: master.options.upstream_auth))

        http_through(runtime.listen_port, "http://example.com/probe")
        self.assertTrue(wait_until(lambda: bool(upstream.requests)))
        self.assertNotIn(b"Proxy-Authorization", upstream.requests[0])

    def test_the_head_slot_and_credentials_are_seeded_before_traffic(self) -> None:
        """开机注入：第一条连接进来时 mode 与 upstream_auth 都必须已经就位。"""
        upstream = self._fake_upstream()
        runtime = self._engaged_runtime(
            upstream, upstream_username="alice", upstream_password="secret"
        )
        master = runtime._master
        assert master is not None

        self.assertEqual(
            runtime.call(lambda: list(master.options.mode)),
            [f"upstream:http://127.0.0.1:{upstream.port}"],
        )
        self.assertEqual(
            runtime.call(lambda: master.options.upstream_auth), "alice:secret"
        )

    def test_turning_the_upstream_off_restores_regular_and_clears_auth(self) -> None:
        """热更关闭：首槽换回 regular，凭证一并回 None。

        凭证这一半不是顺手 —— 留着它，reverse 通道的请求会继续被补
        ``Authorization``（见 `UpstreamAuthAddonTests` 那条串台用例）。
        """
        upstream = self._fake_upstream()
        runtime = self._engaged_runtime(
            upstream, upstream_username="alice", upstream_password="secret"
        )
        master = runtime._master
        assert master is not None

        runtime.apply_channels(use_upstream=False)

        self.assertEqual(runtime.call(lambda: list(master.options.mode)), ["regular"])
        self.assertIsNone(runtime.call(lambda: master.options.upstream_auth))

    def test_a_bypass_rule_does_not_take_the_upstream_away(self) -> None:
        """绕行规则命中时出口仍是上游（计划 §6 的 GatewayL4 行）。

        机理：``handle_connect_upstream``（`http/__init__.py:802-804`）**无条件**
        构造 ``HttpUpstreamProxy``，隧道先建好，``ignore_hosts`` 由隧道**内部**的
        子 ``NextLayer`` 再判 —— 绕行只决定隧道里要不要解密，不决定连谁。
        这里验两件可确定的事：两个平面同时在场且互不干扰（mode 仍是 upstream、
        ignore_hosts 仍下发、gateway 仍判得出绕行），以及明文流量照旧到达上游。
        """
        upstream = self._fake_upstream()
        runtime = self._engaged_runtime(upstream)
        runtime.apply_gateway_rules(
            [
                GatewayRule(
                    layer=GatewayLayer.L4,
                    policy=GatewayPolicy.BYPASS,
                    value="example.com",
                )
            ]
        )
        master = runtime._master
        assert master is not None

        self.assertEqual(
            runtime.call(lambda: list(master.options.mode)),
            [f"upstream:http://127.0.0.1:{upstream.port}"],
        )
        self.assertTrue(runtime.call(lambda: list(master.options.ignore_hosts)))
        self.assertIsNotNone(
            runtime.call(lambda: master.gateway.decide("example.com", 443, "GET"))
        )

        # 明文 HTTP 不经 CONNECT，绕行规则管不到它，仍旧到达上游。
        http_through(runtime.listen_port, "http://example.com/probe")
        self.assertTrue(wait_until(lambda: bool(upstream.requests)))


class FakeUpstream:
    """只够用的假上游代理：收一条请求头，回一条固定 200，记下原文。

    真造一个 socket 服务而不是拿 mock 糊：要验的正是「报文确实以 absolute-form
    发到了上游那一端」，中间隔一层假对象就什么都没验到。
    """

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = int(self.sock.getsockname()[1])
        self.requests: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:  # close() 关掉监听 socket，线程随之退出
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            self.requests.append(data)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi"
            )

    def close(self) -> None:
        self.sock.close()


def http_through(listen_port: int, url: str, *, timeout_ms: int = 20000) -> bytes:
    """经 ferret 的监听口发一条明文代理请求，返回客户端收到的原始响应。

    客户端跑在后台线程、主线程用 `wait_until` 泵 Qt 事件队列：内核在自己的线程
    上跑，主线程阻塞在 recv 上会让等待原语失去意义（`_qt.py` 的机理说明）。
    """
    result: dict[str, bytes] = {}

    def run() -> None:
        try:
            with socket.create_connection(("127.0.0.1", listen_port), timeout=10) as c:
                host = url.split("/")[2]
                c.sendall(
                    f"GET {url} HTTP/1.1\r\nHost: {host}\r\n"
                    "Connection: close\r\n\r\n".encode()
                )
                buf = b""
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                result["response"] = buf
        except OSError as exc:
            result["response"] = f"socket error: {exc}".encode()

    threading.Thread(target=run, daemon=True).start()
    if not wait_until(lambda: "response" in result, timeout_ms=timeout_ms):
        raise AssertionError("客户端请求超时")
    return result["response"]


if __name__ == "__main__":
    unittest.main()
