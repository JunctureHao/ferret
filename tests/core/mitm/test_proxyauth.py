"""代理认证的内核侧验收（.plans/proxyauth.md §7）。

分三组，按「越靠近原生越不需要内核」排：

* `ProxyAuthAddonTests` —— 直接驱动原生 addon 的钩子。这里钉的是**挑战判定的
  按模式分流**：``is_http_proxy``（``addons/proxyauth.py:143-151``）只认
  RegularMode / UpstreamMode，其余模式不是「跳过」而是按 HTTP 服务器语义回 401。
  那不是 bug，是原生设计（一个 addon 同时服务代理与反代两种部署），ferret 分不开，
  只能把闸门放在选项侧。**这组用例存在的意义就是防止将来有人把它当 bug「修」掉。**
  剥头方向（regular 剥 ``Proxy-Authorization``、reverse 剥 ``Authorization``）
  同理，也是钉子而不是期望。
* `ProxyAuthGateTests` —— `MitmRuntime._effective_proxyauth()` 的让路矩阵，
  纯函数不起内核。让路名单比 `_effective_block_private` 多一个 **local**：
  原生 Block 对 LocalMode 连接有豁免（``block.py:35``），ProxyAuth 没有。
* `ProxyAuthKernelTests` —— 跑真内核 + 一个假源站，走完整条链路：真客户端连
  ferret 的监听口 → 407 → 带凭证重试 → 200，并验源站**没有**收到凭证头。

让路那一半在内核组里只验 reverse 这一条：local 要提权装驱动、wireguard 要开 UDP
口，在 CI 上起不来；三条通道走的是同一个判据同一行代码，`ProxyAuthGateTests`
已经把矩阵钉死，内核组只需证明「这个判据真的被接到了 options.update 上」。
"""

import asyncio
import base64
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.proxyauth import ProxyAuth, SingleUser
from mitmproxy.proxy.mode_specs import ProxyMode
from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.addons import CertDownloadAddon, ProxyAuthScrubAddon
from ferret.core.mitm.bindings import Options, OptionsError
from ferret.core.mitm.master import FerretMaster

from ._qt import start_runtime, wait_until

# 假源站与「经代理发一条明文请求」的架子与上游那组逐字相同，不重复一遍：
# FakeUpstream 收请求头、回固定 200、记下原文 —— 当假源站用正合适。
from .test_upstream import FakeUpstream as FakeOrigin
from .test_upstream import free_port, http_through

BASIC = "Basic " + base64.b64encode(b"alice:secret").decode()
WRONG = "Basic " + base64.b64encode(b"alice:wrong").decode()


def flow_in_mode(spec: str, *, scheme: str = "http"):
    """一条 ``client_conn.proxy_mode`` 指定为 ``spec`` 的 flow。

    ``is_http_proxy`` 的判据就是这个字段的类型（``proxyauth.py:143-151``），
    所以驱动它必须换掉 tflow 的默认 regular —— 与 `test_upstream.flow_in_mode`
    同一手法。
    """
    flow = tflow.tflow()
    flow.client_conn.proxy_mode = ProxyMode.parse(spec)
    flow.request.scheme = scheme
    return flow


