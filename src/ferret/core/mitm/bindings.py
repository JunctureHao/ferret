"""Centralized mitmproxy imports and packaging compatibility shims."""

import sys
from types import ModuleType
from typing import Any

# 打包瘦身：这些模块 ferret 永不使用，却会被 mitmproxy.addons.__init__ 和
# mitmproxy.addons.export 在导入期拉进来。用桩顶替以配合 __main__.py 的
# --nofollow-import-to，必须在任何 mitmproxy 导入之前完成。
# pyperclip 仅被 Export.clip / Cut.clip 使用，ferret 走自己的 Qt 剪贴板，两者都不调。
_STUBBED_MODULES: dict[str, dict[str, Any]] = {
    "mitmproxy.addons.onboarding": {},
    "mitmproxy.addons.onboardingapp": {"app": None},
    "mitmproxy.addons.proxyauth": {},
    "mitmproxy.addons.maplocal": {},
    "mitmproxy.addons.cut": {},
    "pyperclip": {"copy": None, "PyperclipException": Exception},
}

for _name, _attrs in _STUBBED_MODULES.items():
    _stub = ModuleType(_name)
    for _attr, _value in _attrs.items():
        setattr(_stub, _attr, _value)
    sys.modules.setdefault(_name, _stub)

from mitmproxy import certs, connection, contentviews, ctx, io
from mitmproxy.addons import export as export_module
from mitmproxy.addons import tlsconfig as _tlsconfig_module
from mitmproxy.addons.anticache import AntiCache
from mitmproxy.addons.anticomp import AntiComp
from mitmproxy.addons.block import Block
from mitmproxy.addons.blocklist import BlockList
from mitmproxy.addons.blocklist import parse_spec as parse_block_spec
from mitmproxy.addons.clientplayback import (
    ClientPlayback,
    ReplayHandler,
)
from mitmproxy.addons.core import Core
from mitmproxy.addons.disable_h2c import DisableH2C
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.readfile import ReadFile
from mitmproxy.addons.save import Save
from mitmproxy.addons.savehar import SaveHar
from mitmproxy.addons.strip_dns_https_records import StripDnsHttpsRecords
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.view import View
from mitmproxy.exceptions import CommandError, FlowReadException, OptionsError
from mitmproxy.flow import Flow
from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import HTTPFlow, Request, Response
from mitmproxy.master import Master
from mitmproxy.net.http import status_codes
from mitmproxy.options import KEY_SIZE, Options
from mitmproxy.proxy import server_hooks
from mitmproxy.utils import human

tlsconfig_module: Any = _tlsconfig_module

# contentviews 的 make_metadata 无条件读 ctx.options.protobuf_definitions，而
# ctx.options 只由 Master.__init__ 写入（master.py:52）。ferret 的 Master 跑在
# 独立线程，端口被占时压根不会建起来，只读会话页却照样要渲染 body。这里在导入
# 期（主线程，早于 mitm 线程启动）一次性兜底成默认 options：
# - 只在导入期写一次，不在运行期跨线程读写 ctx，不碰“禁止使用 ctx”那条红线；
# - Master 起来后会用自己的 options 覆盖它，protobuf_definitions 照常生效。
# ctx 不列入 __all__：除这处兜底外，其余模块一律不得碰它。
if not hasattr(ctx, "options"):
    ctx.options = Options()

__all__ = [
    "KEY_SIZE",
    "AntiCache",
    "AntiComp",
    "Block",
    "BlockList",
    "ClientPlayback",
    "CommandError",
    "Core",
    "DisableH2C",
    "DnsResolver",
    "Flow",
    "FlowReadException",
    "HTTPFlow",
    "Master",
    "NextLayer",
    "Options",
    "OptionsError",
    "Proxyserver",
    "ReadFile",
    "ReplayHandler",
    "Request",
    "Response",
    "Save",
    "SaveHar",
    "StripDnsHttpsRecords",
    "TlsConfig",
    "View",
    "certs",
    "connection",
    "contentviews",
    "export_module",
    "human",
    "io",
    "parse_block_spec",
    "parse_filter",
    "server_hooks",
    "status_codes",
    "tlsconfig_module",
]
