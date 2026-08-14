"""mitmproxy runtime assembly used by Ferret applications."""

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
    View,
)


class CaptureMaster(Master):
    """Minimal mitmproxy master with Ferret's required addons."""

    def __init__(
        self,
        opts: Options | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        view: View | None = None,
    ) -> None:
        super().__init__(opts, event_loop=event_loop, with_termlog=False)
        self.view = view if view is not None else View()
        self.addons.add(
            Core(),
            Proxyserver(),
            FerretTlsConfig(),
            NextLayer(),
            DnsResolver(),
            self.view,
            ClientPlayback(),
            LogAddon(),
        )
