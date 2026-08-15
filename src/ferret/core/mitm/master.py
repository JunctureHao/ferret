"""mitmproxy Master assembly used by Ferret."""

import asyncio

from ferret.core.mitm.addons import FerretTlsConfig, LogAddon
from ferret.core.mitm.bindings import (
    ClientPlayback,
    Core,
    DnsResolver,
    Master,
    NextLayer,
    Options,
    Proxyserver,
    ReadFile,
    Save,
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
        self.save = Save()

        self.addons.add(
            Core(),
            self.proxyserver,
            FerretTlsConfig(),
            NextLayer(),
            DnsResolver(),
            self.view,
            self.readfile,
            self.client_playback,
            self.save,
            LogAddon(),
        )


CaptureMaster = FerretMaster
