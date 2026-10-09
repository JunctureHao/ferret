"""mitmproxy Master assembly used by Ferret."""

from __future__ import annotations

import asyncio

from ferret.core.mitm.addons import (
    CertDownloadAddon,
    FerretRewriteAddon,
    FerretScriptAddon,
    FerretServerPlayback,
    FerretTlsConfig,
    GatewayL4Addon,
    GatewayL7Addon,
    GatewayState,
    LogAddon,
    ProxyAuthScrubAddon,
)
from ferret.core.mitm.bindings import (
    AntiCache,
    AntiComp,
    Block,
    ClientPlayback,
    Core,
    DisableH2C,
    DnsResolver,
    Flow,
    HTTPFlow,
    Master,
    NextLayer,
    Options,
    ProxyAuth,
    Proxyserver,
    StickyAuth,
    StickyCookie,
    StripDnsHttpsRecords,
    UpdateAltSvc,
    UpstreamAuth,
    View,
)
from ferret.core.mitm.compose import ComposeAddon
from ferret.core.mitm.intercept import FerretIntercept, InterceptState
from ferret.core.mitm.io import (
    FerretReadFile,
    FerretSave,
    flow_import,
    imported_flow_ids,
)
from ferret.core.mitm.sse import FerretSseAddon
from ferret.core.mitm.view import FerretView
from ferret.core.mitm.wireguard_source import WireGuardSourceAddon


