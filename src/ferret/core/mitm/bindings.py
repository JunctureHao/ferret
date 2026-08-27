"""Centralized mitmproxy imports and packaging compatibility shims."""

import os
import posixpath
import sys
from types import ModuleType
from typing import Any

# werkzeug 的目录穿越守卫，照搬 werkzeug/security.py:12-25,169-201（BSD-3-Clause,
# © Pallets）。mitmproxy 整个包里只有 maplocal.py:9 一句 `from werkzeug.security import
# safe_join`，却把 34 个 werkzeug 子模块（serving / test / formparser / datastructures /
# wrappers 全在内）连同它独占的 colorama、markupsafe 一起拖进包里 —— 是目前构建里最大的
# 一块纯废重。这是安全边界，所以只做等价搬运：不自己发挥，也不改写成 pathlib.resolve()
# （那会碰文件系统，而 maplocal 送进来的候选路径本来就允许不存在）。
# 升级 mitmproxy / werkzeug 之后要回头比对上游这个函数有没有变。
_OS_ALT_SEPS: list[str] = [
    sep for sep in (os.sep, os.altsep) if sep is not None and sep != "/"
]
# https://chrisdenton.github.io/omnipath/Special%20Dos%20Device%20Names.html
_WINDOWS_DEVICE_FILES = {
    "AUX",
    "CON",
    "CONIN$",
    "CONOUT$",
    *(f"COM{c}" for c in "123456789¹²³"),
    *(f"LPT{c}" for c in "123456789¹²³"),
    "NUL",
    "PRN",
}


def _safe_join(directory: str, *pathnames: str) -> str | None:
    """Join untrusted segments onto ``directory``; ``None`` 表示这条路径不安全."""
    if not directory:
        # 保证结果是 ./path：directory 为空时，第一段不可信路径不能升格成可信前缀。
        directory = "."
    parts = [directory]
    for part in pathnames:
        if not part:
            continue
        part = posixpath.normpath(part)
        if (
            os.path.isabs(part)
            # ntpath.isabs 抓不到这一种。上游写成两句 startswith，这里并成元组形式
            # 以过 ruff 的 PIE810，语义完全一致。
            or part.startswith(("/", "../"))
            or part == ".."
            or any(sep in part for sep in _OS_ALT_SEPS)
            or (
                os.name == "nt"
                and any(
                    p.partition(".")[0].strip().upper() in _WINDOWS_DEVICE_FILES
                    for p in part.split("/")
                )
            )
        ):
            return None
        parts.append(part)
    return posixpath.join(*parts)


# 打包瘦身：这些模块 ferret 永不使用，却会被 mitmproxy.addons.__init__、
# mitmproxy.addons.export 和 mitmproxy.master 在导入期拉进来。用桩顶替以配合
# __main__.py 的 --nofollow-import-to，必须在任何 mitmproxy 导入之前完成。
# - pyperclip 仅被 Export.clip / Cut.clip 使用，ferret 走自己的 Qt 剪贴板，两者都不调。
# - browser / command_history / termlog 是 mitmproxy 自家命令行界面用的（拉浏览器、
#   命令历史、终端日志），FerretMaster 明确 with_termlog=False。
# - comment 里只有一条 `flow.comment` 控制台命令；comment 属性本身长在
#   mitmproxy/flow.py 的 Flow 上，ferret 在 facade.py 里直接赋值，不经过这个 addon。
# - werkzeug 见上面的 _safe_join。colorama / markupsafe 只有 werkzeug 引用，桩掉
#   werkzeug 之后它们自然不可达，不必单独立桩。
# 别顺手把 script 也桩了：脚本功能后面要做，留着。
_STUBBED_MODULES: dict[str, dict[str, Any]] = {
    "mitmproxy.addons.browser": {},
    "mitmproxy.addons.command_history": {},
    "mitmproxy.addons.comment": {},
    "mitmproxy.addons.onboarding": {},
    "mitmproxy.addons.onboardingapp": {"app": None},
    "mitmproxy.addons.proxyauth": {},
    "mitmproxy.addons.cut": {},
    # mitmproxy/master.py:25 的类注解 `termlog.TermLog | None` 在导入期就求值（那个
    # 文件没写 from __future__ import annotations），所以桩必须带上这个名字。
    "mitmproxy.addons.termlog": {"TermLog": type("TermLog", (), {})},
    "pyperclip": {"copy": None, "PyperclipException": Exception},
    "werkzeug": {},
    "werkzeug.security": {"safe_join": _safe_join},
}

for _name, _attrs in _STUBBED_MODULES.items():
    _stub = ModuleType(_name)
    # 让桩也算个包，werkzeug.security 才能作为 werkzeug 的子模块被 from-import 找到。
    _stub.__path__ = []  # type: ignore[attr-defined]
    for _attr, _value in _attrs.items():
        setattr(_stub, _attr, _value)
    sys.modules.setdefault(_name, _stub)

from mitmproxy import certs, connection, contentviews, ctx, io
from mitmproxy.addons import export as export_module
from mitmproxy.addons import tlsconfig as _tlsconfig_module
from mitmproxy.addons.anticache import AntiCache
from mitmproxy.addons.anticomp import AntiComp
from mitmproxy.addons.block import Block
from mitmproxy.addons.clientplayback import (
    ClientPlayback,
    ReplayHandler,
)
from mitmproxy.addons.core import Core
from mitmproxy.addons.disable_h2c import DisableH2C
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.intercept import Intercept
from mitmproxy.addons.maplocal import MapLocal, parse_map_local_spec
from mitmproxy.addons.mapremote import MapRemote, parse_map_remote_spec
from mitmproxy.addons.modifybody import ModifyBody
from mitmproxy.addons.modifyheaders import ModifyHeaders, parse_modify_spec
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.readfile import ReadFile
from mitmproxy.addons.save import Save
from mitmproxy.addons.savehar import SaveHar
from mitmproxy.addons.strip_dns_https_records import StripDnsHttpsRecords
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.view import View
from mitmproxy.exceptions import (
    AddonHalt,
    CommandError,
    FlowReadException,
    OptionsError,
)
from mitmproxy.flow import Flow
from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import Headers, HTTPFlow, Request, Response
from mitmproxy.master import Master
from mitmproxy.net.http import status_codes

# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from mitmproxy.net.http import url as http_url
from mitmproxy.net.http.http1.assemble import (
    assemble_request_head,
    assemble_response_head,
)
from mitmproxy.options import KEY_SIZE, Options
from mitmproxy.proxy import server_hooks
from mitmproxy.utils import emoji, human
from mitmproxy.websocket import WebSocketData, WebSocketMessage
from wsproto.frame_protocol import Opcode

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
    "AddonHalt",
    "AntiCache",
    "AntiComp",
    "Block",
    "ClientPlayback",
    "CommandError",
    "Core",
    "DisableH2C",
    "DnsResolver",
    "Flow",
    "FlowReadException",
    "HTTPFlow",
    "Headers",
    "Intercept",
    "MapLocal",
    "MapRemote",
    "Master",
    "ModifyBody",
    "ModifyHeaders",
    "NextLayer",
    "Opcode",
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
    "WebSocketData",
    "WebSocketMessage",
    "assemble_request_head",
    "assemble_response_head",
    "certs",
    "connection",
    "contentviews",
    "emoji",
    "export_module",
    "http_url",
    "human",
    "io",
    "parse_filter",
    "parse_map_local_spec",
    "parse_map_remote_spec",
    "parse_modify_spec",
    "server_hooks",
    "status_codes",
    "tlsconfig_module",
]