class ProxyAuthAddonTests(unittest.TestCase):
    """原生 ``ProxyAuth`` 的挑战 / 剥头 / 跳过三类行为，以及它在 ferret 链上的位置。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def _addon(self) -> ProxyAuth:
        addon = self.master.addons.get("proxyauth")
        assert isinstance(addon, ProxyAuth)
        return addon

    def test_the_addon_is_mounted(self) -> None:
        self.assertIn(self._addon(), self.master.addons.chain)

    def test_it_sits_immediately_before_the_proxyserver(self) -> None:
        """位置对齐原生 ``default_addons()``：认证要先于任何流量成形判完。

        紧跟其后的是抹凭证的自研件 —— 顺序不能倒，倒了就抹了个空。
        """
        names = [addon.__class__.__name__ for addon in self.master.addons.chain]
        self.assertEqual(
            names[names.index("ProxyAuth") : names.index("ProxyAuth") + 3],
            ["ProxyAuth", "ProxyAuthScrubAddon", "Proxyserver"],
        )

    def test_the_option_exists_only_after_the_addon_is_added(self) -> None:
        """所以凭证只能等 Master 建好再写 —— `_apply_proxyauth` 的存在理由
        （与 upstream_auth / anticache 那两组完全同一个约束）。"""
        self.assertNotIn("proxyauth", Options().keys())
        self.assertIn("proxyauth", self.master.options)

    def test_the_default_is_none(self) -> None:
        """原生没有「关闭」spec，关就是置 None。"""
        self.assertIsNone(self.master.options.proxyauth)

    def test_the_spec_must_have_exactly_two_colon_separated_parts(self) -> None:
        """``SingleUser`` 用 ``split(":")`` 切，多一段少一段都抛 OptionsError。

        这就是 UI 前置拒绝冒号、`_effective_proxyauth` 另设兜底闸门的原因：
        含冒号的串推下去会把整个 ``options.update`` 连 mode 一起打回。
        """
        for spec in ("alice:secret", "alice:", ":"):
            with self.subTest(spec=spec):
                self.master.options.update(proxyauth=spec)
                self.assertEqual(self.master.options.proxyauth, spec)
        for spec in ("alice", "alice:se:cret"):
            with self.subTest(spec=spec), self.assertRaises(OptionsError):
                self.master.options.update(proxyauth=spec)

    def test_an_empty_password_is_accepted_by_the_validator(self) -> None:
        """`_effective_proxyauth` 只要求用户名非空，密码可空 —— 依据在这里。"""
        self.assertTrue(SingleUser("alice:")("alice", ""))
        self.assertFalse(SingleUser("alice:")("alice", "x"))

    def test_regular_and_upstream_get_407_everything_else_gets_401(self) -> None:
        """**通道适用性的根源，也是让路方案的全部理由。不要把 401 那半当 bug 修。**

        ``is_http_proxy`` 只认 RegularMode / UpstreamMode。其余三种模式的客户端
        并不知道自己在对一个中间件说话，收到 401 只会把通道打挂 —— 所以
        local / wireguard / reverse 接通期间 proxyauth 必须整个撤下。
        """
        self.master.options.update(proxyauth="alice:secret")
        cases = {
            "regular": (407, "Proxy-Authenticate"),
            "upstream:http://proxy.corp:8080": (407, "Proxy-Authenticate"),
            "reverse:https://example.com": (401, "WWW-Authenticate"),
            "local": (401, "WWW-Authenticate"),
            "wireguard": (401, "WWW-Authenticate"),
        }
        for spec, (status, header) in cases.items():
            with self.subTest(spec=spec):
                flow = flow_in_mode(spec)
                self._addon().requestheaders(flow)
                assert flow.response is not None
                self.assertEqual(flow.response.status_code, status)
                self.assertEqual(
                    flow.response.headers[header], 'Basic realm="mitmproxy"'
                )

    def test_a_correct_credential_lets_the_request_through(self) -> None:
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.request.headers["Proxy-Authorization"] = BASIC
        self._addon().requestheaders(flow)
        self.assertIsNone(flow.response)

    def test_a_wrong_credential_is_rejected(self) -> None:
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.request.headers["Proxy-Authorization"] = WRONG
        self._addon().requestheaders(flow)
        assert flow.response is not None
        self.assertEqual(flow.response.status_code, 407)

    def test_the_credential_header_is_stripped_before_forwarding(self) -> None:
        """认证成功后凭证不转发给目标服务器（原生 vaporize）。"""
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.request.headers["Proxy-Authorization"] = BASIC
        self._addon().requestheaders(flow)
        self.assertNotIn("Proxy-Authorization", flow.request.headers)

    def test_reverse_mode_consumes_the_real_authorization_header(self) -> None:
        """**串台语义的钉子，不要把它当 bug 修掉。**

        reverse 通道下原生剥的是 ``Authorization`` —— 那本该是客户端发给**源站**
        的真实凭证，却被当成代理凭证消费掉了（匹配不上还会回 401）。原生一个
        addon 服务两种部署，分不开；ferret 的处置是 reverse 接通期间整个让路。
        """
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("reverse:https://example.com")
        flow.request.headers["Authorization"] = BASIC
        self._addon().requestheaders(flow)
        self.assertIsNone(flow.response)
        self.assertNotIn("Authorization", flow.request.headers)

    def test_the_native_addon_records_the_credential_in_flow_metadata(self) -> None:
        """原生把明文凭证元组写进 ``flow.metadata``（``proxyauth.py:107``）。

        它会顺着详情面板与 ``.flows`` 存盘两条路把密码带出内核，所以 ferret 紧跟
        着抹掉它（下一条用例）。这一条先把原生行为本身钉住 —— 抹除件的前提。
        """
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.request.headers["Proxy-Authorization"] = BASIC
        self._addon().requestheaders(flow)
        self.assertEqual(flow.metadata["proxyauth"], ("alice", "secret"))

    def test_the_scrubber_removes_the_credential_from_flow_metadata(self) -> None:
        """`ProxyAuthScrubAddon` 把它抹掉，且对没有该键的 flow 是空操作。"""
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.request.headers["Proxy-Authorization"] = BASIC
        self._addon().requestheaders(flow)
        scrubber = ProxyAuthScrubAddon()
        scrubber.requestheaders(flow)
        self.assertNotIn("proxyauth", flow.metadata)
        scrubber.requestheaders(flow)  # 幂等：没这个键也不炸
        self.assertNotIn("proxyauth", flow.metadata)

    def test_a_connect_authenticated_connection_skips_later_challenges(self) -> None:
        """CONNECT 认证过的连接进 ``self.authenticated`` 弱键表，后续请求免验
        —— HTTPS 场景下浏览器只会被问一次。"""
        self.master.options.update(proxyauth="alice:secret")
        connect = flow_in_mode("regular", scheme="https")
        connect.request.headers["Proxy-Authorization"] = BASIC
        self._addon().http_connect(connect)
        self.assertIsNone(connect.response)

        later = flow_in_mode("regular", scheme="https")
        later.client_conn = connect.client_conn
        self._addon().requestheaders(later)
        self.assertIsNone(later.response)

    def test_replayed_flows_are_never_challenged(self) -> None:
        """compose / 重放不受挑战（``proxyauth.py:80-81``）—— 否则内部构造的
        请求会被自己的认证挡回来。"""
        self.master.options.update(proxyauth="alice:secret")
        flow = flow_in_mode("regular")
        flow.is_replay = "request"
        self._addon().requestheaders(flow)
        self.assertIsNone(flow.response)

    def test_socks5_subnegotiation_follows_the_same_credential(self) -> None:
        """regular 监听口自带 SOCKS5 自动协商，装载后这条入口一并受保护
        （.plans/proxyauth.md §3.2）—— 白捡的一层，没有也不需要 UI 入口。"""
        self.master.options.update(proxyauth="alice:secret")
        good = _Socks5("alice", "secret")
        # 鸭子替身（真身 Socks5AuthData 是 dataclass，client_conn 要真连接对象，
        # 直驱钩子用不上那么重）；ty 抱怨类型不符，按仓库惯例行级豁免。
        self._addon().socks5_auth(good)  # ty: ignore[invalid-argument-type]
        self.assertTrue(good)
        bad = _Socks5("alice", "wrong")
        self._addon().socks5_auth(bad)  # ty: ignore[invalid-argument-type]
        self.assertFalse(bad)


class _Conn:
    """``connection.Client`` 的替身：``authenticated`` 是 WeakKeyDictionary，
    裸 ``object()`` 没有 ``__weakref__`` 槽会被拒收，任意自造类都行。"""


class _Socks5:
    """``Socks5AuthData`` 的最小替身。

    生效语义来自 ``Socks5AuthHook`` 的 docstring：钩子**恒返回 None**，调用方
    按 ``data.valid`` 判真假，所以替身实现 ``__bool__``、断言落在 data 上。
    认证成功时 addon 还会把连接记进 ``authenticated`` 弱键表
    （``proxyauth.py:68``），所以必须带一个可弱引用的 ``client_conn``。
    """

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
        self.valid = False
        self.client_conn = _Conn()

    def __bool__(self) -> bool:
        return self.valid


class ProxyAuthGateTests(unittest.TestCase):
    """`MitmRuntime._effective_proxyauth()` 的让路矩阵（纯函数，不起内核）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _runtime(
        self,
        *,
        enabled: bool = True,
        username: str = "alice",
        password: str = "secret",
        channels_engaged: bool = True,
        **channels,
    ) -> MitmRuntime:
        runtime = MitmRuntime(
            listen_port=free_port(),
            proxyauth_enabled=enabled,
            proxyauth_username=username,
            proxyauth_password=password,
            **channels,
        )
        runtime.channels_engaged = channels_engaged
        return runtime

    def test_disabled_yields_none(self) -> None:
        self.assertIsNone(self._runtime(enabled=False)._effective_proxyauth())

    def test_an_empty_username_yields_none(self) -> None:
        """空密码合法，空用户名不合法 —— 后者等于谁都能用空用户名进来。"""
        self.assertIsNone(self._runtime(username="")._effective_proxyauth())

    def test_an_empty_password_is_allowed(self) -> None:
        self.assertEqual(self._runtime(password="")._effective_proxyauth(), "alice:")

    def test_a_colon_on_either_side_yields_none(self) -> None:
        """手改配置文件的兜底闸门：原生解析不了的串绝不下发。"""
        for username, password in (("a:b", "secret"), ("alice", "a:b")):
            with self.subTest(username=username):
                runtime = self._runtime(username=username, password=password)
                # assertLogs 一石二鸟：钉住「静默失效必须留一行日志」，同时拦住
                # 这条 warning 不让它冒到根 logger —— 本模块前一组用例造过
                # Master，根上留着指向已关 event loop 的 LegacyLogEvents，冒上去
                # 就是 RuntimeError（先例：tests/apps/common/flow/test_detail.py）。
                with self.assertLogs("ferret.mitmproxy", "WARNING"):
                    self.assertIsNone(runtime._effective_proxyauth())

    def test_regular_only_keeps_the_credential(self) -> None:
        self.assertEqual(self._runtime()._effective_proxyauth(), "alice:secret")

    def test_upstream_does_not_yield(self) -> None:
        """upstream 只是把首槽换成 upstream，仍是同一条 HTTP 代理通道
        —— ``is_http_proxy`` 认它，407 照常挑战。"""
        runtime = self._runtime(
            use_upstream=True, upstream_target="http://proxy.corp:8080"
        )
        self.assertEqual(runtime._effective_proxyauth(), "alice:secret")

    def test_each_incompatible_channel_yields(self) -> None:
        for kwargs in (
            {"use_local": True},
            {"use_wireguard": True},
            {"use_reverse": True, "reverse_target": "https://example.com"},
        ):
            with self.subTest(**kwargs):
                # dict 字面量解包 ty 验不了（先例：test_proxy_port_dialog 的
                # ProxyPortDialog(**values)），行级豁免。
                runtime = self._runtime(**kwargs)  # ty: ignore[invalid-argument-type]
                self.assertIsNone(runtime._effective_proxyauth())

    def test_no_yield_before_the_session_is_engaged(self) -> None:
        """**保护的是监听口本身，不是抓包会话。**

        ferret 的内核常驻监听，「开始抓包」之前局域网就已经能连上来了。所以让路
        只在真的接通之后发生 —— 勾了 local 但没点开始，认证照样生效。
        这条与 `_effective_block_private` 的 engaged 闸门同源。
        """
        runtime = self._runtime(use_local=True, channels_engaged=False)
        self.assertEqual(runtime._effective_proxyauth(), "alice:secret")

    def test_yielding_preserves_the_stored_intent(self) -> None:
        """让路只改下发值，用户配置原样留着，通道撤下后自动恢复。"""
        runtime = self._runtime(use_local=True)
        self.assertIsNone(runtime._effective_proxyauth())
        self.assertTrue(runtime.proxyauth_enabled)
        self.assertEqual(runtime.proxyauth_username, "alice")
        runtime.channels_engaged = False
        self.assertEqual(runtime._effective_proxyauth(), "alice:secret")


