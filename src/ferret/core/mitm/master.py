"""mitmproxy Master assembly used by Ferret."""

import asyncio

from ferret.core.mitm.addons import (
    CertDownloadAddon,
    FerretTlsConfig,
    GatewayL4Addon,
    GatewayL7Addon,
    GatewayState,
    LogAddon,
)
from ferret.core.mitm.bindings import (
    AntiCache,
    AntiComp,
    Block,
    ClientPlayback,
    Core,
    DisableH2C,
    DnsResolver,
    MapLocal,
    MapRemote,
    Master,
    ModifyBody,
    ModifyHeaders,
    NextLayer,
    Options,
    Proxyserver,
    ReadFile,
    Save,
    StickyAuth,
    StickyCookie,
    StripDnsHttpsRecords,
    View,
)
from ferret.core.mitm.compose import ComposeAddon
from ferret.core.mitm.intercept import FerretIntercept, InterceptState
from ferret.core.mitm.sse import FerretSseAddon


class FerretMaster(Master):
    """Minimal native addon assembly for Ferret's shared runtime."""

    def __init__(
        self,
        opts: Options | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        view: View | None = None,
    ) -> None:
        super().__init__(opts, event_loop=event_loop, with_termlog=False)
        self.view = view if view is not None else View()
        self.proxyserver = Proxyserver()
        self.readfile = ReadFile()
        self.client_playback = ClientPlayback()
        self.gateway = GatewayState()
        self.map_remote = MapRemote()
        self.map_local = MapLocal()
        self.modify_body = ModifyBody()
        self.modify_headers = ModifyHeaders()
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
        self.save = Save()
        self.tls_config = FerretTlsConfig()
        self.cert_download = CertDownloadAddon(self.tls_config)

        self.addons.add(
            Core(),
            # 位置对齐原生 default_addons()（core → block → strip_dns_https_records）：
            # Block 只挂 client_connected，必须在任何流量成形之前决定放不放这条连接。
            Block(),
            StripDnsHttpsRecords(),
            AntiCache(),
            AntiComp(),
            self.client_playback,
            DisableH2C(),
            self.proxyserver,
            DnsResolver(),
            # 只挂 server_connect：连接级屏蔽要赶在真正拨号之前把 server.error 写上。
            # 网关另外两条 L4 策略（仅允许 / 绕行）落在 NextLayer 的 allow_hosts /
            # ignore_hosts 选项上，没有代码。
            GatewayL4Addon(self.gateway),
            NextLayer(),
            # 四个重写 addon 的相对次序对齐原生 default_addons()
            # （next_layer → mapremote → maplocal → modifybody → modifyheaders
            # → save → tlsconfig）。同时保证它们早于 View.request：流量表第一次
            # 上屏拿到的就已经是重写后的 URL/报文，不会先闪一下原始值。
            self.map_remote,
            self.map_local,
            self.modify_body,
            self.modify_headers,
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
            # 必须在网关**之后**：绕行/仅允许命中时 GatewayL7Addon 抛 AddonHalt
            # 截断派发，断点因此收不到这条流量 —— 用户明确说了不管的流量，不该
            # 被断点拦下来。位置对齐原生 console master（intercept → view）。
            self.intercept,
            self.view,
            # 挂在 View 之后：摘除（record=False）要等 View 收录完再执行，靠
            # `loop.call_soon` 排在当前一轮钩子派发之后，次序与链上位置无关。
            self.compose,
            self.sse,
            self.readfile,
            self.save,
            LogAddon(),
        )


CaptureMaster = FerretMaster