class FerretMaster(Master):
    """Minimal native addon assembly for Ferret's shared runtime."""

    def __init__(
        self,
        opts: Options | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        view: View | None = None,
    ) -> None:
        super().__init__(opts, event_loop=event_loop, with_termlog=False)
        # 原生构造器把 LegacyLogEvents 挂到**根** logger 上且从不摘（上游靠
        # PYTEST_CURRENT_TEST 换手，我们不用 pytest）。它把每条日志
        # call_soon_threadsafe 回内核循环、发早已废弃的 add_log 钩子 —— 装上的
        # mitmproxy 里已经没有任何 `def add_log` 收它，纯属空转；更要命的是内核停掉
        # 后循环一关，应用里随便哪句 log 都会从 `Handler.emit` 里抛
        # `RuntimeError: Event loop is closed`（emit 不兜异常，直接穿透调用方）。
        self._legacy_log_events.uninstall()
        self.view = view if view is not None else FerretView()
        self.proxyserver = Proxyserver()
        self.wireguard_source = WireGuardSourceAddon()
        self.readfile = FerretReadFile()
        self.client_playback = ClientPlayback()
        self.gateway = GatewayState()
        # 自研统一重写引擎（docs/design.md#rewrite）：原生 MapRemote / MapLocal /
        # ModifyBody / ModifyHeaders 四件退役，八个类型一个 addon、行序＝执行序。
        self.rewrite = FerretRewriteAddon()
        # 用户脚本扩展（docs/design.md#scripts）：常驻无开关，空列表即全空转。
        self.scripts = FerretScriptAddon()
        # mock 响应池（docs/design.md#mock）：原生 ServerPlayback 的 Ferret
        # 子类，request 钩子按请求哈希命中已录响应直接顶回、不拨上游；空表零副作用，
        # 「开关」就是 flowmap 有没有货。池内容与旋钮由 runtime 播种 / facade 热更，
        # 装载只走方法调用（add_flows / load_flows），`server_replay` 选项那条带
        # 单向闸的文件通道不用。子类只加一件事：被代理认证挑战过的流不作答（#76）。
        self.server_playback = FerretServerPlayback()
        # 固定会话（StickyCookie / StickyAuth）：默认关，开关在设置页。排在重写类
        # 之后 —— 代理补回的 Cookie / Authorization 要压过用户对同名头的重写规则，
        # 否则「会话不丢」这条承诺会被自己的重写页拆台；排在 View 之前 —— 流量表
        # 第一次上屏的请求头就已经含补回的头。jar / hosts 只在 mitm 线程读写。
        self.sticky_cookie = StickyCookie()
        self.sticky_auth = StickyAuth()
        self.intercept_state = InterceptState()
        self.intercept = FerretIntercept(self.intercept_state)
        self.compose = ComposeAddon(self.view)
        # SSE tee：挂在 View 之后、与 Compose 同区。检测点是 responseheaders
        # （源码注释明确 stream 必须在 response 钩子之前换），链上这里照常能收到。
        # bridge 由 runtime 在挂 UiBridgeAddon 时注入（master 装配时还不认识它）。
        self.sse = FerretSseAddon()
        self.sse.on_flow_updated = lambda flow, view=self.view: view.update([flow])
        if isinstance(self.view, FerretView):
            self.view.additional_size = self.sse.memory_size
        self.view.sig_store_remove.connect(self.sse.flow_removed)
        self.compose.on_cancel = self._cancel_compose
        self.save = FerretSave()
        self.tls_config = FerretTlsConfig()
        self.cert_download = CertDownloadAddon(self.tls_config)

        self.addons.add(
            Core(),
            # 位置对齐原生 default_addons()（core → block → strip_dns_https_records）：
            # Block 只挂 client_connected，必须在任何流量成形之前决定放不放这条连接。
            Block(),
            # 下面两个是原生「兼容性垫片」，刻意无 UI 旋钮（docs/design.md#capture
            # §0 的盘点结论，勿再当缺口提出）：
            # - StripDnsHttpsRecords 受 strip_ech 选项管（原生默认 True）：抹掉 DNS
            #   HTTPS 记录里的 ECH 配置是内核签出匹配证书的前提，关掉只会让 ECH 域名
            #   拦截失败；且只在 dns_response 生效，regular 下客户端自解 DNS、全程空转。
            # - DisableH2C 不注册任何 option、恒开：mitmproxy 只认 TLS 上的 HTTP/2，
            #   明文 h2c 升级必须剥头、先验知识前奏必须杀，关掉只会让这类流坏掉。
            #   （http2 选项关的是 TLS 上的 h2 协商，与此正交，开关在设置页。）
            StripDnsHttpsRecords(),
            # 来源标注必须紧跟兼容垫片、远早于 View.request：按 client_conn.proxy_mode
            # 把每条流量钉到对应 WireGuard 设备（见 wireguard_source.py），只读连接元
            # 数据、写 flow.metadata，与下方 AntiCache/AntiComp 的请求改写正交。不插在
            # block→strip_dns_https_records 之间是为保原生相邻序（test_block 钉死）。
            self.wireguard_source,
            AntiCache(),
            AntiComp(),
            self.client_playback,
            DisableH2C(),
            # 代理认证（docs/design.md#auth）：位置对齐原生 default_addons() —— 紧挨
            # proxyserver 之前。挂三个钩子（requestheaders / http_connect /
            # socks5_auth），`proxyauth` 选项为 None 时全部空转，故常驻无开关，
            # 开关在选项侧（MitmRuntime._effective_proxyauth）。
            # 排在网关之前是刻意的：网关规则管「抓什么」，认证管「谁能用」，后者先判。
            # 注意它只挑战 regular / upstream 两种模式（is_http_proxy，
            # addons/proxyauth.py:143-151），local / wireguard / reverse 会吃 401 被
            # 打挂 —— 这三条通道接通期间由 _effective_proxyauth() 让路置 None。
            ProxyAuth(),
            # 紧跟 ProxyAuth：把它写进 flow.metadata 的明文凭证抹掉，越早越好。
            ProxyAuthScrubAddon(),
            self.proxyserver,
            DnsResolver(),
            # 只挂 server_connect：连接级屏蔽要赶在真正拨号之前把 server.error 写上。
            # 网关另外两条 L4 策略（仅允许 / 绕行）落在 NextLayer 的 allow_hosts /
            # ignore_hosts 选项上，没有代码。
            GatewayL4Addon(self.gateway),
            NextLayer(),
            # 自研重写件顶替原生四件的原链位（next_layer → rewrite → save →
            # tlsconfig，对齐原生 default_addons() 的相对位置）。同时保证它早于
            # View.request：流量表第一次上屏拿到的就已经是重写后的 URL/报文，
            # 不会先闪一下原始值。
            self.rewrite,
            self.sticky_cookie,
            self.sticky_auth,
            self.tls_config,
            self.cert_download,
            # 必须排在 View 之前：绕行/仅允许靠 AddonHalt 截断这一次派发，从这里
            # 往后（Intercept / View / ReadFile / Save / LogAddon / UiBridgeAddon）
            # 一个都收不到，前面的 addon 则照常跑完。原生 BlockList 因此也从链上撤掉
            # 了 —— 它在网关**之前**，高优先级的绕行规则否决不了它，屏蔽（出）改由
            # 网关自己回响应。
            GatewayL7Addon(self.gateway),
            # 脚本必须排在网关之后：绕行/仅允许命中时 AddonHalt 截断派发，脚本
            # 收不到用户明确说了不管的流量（与下面 intercept 同一语义）；也必须
            # 排在重写之后：脚本拿到的是重写**后**的报文，与 View/断点所见一致
            # （钉死语义，见 docs/design.md#scripts）。
            self.scripts,
            # mock 响应池（docs/design.md#mock）链位四条理由：
            # 1) 必须在网关**之后** —— 绕行/仅允许命中时 AddonHalt 截断派发，用户
            #    明确不管的流量不该被 mock（与下面 scripts / intercept 同一语义）。
            #    这也是它不能照抄原生链位（next_layer 之后、modify* 之前）的原因。
            # 2) 在 scripts **之后**（原生相邻序 script → serverplayback）：脚本
            #    请求钩子先改报文，mock 按改后请求匹配；mock 出的响应照常过脚本
            #    响应钩子与响应期重写规则（rewrite 在网关之前）——可叠加。
            # 3) 在 intercept **之前**：两者同名 `request` 钩子且整条链派发完才
            #    wait_for_resume，断点拦住的流已带上 mock 响应 ——「断点所见 =
            #    最终结果」，与「断点所见与 View 一致」的既有哲学同构。
            # 4) 它不看 flow.response 就直接覆盖（原生如此）：同时被网关屏蔽(出)
            #    又命中 mock 的流量，mock 会顶掉屏蔽响应 —— 接受「mock 优先」语义。
            self.server_playback,
            # 必须在网关**之后**：绕行/仅允许命中时 GatewayL7Addon 抛 AddonHalt
            # 截断派发，断点因此收不到这条流量 —— 用户明确说了不管的流量，不该
            # 被断点拦下来。位置对齐原生 console master（intercept → view）。
            self.intercept,
            self.view,
            # 反向代理通道（docs/design.md#capture）：reverse 流被 `isinstance(
            # ReverseMode)` 闸死，对非 reverse 流零副作用；位置在网关后、intercept
            # 前，命中绕行的流到不了它（AddonHalt 截断在前），与原生 default_addons()
            # 尾序（tlsconfig → upstream_auth → update_alt_svc）一致。常驻无开关。
            # 上游代理凭证注入（出口经企业代理时的 Basic 认证）：只认 upstream /
            # reverse 两种模式（upstream_auth.py:40-58），`upstream_auth` 选项为
            # None 时全钩子空转，故常驻无开关。但它**不是**只靠模式闸门就零副作用
            # —— 选项非空时 reverse 通道的请求也会被补上 `Authorization` 头
            # （upstream_auth.py:56-58），即把上游代理的密码发给 reverse 目标。原生
            # 分不开这两者，所以闸门在选项侧：MitmRuntime._upstream_auth() 在上游
            # 关闭时恒回 None，同开时由对话框给出警告。
            UpstreamAuth(),
            UpdateAltSvc(),
            # 挂在 View 之后：摘除（record=False）要等 View 收录完再执行，靠
            # `loop.call_soon` 排在当前一轮钩子派发之后，次序与链上位置无关。
            self.compose,
            self.sse,
            self.readfile,
            self.save,
            LogAddon(),
        )

    async def load_flow(self, f: Flow) -> None:
        imported = imported_flow_ids()
        if imported is not None:
            existing = self.view.get_by_id(f.id)
            if existing is not None and existing is not f:
                # Historical files can contain successive versions of one ID.
                # Keep the last state in the original stored object, so the
                # table and every native lifecycle hook share its identity.
                # An archive must never overwrite a live request or breakpoint.
                if (
                    existing.live
                    or existing.intercepted
                    or type(existing) is not type(f)
                ):
                    return
                previous = existing.get_state()
                existing.set_state(f.get_state())
                if isinstance(existing, HTTPFlow):
                    # Admission is per record, not per ID: an earlier version
                    # from this same file may already be in the import ledger.
                    # Gateway BYPASS/ALLOW_ONLY can halt every View hook of the
                    # new version; keep the previously admitted state then.
                    admitted: set[str] = set()
                    try:
                        with flow_import(admitted):
                            await super().load_flow(existing)
                    finally:
                        if existing.id not in admitted:
                            existing.set_state(previous)
                        imported.update(admitted)
                    return
                f = existing
        await super().load_flow(f)

    def _cancel_compose(self, flow: HTTPFlow) -> None:
        self.gateway.release([flow.id])
        self.intercept_state.release([flow.id])
        self.sse.error(flow)
        self.view.update([flow])


CaptureMaster = FerretMaster
