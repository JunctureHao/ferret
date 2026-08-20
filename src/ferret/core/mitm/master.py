"""mitmproxy Master assembly used by Ferret."""

import asyncio

from ferret.core.mitm.addons import FerretTlsConfig, LogAddon
from ferret.core.mitm.bindings import (
    AntiCache,
    AntiComp,
    Block,
    BlockList,
    ClientPlayback,
    Core,
    DisableH2C,
    DnsResolver,
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
        self.block_list = BlockList()
        self.save = Save()

        self.addons.add(
            Core(),
            # 位置对齐原生 default_addons()（core → block → strip_dns_https_records）：
            # Block 只挂 client_connected，必须在任何流量成形之前决定放不放这条连接。
            Block(),
            StripDnsHttpsRecords(),
            self.block_list,
            AntiCache(),
            AntiComp(),
            self.client_playback,
            DisableH2C(),
            self.proxyserver,
            DnsResolver(),
            NextLayer(),
            FerretTlsConfig(),
            self.view,
            self.readfile,
            self.save,
            LogAddon(),
        )


CaptureMaster = FerretMaster
