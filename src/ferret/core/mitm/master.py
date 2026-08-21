"""mitmproxy Master assembly used by Ferret."""

import asyncio

from ferret.core.mitm.addons import (
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
    MapRemote,
    Master,
    NextLayer,
    Options,
    Proxyserver,
    ReadFile,
    Save,
    StripDnsHttpsRecords,
    View,
)


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
        self.save = Save()

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
            # 位置对齐原生 default_addons()（next_layer → mapremote → …→ save →
            # tlsconfig）。同时保证它早于 View.request：流量表第一次上屏拿到的
            # 就已经是重写后的 URL，不会先闪一下原地址。
            self.map_remote,
            FerretTlsConfig(),
            # 必须紧挨在 View 前面：绕行/仅允许靠 AddonHalt 截断这一次派发，从这里
            # 往后（View / ReadFile / Save / LogAddon / UiBridgeAddon）一个都收不到，
            # 前面的 addon 则照常跑完。原生 BlockList 因此也从链上撤掉了 —— 它在
            # 网关**之前**，高优先级的绕行规则否决不了它，屏蔽（出）改由网关自己回响应。
            GatewayL7Addon(self.gateway),
            self.view,
            self.readfile,
            self.save,
            LogAddon(),
        )


CaptureMaster = FerretMaster
