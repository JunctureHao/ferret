"""Centralized mitmproxy imports and packaging compatibility shims."""

import sys
from types import ModuleType
from typing import Any

_STUBBED_ADDONS = {
    "mitmproxy.addons.onboarding": (),
    "mitmproxy.addons.onboardingapp": ("app",),
    "mitmproxy.addons.proxyauth": (),
    "mitmproxy.addons.maplocal": (),
    "mitmproxy.addons.cut": (),
    "mitmproxy.addons.export": (),
}

for _name, _attrs in _STUBBED_ADDONS.items():
    _stub = ModuleType(_name)
    for _attr in _attrs:
        setattr(_stub, _attr, None)
    sys.modules.setdefault(_name, _stub)

from mitmproxy import certs, connection, io
from mitmproxy.addons import tlsconfig as _tlsconfig_module
from mitmproxy.addons.clientplayback import (
    ClientPlayback,
    ReplayHandler,
)
from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.readfile import ReadFile
from mitmproxy.addons.save import Save
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.view import View
from mitmproxy.flow import Flow
from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import HTTPFlow, Request, Response
from mitmproxy.master import Master
from mitmproxy.net.http import status_codes
from mitmproxy.net.http.http1.assemble import (
    assemble_request,
    assemble_response,
)
from mitmproxy.options import KEY_SIZE, Options
from mitmproxy.proxy import server_hooks
from mitmproxy.utils import human

tlsconfig_module: Any = _tlsconfig_module

__all__ = [
    "KEY_SIZE",
    "ClientPlayback",
    "Core",
    "DnsResolver",
    "Flow",
    "HTTPFlow",
    "Master",
    "NextLayer",
    "Options",
    "Proxyserver",
    "ReadFile",
    "ReplayHandler",
    "Request",
    "Response",
    "Save",
    "TlsConfig",
    "View",
    "assemble_request",
    "assemble_response",
    "certs",
    "connection",
    "human",
    "io",
    "parse_filter",
    "server_hooks",
    "status_codes",
    "tlsconfig_module",
]