class ProxyAuthKernelTests(unittest.TestCase):
    """跑真内核 + 假源站：完整链路的端到端验收。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _origin(self) -> FakeOrigin:
        origin = FakeOrigin()
        self.addCleanup(origin.close)
        return origin

    def _runtime(self, **kwargs) -> MitmRuntime:
        runtime = MitmRuntime(listen_port=free_port(), **kwargs)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        return runtime

    def _authed(self) -> MitmRuntime:
        return self._runtime(
            proxyauth_enabled=True,
            proxyauth_username="alice",
            proxyauth_password="secret",
        )

    def test_the_credential_is_seeded_before_any_traffic(self) -> None:
        """开机注入：第一条连接进来时选项必须已经就位。

        **不等 `channels_engaged`** —— 监听口在「开始抓包」之前就对局域网开着，
        防蹭保护的正是它。
        """
        runtime = self._authed()
        master = runtime._master
        assert master is not None
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime.call(lambda: master.options.proxyauth), "alice:secret")

    def test_a_request_without_credentials_gets_407(self) -> None:
        origin = self._origin()
        runtime = self._authed()
        response = http_through(
            runtime.listen_port, f"http://127.0.0.1:{origin.port}/probe"
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 407"), response)
        self.assertIn(b'Proxy-Authenticate: Basic realm="mitmproxy"', response)
        self.assertEqual(origin.requests, [])

    def test_a_request_with_credentials_reaches_the_origin_without_them(self) -> None:
        """剥头的端到端验证：源站拿到请求，但看不到 ``Proxy-Authorization``。"""
        origin = self._origin()
        runtime = self._authed()
        response = http_through(
            runtime.listen_port,
            f"http://127.0.0.1:{origin.port}/probe",
            extra_headers=f"Proxy-Authorization: {BASIC}\r\n",
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"), response)
        self.assertTrue(wait_until(lambda: bool(origin.requests)))
        self.assertNotIn(b"Proxy-Authorization", origin.requests[0])

    def test_the_credential_never_reaches_the_flow_snapshot(self) -> None:
        """`ProxyAuthScrubAddon` 的端到端验证：View 收录的 flow 里没有凭证。

        原生把 ``(username, password)`` 写进 ``flow.metadata``，而详情面板整份
        铺 metadata、``.flows`` 存盘也含它 —— 抹除件必须早于 View 生效。
        """
        origin = self._origin()
        runtime = self._authed()
        http_through(
            runtime.listen_port,
            f"http://127.0.0.1:{origin.port}/probe",
            extra_headers=f"Proxy-Authorization: {BASIC}\r\n",
        )
        self.assertTrue(wait_until(lambda: len(runtime.view) > 0))
        for flow in runtime.call(lambda: list(runtime.view)):
            self.assertNotIn("proxyauth", flow.metadata)

    def test_the_ca_download_endpoint_stays_reachable_without_credentials(self) -> None:
        """**刻意的绕过，不要「修」掉。**

        `CertDownloadAddon.request` 晚于 ProxyAuth 的 ``requestheaders`` 一拍，
        无条件覆写 ``flow.response``，407 被顶掉。要的就是这个：没装证书的手机
        得先能把证书下下来，不能被认证挡在门外；而这个端点只吐一份公开的 CA
        公钥证书，本来就不含秘密。
        """
        runtime = self._authed()
        response = http_through(
            runtime.listen_port, f"http://{CertDownloadAddon.HOST}/"
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"), response)
        self.assertIn(b"application/x-x509-ca-cert", response)

    def test_turning_the_credential_off_at_runtime_lifts_the_challenge(self) -> None:
        """热更关闭：选项回 None，同一个客户端不再吃 407（内核不重启）。"""
        origin = self._origin()
        runtime = self._authed()
        master = runtime._master
        assert master is not None

        runtime.apply_proxy_auth(enabled=False)

        self.assertIsNone(runtime.call(lambda: master.options.proxyauth))
        response = http_through(
            runtime.listen_port, f"http://127.0.0.1:{origin.port}/probe"
        )
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"), response)

    def test_turning_it_off_keeps_the_stored_credential(self) -> None:
        """关掉认证不清空用户名密码：下次勾上还是原来那套。"""
        runtime = self._authed()
        runtime.apply_proxy_auth(enabled=False)
        self.assertEqual(runtime.proxyauth_username, "alice")
        self.assertEqual(runtime.proxyauth_password, "secret")
        runtime.apply_proxy_auth(enabled=True)
        master = runtime._master
        assert master is not None
        self.assertEqual(runtime.call(lambda: master.options.proxyauth), "alice:secret")

    def test_engaging_reverse_yields_and_disengaging_restores(self) -> None:
        """让路必须随通道热切换即时生效（同一次 ``options.update`` 推下去）。

        三条互斥通道里只跑 reverse：local 要提权装驱动、wireguard 要开 UDP 口，
        CI 上起不来，而三者共用同一个判据同一行代码，矩阵已由
        `ProxyAuthGateTests` 钉死 —— 这里只证明判据真的接到了 options 上。
        """
        runtime = self._authed()
        master = runtime._master
        assert master is not None

        runtime.apply_channels(
            use_reverse=True,
            reverse_target="https://example.com",
            reverse_port=free_port(),
        )
        runtime.set_channels_engaged(True)
        self.assertIsNone(runtime.call(lambda: master.options.proxyauth))

        runtime.set_channels_engaged(False)
        self.assertEqual(runtime.call(lambda: master.options.proxyauth), "alice:secret")


if __name__ == "__main__":
    unittest.main()
