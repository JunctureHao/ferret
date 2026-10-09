"""Centralized mitmproxy imports and packaging compatibility shims."""

from __future__ import annotations

import os
import posixpath
import sys
import zlib
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
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
# 注意 maplocal / mapremote / modifybody / modifyheaders 四个模块**没有**随重写
# 引擎自研（docs/design.md#rewrite）而立桩：`mitmproxy.addons.__init__` 在导入期
# 无条件 import 它们，ferret 这边已经不再引用（重写引擎见 core/mitm/rewrite.py
# 与 addons.py::FerretRewriteAddon），但桩掉它们需要连 addons.__init__ 一起桩，
# 得不偿失。
# - pyperclip 仅被 Export.clip / Cut.clip 使用，ferret 走自己的 Qt 剪贴板，两者都不调。
# - browser / command_history / termlog 是 mitmproxy 自家命令行界面用的（拉浏览器、
#   命令历史、终端日志），FerretMaster 明确 with_termlog=False。
# - comment 里只有一条 `flow.comment` 控制台命令；comment 属性本身长在
#   mitmproxy/flow.py 的 Flow 上，ferret 在 facade.py 里直接赋值，不经过这个 addon。
# - werkzeug 见上面的 _safe_join。colorama / markupsafe 只有 werkzeug 引用，桩掉
#   werkzeug 之后它们自然不可达，不必单独立桩。
# - ldap3 / mitmproxy.utils.htpasswd 是 proxyauth 的两条弃用分支，见下面的就地注释。
# script 保持不桩（脚本功能已落地 core/mitm/scripts.py，docs/design.md#scripts）：
# 我们从不 import mitmproxy.addons.script，它必须留着不桩。
# 也别顺手把 proxyauth 桩回去：它已经解桩装载（master.py 的 ProxyAuth()）。
_STUBBED_MODULES: dict[str, dict[str, Any]] = {
    "mitmproxy.addons.browser": {},
    "mitmproxy.addons.command_history": {},
    "mitmproxy.addons.comment": {},
    "mitmproxy.addons.onboarding": {},
    "mitmproxy.addons.onboardingapp": {"app": None},
    "mitmproxy.addons.cut": {},
    # mitmproxy/master.py:25 的类注解 `termlog.TermLog | None` 在导入期就求值（那个
    # 文件没写 from __future__ import annotations），所以桩必须带上这个名字。
    "mitmproxy.addons.termlog": {"TermLog": type("TermLog", (), {})},
    # proxyauth 装载了，但 ferret 只下发 "user:pass" 单用户格式，htpasswd / ldap /
    # "any" 三种 spec 都不产出（UI 无入口，_effective_proxyauth 只拼这一种），所以把
    # 它那两条永不可达的分支反向桩掉 —— 否则 addons/proxyauth.py 的两句模块级导入
    # （`import ldap3` 与 `from mitmproxy.utils import htpasswd`）会把 ldap3 整包拖进
    # 构建，而 utils/htpasswd.py 的模块级 `import bcrypt` 会在 exe 里直接炸
    # （bcrypt 带 --nofollow-import-to，__main__.py:81）。
    # 两个桩都是死代码：Ldap 在 proxyauth 里只出现在 from __future__ annotations 延迟
    # 求值的类注解里；htpasswd 在整个 mitmproxy 里只有 proxyauth 一个消费者。
    # 哪天要开 htpasswd / ldap 入口，先把这两行删掉再说。
    "ldap3": {},
    "mitmproxy.utils.htpasswd": {},
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

from mitmproxy import addonmanager, certs, connection, contentviews, ctx, hooks, io
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
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyauth import ProxyAuth
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.readfile import ReadFile
from mitmproxy.addons.save import Save
from mitmproxy.addons.savehar import SaveHar
from mitmproxy.addons.serverplayback import ServerPlayback
from mitmproxy.addons.stickyauth import StickyAuth
from mitmproxy.addons.stickycookie import StickyCookie
from mitmproxy.addons.strip_dns_https_records import StripDnsHttpsRecords
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.update_alt_svc import UpdateAltSvc
from mitmproxy.addons.upstream_auth import UpstreamAuth
from mitmproxy.addons.view import View
from mitmproxy.dns import DNSFlow
from mitmproxy.exceptions import (
    AddonHalt,
    CommandError,
    FlowReadException,
    OptionsError,
)
from mitmproxy.flow import Flow
from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import Headers, HTTPFlow, Request, Response
from mitmproxy.log import MitmLogHandler
from mitmproxy.master import Master
from mitmproxy.net import encoding as _net_encoding

# mTLS 唯一触点：net_tls.create_proxy_server_context.cache_clear。该函数是模块级的
# @lru_cache(256)，键里只有 client_cert 的**路径字符串** —— 路径不变、原地换掉证书
# 文件内容，旧上下文会被继续复用；缓存又挂在模块上，跨 MitmRuntime.restart 存活，
# 「停止抓包 → 换证书 → 重新开始」也清不掉。所以下发 client_certs 前后都得手动清。
# 见 docs/design.md#tls。
from mitmproxy.net import tls as net_tls
from mitmproxy.net.http import status_codes

# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from mitmproxy.net.http import url as http_url
from mitmproxy.net.http.headers import infer_content_encoding
from mitmproxy.net.http.http1.assemble import (
    assemble_request_head,
    assemble_response_head,
)
from mitmproxy.options import KEY_SIZE, Options
from mitmproxy.proxy import server_hooks
from mitmproxy.proxy.mode_servers import (
    LocalRedirectorInstance,
    WireGuardServerInstance,
)
from mitmproxy.proxy.mode_specs import ProxyMode, UpstreamMode
from mitmproxy.tcp import TCPFlow
from mitmproxy.udp import UDPFlow
from mitmproxy.utils import emoji, human, signals
from mitmproxy.version import FLOW_FORMAT_VERSION
from mitmproxy.websocket import WebSocketData, WebSocketMessage
from mitmproxy_rs import process_info as rs_process_info
from mitmproxy_rs import wireguard as rs_wireguard
from wsproto.frame_protocol import Opcode

tlsconfig_module: Any = _tlsconfig_module

# Upstream's one-entry encoding cache has no byte ceiling. Keep native codecs
# and their full-body behaviour, but do not let an export/rewrite pin its last
# huge encoded/decoded pair after the caller releases the result. This runs at
# the codec boundary on the calling thread, never from a GUI cleanup timer.
ENCODING_CACHE_LIMIT = 2 * 1024 * 1024
_native_encoding: Any = _net_encoding
_native_decode = _native_encoding.decode
_native_encode = _native_encoding.encode


def _trim_encoding_cache() -> None:
    cached = _native_encoding._cache
    if (
        sys.getsizeof(cached.encoded) + sys.getsizeof(cached.decoded)
        > ENCODING_CACHE_LIMIT
    ):
        _native_encoding._cache = _native_encoding.CachedDecode(None, None, None, None)


class PreviewDecodingUnavailable(Exception):
    """The codec cannot enforce a preview output/window budget."""


@dataclass
class _DecodeBudget:
    limit: int
    incomplete_input: bool


_decode_budget: ContextVar[_DecodeBudget | None] = ContextVar(
    "ferret_body_decode_budget", default=None
)


@contextmanager
def bounded_content_decoding(
    limit: int, *, incomplete_input: bool = False
) -> Generator[None]:
    """Scope native Message.get_content/get_text calls to a preview budget.

    Only this context uses bounded variants of upstream's compression codecs.
    ContextVar keeps concurrent exports and mitm callbacks on their native path;
    preview decoding bypasses the shared cache, including existing full results.
    The caller supplies at most limit + 1 encoded bytes and checks the sentinel
    output byte. Unknown codecs and Brotli have no bounded Python output API,
    so preview is explicitly deferred instead of decompressing then slicing.
    """
    token = _decode_budget.set(_DecodeBudget(limit, incomplete_input))
    try:
        yield
    finally:
        _decode_budget.reset(token)


def _bounded_zlib(data: bytes, encoding: str, budget: _DecodeBudget) -> bytes:
    windows = (47,) if encoding == "gzip" else (15, -15)
    for window in windows:
        try:
            decoder = zlib.decompressobj(window)
            decoded = decoder.decompress(data, budget.limit + 1)
            # Native gzip permits a partial stream; native deflate only permits
            # it here when *our* encoded-input budget shortened a valid body.
            if (
                encoding != "gzip"
                and not decoder.eof
                and not budget.incomplete_input
                and len(decoded) <= budget.limit
            ):
                raise zlib.error("incomplete deflate stream")
            return decoded
        except zlib.error:
            if window == windows[-1]:
                raise ValueError("Invalid compressed preview") from None
    raise AssertionError("No zlib window configured")


@wraps(_native_decode)
def _decode_with_budget(encoded, encoding: str, errors: str = "strict"):
    budget = _decode_budget.get()
    if budget is not None and isinstance(encoded, bytes):
        name = encoding.lower()
        if name in ("gzip", "deflate", "deflateraw"):
            return _bounded_zlib(encoded, name, budget) if encoded else b""
        if name == "zstd":
            if not encoded:
                return b""
            try:
                # Same streaming decoder as upstream, with a bounded read and
                # an 8 MiB window ceiling (bytes in the installed C backend).
                decoder = _native_encoding.zstd.ZstdDecompressor(
                    max_window_size=8 * 1024 * 1024
                )
                with decoder.stream_reader(encoded, read_across_frames=True) as reader:
                    return reader.read(budget.limit + 1)
            except _native_encoding.zstd.ZstdError:
                raise PreviewDecodingUnavailable(name) from None
        if name == "br":
            raise PreviewDecodingUnavailable(name)
        if name not in ("none", "identity"):
            try:
                codec = _native_encoding.codecs.lookup(name)
            except LookupError:
                raise ValueError("Unknown preview charset") from None
            if not codec._is_text_encoding:
                # A bogus charset such as bz2 must not provide a second,
                # unbounded decompression route through native get_text.
                raise ValueError("Non-text preview charset")
    try:
        return _native_decode(encoded, encoding, errors)
    finally:
        _trim_encoding_cache()


@wraps(_native_encode)
def _encode_with_cache_limit(decoded, encoding, errors="strict"):
    try:
        return _native_encode(decoded, encoding, errors)
    finally:
        _trim_encoding_cache()


_native_encoding.decode = _decode_with_budget
_native_encoding.encode = _encode_with_cache_limit

# 目录穿越守卫（werkzeug 等价实现，见上面的 _safe_join）：自研重写引擎的文件映射
# 按原生 MapLocal 的候选算法取路径，URL 后缀是不可信输入，必须走同一道守卫。
safe_join = _safe_join

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
    "ENCODING_CACHE_LIMIT",
    "FLOW_FORMAT_VERSION",
    "KEY_SIZE",
    "AddonHalt",
    "AntiCache",
    "AntiComp",
    "Block",
    "ClientPlayback",
    "CommandError",
    "Core",
    "DNSFlow",
    "DisableH2C",
    "DnsResolver",
    "Flow",
    "FlowReadException",
    "HTTPFlow",
    "Headers",
    "Intercept",
    "LocalRedirectorInstance",
    "Master",
    "MitmLogHandler",
    "NextLayer",
    "Opcode",
    "Options",
    "OptionsError",
    "PreviewDecodingUnavailable",
    "ProxyAuth",
    "ProxyMode",
    "Proxyserver",
    "ReadFile",
    "ReplayHandler",
    "Request",
    "Response",
    "Save",
    "SaveHar",
    "ServerPlayback",
    "StickyAuth",
    "StickyCookie",
    "StripDnsHttpsRecords",
    "TCPFlow",
    "TlsConfig",
    "UDPFlow",
    "UpdateAltSvc",
    "UpstreamAuth",
    "UpstreamMode",
    "View",
    "WebSocketData",
    "WebSocketMessage",
    "WireGuardServerInstance",
    "addonmanager",
    "assemble_request_head",
    "assemble_response_head",
    "bounded_content_decoding",
    "certs",
    "connection",
    "contentviews",
    "emoji",
    "export_module",
    "hooks",
    "http_url",
    "human",
    "infer_content_encoding",
    "io",
    "net_tls",
    "parse_filter",
    "rs_process_info",
    "rs_wireguard",
    "server_hooks",
    "signals",
    "status_codes",
    "tlsconfig_module",
]
