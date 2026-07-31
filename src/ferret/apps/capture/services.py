import asyncio
import os
import re
import shlex
import subprocess
import sys
import weakref
import zlib
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

_STUBBED_ADDONS = {
    # onboarding.py 顶层 import asgiapp(依赖 asgiref)，整枝屏蔽
    "mitmproxy.addons.onboarding": (),
    # onboarding.py 里有 `from mitmproxy.addons.onboardingapp import app`，桩需提供 app 属性
    "mitmproxy.addons.onboardingapp": ("app",),
    "mitmproxy.addons.proxyauth": (),
    # maplocal.py 顶层 `from werkzeug.security import safe_join`，werkzeug 被排除需整枝屏蔽
    "mitmproxy.addons.maplocal": (),
    "mitmproxy.addons.cut": (),
    # export.py 顶层 import pyperclip；FlowExporter 已本地化（见本文件下方），不再依赖它
    "mitmproxy.addons.export": (),
}

for _name, _attrs in _STUBBED_ADDONS.items():
    _stub = ModuleType(_name)
    for _attr in _attrs:
        setattr(_stub, _attr, None)
    sys.modules.setdefault(_name, _stub)


# ─────────────────────────────────────────────────────────────
# 单一 mitmproxy 导入出口
# 所有 mitmproxy 相关的 import 都集中在这里，确保桩注入（上方）在任何
# mitmproxy.addons.* 子模块导入之前完成，避免打包后 mitmproxy.addons.__init__
# 触发被排除模块（cut/export/...）而崩溃。其它模块一律从本文件引入这些符号，
# 不要直接 import mitmproxy。
# ─────────────────────────────────────────────────────────────
from mitmproxy import certs
from mitmproxy.addons.clientplayback import ClientPlayback, ReplayHandler  # noqa: F401
from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.view import View
from mitmproxy.flow import Flow
from mitmproxy.http import HTTPFlow, Request, Response
from mitmproxy.master import Master
from mitmproxy.net.http.http1.assemble import assemble_request, assemble_response
from mitmproxy.options import CONF_BASENAME, CONF_DIR, KEY_SIZE, Options

from ferret.utils.http_parser import (
    build_body,
    parse_cookies_from_headers,
    parse_params,
)
from ferret.utils.process_resolver import resolve_process


class _HTTPOnlyFilter:
    """满足 flowfilter.TFilter 协议（需要 pattern 属性）的 HTTP 流量过滤器。"""

    pattern = "~http"

    def __call__(self, f: Flow) -> bool:
        return isinstance(f, HTTPFlow)


def _safe_content(message) -> bytes:
    """安全获取解压后的 content，解码失败时回退到 raw_content。"""
    try:
        return message.content or b""
    except (ValueError, zlib.error):
        return message.raw_content or b""


class CaptureMaster(Master):
    """最小化的抓包主控。

    用法：
        opts = Options(listen_host="127.0.0.1", listen_port=8080)
        master = CaptureMaster(opts)
        master.addons.add(my_traffic_addon)
        await master.run()          # 在 asyncio 事件循环里运行
        # 停止：master.shutdown()
    """

    def __init__(
        self,
        opts: Options | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
        view: View | None = None,
    ) -> None:
        # with_termlog=False → 不要终端日志
        super().__init__(opts, event_loop=event_loop, with_termlog=False)

        # 只挂载代理服务必需的最小 addon（5 个底座）+
        # view（flow 存储/过滤/排序）：
        # core（事件派发）/ proxyserver（起端口转发）/ tlsconfig（HTTPS 解密）/
        # next_layer（协议分层）/ dns_resolver（DNS 解析）/ view（flow 视图）。
        # 其余能力型 addon（改包/映射/拦截/保存/回放等）按需再单独添加。
        # 如果外部传入了 view，则复用它（用于跨 toggle 保留数据）
        self.view = view if view is not None else View()
        # View 默认会接收 TCP/UDP/DNS/HTTP 等所有 flow，
        # 本项目只展示 HTTP/HTTPS 流量，所以过滤只保留 HTTPFlow。
        # 只有在创建新 View 时才设置过滤器
        if view is None:
            self.view.set_filter(_HTTPOnlyFilter())

        self.addons.add(
            Core(),
            Proxyserver(),
            TlsConfig(),
            NextLayer(),
            DnsResolver(),
            self.view,
            ClientPlayback(),
        )


class UiBridgeAddon:
    """把 mitmproxy 事件钩子桥接给 Qt 信号（UI 作为真正的 addon）。

    设计：
    * 本类是一个标准的 mitmproxy addon——只要实现了 ``request`` / ``response``
      / ``error`` 这类事件钩子方法，被 ``master.addons.add(...)`` 注册后，
      mitmproxy 事件循环会在对应时机自动回调，无需依赖内置 ``View`` 的
      ``SyncSignal``。这让我们能直接拿到原生 ``HTTPFlow`` 对象，并接入
      mitmproxy 的 addon 生态（``mitmproxy.ctx``、options 等）。
    * 钩子里**只发射 flow 引用（不转 dict、不做重解析）**，重活在 UI 真正
      需要时才发生，避免阻塞代理事件循环、拖慢吞吐。
    * 移除/刷新没有对应的普通 addon 钩子，继续由 ``View`` 的 ``SyncSignal``
      转发，保持与表格模型的存储/排序/过滤一致。

    ``bridge`` 需提供 4 个 Qt ``Signal``：``flow_added`` / ``flow_updated`` /
    ``flow_removed``(object, int) / ``view_refreshed``，通常由
    ``CaptureController`` 充当。
    """

    def __init__(self, view: View, bridge: Any) -> None:
        self.view = view
        self.bridge = bridge
        # 移除/刷新：mitmproxy 无对应 addon 钩子，转发 View 的信号
        view.sig_view_remove.connect(self._on_view_remove)
        view.sig_view_refresh.connect(self._on_view_refresh)

    # ------------------------------------------------------------------
    # mitmproxy 事件钩子（鸭子类型，方法名即事件名）
    # ------------------------------------------------------------------
    def request(self, flow: HTTPFlow) -> None:
        """请求已发出（尚无响应），先让表格出现一行。"""
        self.bridge.flow_added.emit(flow)

    def response(self, flow: HTTPFlow) -> None:
        """响应体已完整接收，更新该行（对应 complete 状态）。"""
        self.bridge.flow_updated.emit(flow)

    def error(self, flow: HTTPFlow) -> None:
        """发生错误（如连接失败），更新该行。"""
        self.bridge.flow_updated.emit(flow)

    # ------------------------------------------------------------------
    # View 信号转发（移除/刷新）
    # ------------------------------------------------------------------
    def _on_view_remove(self, flow: Flow, index: int) -> None:
        self.bridge.flow_removed.emit(flow, index)

    def _on_view_refresh(self) -> None:
        self.bridge.view_refreshed.emit()


# ─────────────────────────────────────────────────────────────
# 流量导出（本地化实现，替代原 mitmproxy.addons.export 依赖）
# 逻辑忠实移植自 mitmproxy/addons/export.py，去掉了 ctx.options 依赖
# （export_preserve_original_ip 选项需 Export addon 注册，ferret 未挂载）
# ─────────────────────────────────────────────────────────────


class FlowExporter:
    """流量导出器。

    方法签名与返回值对齐 mitmproxy.addons.export 的模块级函数
    (``curl_command`` / ``httpie_command`` / ``raw_request`` / ``raw_response``
    / ``raw``)，但去掉了对 mitmproxy 运行上下文(``ctx``)与 ``pyperclip`` 的依赖：

    * 不读取 ``ctx.options.export_preserve_original_ip``，因此不生成
      ``--resolve``（等价于该选项为 False，绝大多数场景无差异）；
    * 复制到剪贴板由调用方用 PySide 完成，不引入 ``pyperclip``。

    保留自实现的 Windows 单引号→双引号处理，使 cmd.exe 下更友好。
    """

    # ------------------------------------------------------------------
    # 公开导出方法（命名对齐 mitmproxy.addons.export）
    # ------------------------------------------------------------------
    @staticmethod
    def curl_command(flow: HTTPFlow) -> str:
        """导出为 curl 命令，自动适配当前操作系统。

        - Windows (win32): 单引号转为双引号，兼容 cmd.exe
        - macOS / Linux: 原生单引号格式，兼容 bash/zsh
        """
        request = FlowExporter._cleanup_request(flow)
        FlowExporter._pop_headers(request)

        args = ["curl"]
        for k, v in request.headers.items(multi=True):
            if k.lower() == "accept-encoding":
                args.append("--compressed")
            else:
                args += ["-H", f"{k}: {v}"]

        if request.method != "GET":
            if not request.content:
                # 无 body 时 curl 不会自动加 content-length，
                # 某些服务器/方法组合（如 nginx + POST）要求显式为 0
                args += ["-H", "content-length: 0"]
            args += ["-X", request.method]

        args.append(request.pretty_url)
        cmd = " ".join(shlex.quote(arg) for arg in args)
        if request.content:
            cmd += f" -d {FlowExporter._request_content_for_console(request)}"

        if sys.platform == "win32":
            # Windows: cmd.exe 不支持单引号，转换为双引号
            cmd = FlowExporter._to_windows_curl(cmd)
        return cmd

    @staticmethod
    def httpie_command(flow: HTTPFlow) -> str:
        """导出为 httpie 命令。"""
        request = FlowExporter._cleanup_request(flow)
        FlowExporter._pop_headers(request)

        args = ["http", request.method, request.pretty_url]
        for k, v in request.headers.items(multi=True):
            args.append(f"{k}: {v}")
        cmd = " ".join(shlex.quote(arg) for arg in args)
        if request.content:
            cmd += " <<< " + FlowExporter._request_content_for_console(request)
        return cmd

    @staticmethod
    def raw_request(flow: HTTPFlow) -> bytes:
        """导出原始请求字节。"""
        request = FlowExporter._cleanup_request(flow)
        if request.raw_content is None:
            raise ValueError("Request content missing.")
        return assemble_request(request)

    @staticmethod
    def raw_response(flow: HTTPFlow) -> bytes:
        """导出原始响应字节。"""
        response = FlowExporter._cleanup_response(flow)
        if response.raw_content is None:
            raise ValueError("Response content missing.")
        return assemble_response(response)

    @staticmethod
    def raw(flow: HTTPFlow, separator: bytes = b"\r\n\r\n") -> bytes:
        """导出原始请求和响应（仅一方存在时返回存在的一方）。"""
        request_present = (
            isinstance(flow, HTTPFlow)
            and flow.request
            and flow.request.raw_content is not None
        )
        response_present = (
            isinstance(flow, HTTPFlow)
            and flow.response
            and flow.response.raw_content is not None
        )

        if request_present and response_present:
            parts = [
                FlowExporter.raw_request(flow),
                FlowExporter.raw_response(flow),
            ]
            if flow.websocket:
                parts.append(flow.websocket._get_formatted_messages())
            return separator.join(parts)
        elif request_present:
            return FlowExporter.raw_request(flow)
        elif response_present:
            return FlowExporter.raw_response(flow)
        else:
            raise ValueError("Can't export flow with no request or response.")

    # ------------------------------------------------------------------
    # 私有辅助（命名对齐 mitmproxy.addons.export 的内部函数）
    # ------------------------------------------------------------------
    @staticmethod
    def _cleanup_request(flow: HTTPFlow) -> Request:
        """复制并解码请求，无请求时抛错。"""
        if not flow.request:
            raise ValueError("Can't export flow with no request.")
        request = flow.request.copy()
        request.decode(strict=False)
        return request

    @staticmethod
    def _cleanup_response(flow: HTTPFlow) -> Response:
        """复制并解码响应，无响应时抛错。"""
        if not flow.response:
            raise ValueError("Can't export flow with no response.")
        response = flow.response.copy()
        response.decode(strict=False)
        return response

    @staticmethod
    def _pop_headers(request: Request) -> None:
        """剔除 curl/httpie 导出时冗余的 header。"""
        request.headers.pop("content-length", None)
        if request.headers.get("host", "") == request.host:
            request.headers.pop("host")
        if request.headers.get(":authority", "") == request.host:
            request.headers.pop(":authority")

    @staticmethod
    def _request_content_for_console(request: Request) -> str:
        """把请求体转成 shell 安全字符串（控制字符走 printf 转义）。"""
        try:
            text = request.get_text(strict=True)
        except ValueError:
            raise ValueError("Request content must be valid unicode") from None
        if not text:
            raise ValueError("Request content must be valid unicode")
        escape_control_chars = {chr(i): f"\\x{i:02x}" for i in range(32)}
        escaped_text = "".join(escape_control_chars.get(x, x) for x in text)
        if any(char in escape_control_chars for char in text):
            # 转义序列需要 shell 的 printf 还原，curl/httpie 才能正确发送
            return f'"$(printf {shlex.quote(escaped_text)})"'
        return shlex.quote(escaped_text)

    @staticmethod
    def _to_windows_curl(cmd: str) -> str:
        """将 Unix curl 命令转为 Windows cmd 兼容格式（单引号→双引号）。"""

        # 提取单引号内容，转义内部双引号，再用双引号包裹
        def replace_quotes(match):
            content = match.group(1)
            escaped = content.replace('"', r"\"")
            return f'"{escaped}"'

        return re.sub(r"'([^']*)'", replace_quotes, cmd)


class Cert:
    def check(self) -> bool:
        """检测证书是否安装

        :return: 已安装返回 True，否则 False
        """
        try:
            out = subprocess.run(
                ["certutil", "-store", "-user", "Root"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
        return "mitmproxy" in out.stdout.lower()

    def install(self):
        """安装证书

        若 ~/.mitmproxy/mitmproxy-ca-cert.pem 不存在，则先用 mitmproxy
        自带API（CertStore.from_store，与 mitmproxy 启动逻辑一致）生成证书，
        再用 certutil 安装到用户受信任根 CA。
        """

        cert_dir = os.path.expanduser(CONF_DIR)
        cert_path = os.path.join(cert_dir, "mitmproxy-ca-cert.pem")
        if not os.path.exists(cert_path):
            # 证书不存在 -> 用 mitmproxy 自带 API 生成
            # from_store 在证书缺失时会自动创建并写入所有证书文件
            certs.CertStore.from_store(cert_dir, CONF_BASENAME, KEY_SIZE)

        # 安装到当前用户受信任根 CA（需管理员权限）
        subprocess.run(
            ["certutil", "-addstore", "-user", "Root", cert_path],
            capture_output=True,
            text=True,
            check=True,
        )


# ─────────────────────────────────────────────────────────────
# FlowView：mitmproxy HTTPFlow 的展示视图
# ─────────────────────────────────────────────────────────────


def infer_flow_state(flow: HTTPFlow) -> str:
    """根据 HTTPFlow 当前状态推断展示状态。"""
    if flow.error:
        return "error"
    if flow.response:
        return "complete"
    return "request"


class FlowView:
    """把 mitmproxy HTTPFlow 转换为 UI 可用的字典视图。

    内部持有原始 HTTPFlow，提供表格展示所需字段，并可通过 to_dict()
    生成与原先 _preprocess_flow 兼容的完整字典（供详情面板使用）。

    解析结果按 (flow, state) 维度缓存，且以弱引用持有 flow：flow 被 View
    移除并 GC 后缓存自动释放，不会泄漏；同一 flow 在 request / complete 等
    不同阶段会得到各自独立的解析，避免展示过期数据。这样 build_body 等重活
    只在真正需要且状态变化时执行一次，而非每次打开详情/筛选都重跑。
    """

    # flow -> {state: 解析后的 dict}，弱引用 key，flow 回收即整体释放
    _PARSE_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

    def __init__(self, flow: HTTPFlow, state: str | None = None):
        if not isinstance(flow, HTTPFlow):
            raise TypeError(
                f"FlowView only supports HTTPFlow, got {type(flow).__name__}"
            )
        self.flow = flow
        self.state = state or infer_flow_state(flow)

    # ------------------------------------------------------------------
    # 基础字段（表格展示用）
    # ------------------------------------------------------------------
    @property
    def id(self) -> str:
        return self.flow.id

    @property
    def method(self) -> str:
        return self.flow.request.method

    @property
    def url(self) -> str:
        return self.flow.request.pretty_url

    @property
    def host(self) -> str:
        return self.flow.request.host

    @property
    def scheme(self) -> str:
        return self.flow.request.scheme

    @property
    def path(self) -> str:
        return self.flow.request.path

    @property
    def status_code(self) -> str:
        if self.state == "error":
            return "Error"
        if self.flow.response is None:
            return "等待中..."
        return str(self.flow.response.status_code)

    @property
    def duration(self) -> str:
        if self.flow.response is None or self.flow.request.timestamp_start is None:
            return ""
        duration = (
            self.flow.response.timestamp_end or 0
        ) - self.flow.request.timestamp_start
        return f"{duration * 1000:.0f} ms"

    # ------------------------------------------------------------------
    # 字典视图（详情面板用）
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """生成与原先 _preprocess_flow 兼容的字典（带一次性解析缓存）。"""
        bucket = self._PARSE_CACHE.get(self.flow)
        if bucket is None:
            bucket = {}
            self._PARSE_CACHE[self.flow] = bucket
        cached = bucket.get(self.state)
        if cached is None:
            cached = self._build_dict()
            bucket[self.state] = cached
        return cached

    def _build_dict(self) -> dict[str, Any]:
        flow = self.flow
        state = self.state

        data: dict[str, Any] = {"id": flow.id, "state": state}

        if state in (
            "request_headers",
            "request",
            "response_headers",
            "complete",
            "error",
        ):
            keep_alive = flow.request.headers.get("keep-alive", None)
            if keep_alive is None and flow.request.http_version == "HTTP/1.1":
                keep_alive = "true"
            elif keep_alive is None:
                keep_alive = "false"

            client_addr = flow.client_conn.peername if flow.client_conn else None
            proc_info = resolve_process(client_addr) if client_addr else None
            app = proc_info.to_dict() if proc_info else {}

            client_pn = flow.client_conn.peername if flow.client_conn else None
            client_sn = (
                getattr(flow.client_conn, "sockname", None)
                if flow.client_conn
                else None
            )
            conn_time = ""
            if flow.request.timestamp_start:
                conn_time = (
                    datetime.fromtimestamp(flow.request.timestamp_start, tz=UTC)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S.%f")
                )

            data.update(
                {
                    "Method": flow.request.method,
                    "URL": flow.request.pretty_url,
                    "Host": flow.request.host,
                    "Path": flow.request.path,
                    "Scheme": flow.request.scheme,
                    "HTTP Version": flow.request.http_version,
                    "Request Headers": dict(flow.request.headers),
                    "req_time": flow.request.timestamp_start,
                    "req_timestamp_end": flow.request.timestamp_end,
                    "req_headers_size": len(str(flow.request.headers)),
                    "Status Code": "等待中...",
                    "Keep Alive": keep_alive,
                    **app,
                    "Connection ID": flow.id,
                    "Connection Time": conn_time,
                    "Front Client Address": client_pn[0] if client_pn else "N/A",
                    "Front Client Port": client_pn[1] if client_pn else "N/A",
                    "Front Server Address": client_sn[0] if client_sn else "N/A",
                    "Front Server Port": client_sn[1] if client_sn else "N/A",
                }
            )

            data["Request Params"] = parse_params(flow.request.url)
            data["Request Cookies"] = parse_cookies_from_headers(
                dict(flow.request.headers), "Cookie"
            )

        if state in ("request", "response_headers", "complete", "error"):
            body = _safe_content(flow.request)
            req_duration = None
            if flow.request.timestamp_end and flow.request.timestamp_start:
                req_duration = (
                    flow.request.timestamp_end - flow.request.timestamp_start
                ) * 1000
            req_ct = flow.request.headers.get("Content-Type", "-")
            req_body_info = build_body(body, req_ct)
            data.update(
                {
                    "req_size": len(body),
                    "req_duration": req_duration,
                    "Request Body": body,
                    "Request Content-Type": req_ct,
                    "Request Body Text": req_body_info["text"],
                    "Request Body Pretty": req_body_info["pretty"],
                    "Request Fold Regions": req_body_info["fold_regions"],
                    "Request Is Binary": req_body_info["is_binary"],
                    "Request Body MIME": req_body_info["mime"],
                }
            )

        if state in ("response_headers", "complete", "error") and flow.response:
            data["Response Cookies"] = parse_cookies_from_headers(
                dict(flow.response.headers), "Set-Cookie"
            )

            server_addr = "N/A"
            if flow.server_conn and flow.server_conn.peername:
                server_addr = (
                    f"{flow.server_conn.peername[0]}:{flow.server_conn.peername[1]}"
                )

            protocol = flow.request.http_version
            if flow.server_conn and flow.server_conn.alpn:
                protocol = flow.server_conn.alpn.decode()

            proxy_protocol = "http"
            if (
                flow.server_conn
                and hasattr(flow.server_conn, "tls_established")
                and flow.server_conn.tls_established
            ):
                proxy_protocol = "https"

            server_pn = flow.server_conn.peername if flow.server_conn else None
            server_sn = (
                getattr(flow.server_conn, "source_address", None)
                if flow.server_conn
                else None
            )

            data.update(
                {
                    "Status Code": flow.response.status_code,
                    "Reason": flow.response.reason,
                    "Response Headers": dict(flow.response.headers),
                    "Response HTTP Version": flow.response.http_version,
                    "Server Address": server_addr,
                    "Protocol": protocol,
                    "res_headers_size": len(str(flow.response.headers)),
                    "res_timestamp_start": flow.response.timestamp_start,
                    "Proxy Protocol": proxy_protocol,
                    "Back Client Address": server_sn[0] if server_sn else "N/A",
                    "Back Client Port": server_sn[1] if server_sn else "N/A",
                    "Back Server Address": server_pn[0] if server_pn else "N/A",
                    "Back Server Port": server_pn[1] if server_pn else "N/A",
                }
            )

            conn = flow.server_conn
            if conn and getattr(conn, "tls_established", False):
                tls_info = {
                    "TLS Version": getattr(conn, "tls_version", "N/A"),
                    "TLS SNI": getattr(conn, "sni", "N/A"),
                    "TLS ALPN Offers": [
                        a.decode() if isinstance(a, bytes) else str(a)
                        for a in getattr(conn, "alpn_offers", []) or []
                    ],
                    "TLS ALPN Selected": (conn.alpn.decode() if conn.alpn else "N/A"),
                    "TLS Cipher": getattr(conn, "cipher", "N/A"),
                    "TLS Cipher List": list(getattr(conn, "cipher_list", []) or []),
                }
                if hasattr(conn, "certificate_list") and conn.certificate_list:
                    server_cert = conn.certificate_list[0]
                    if server_cert:
                        tls_info["Not Before"] = server_cert.notbefore.strftime(
                            "%Y-%m-%d %H:%M:%S.000"
                        )
                        tls_info["Not After"] = server_cert.notafter.strftime(
                            "%Y-%m-%d %H:%M:%S.000"
                        )
                data.update(tls_info)

        if state in ("complete", "error") and flow.response:
            duration = (flow.response.timestamp_end or 0) - (
                flow.request.timestamp_start or 0
            )
            res_duration = None
            if flow.response.timestamp_end and flow.response.timestamp_start:
                res_duration = (
                    flow.response.timestamp_end - flow.response.timestamp_start
                ) * 1000
            body = _safe_content(flow.response)
            req_total_size = data.get("req_headers_size", 0) + data.get("req_size", 0)
            res_total_size = data.get("res_headers_size", 0) + len(body)
            total_size = req_total_size + res_total_size
            res_ct = flow.response.headers.get("Content-Type", "-")
            res_body_info = build_body(body, res_ct)
            data.update(
                {
                    "Response Body": body,
                    "Response Content-Type": res_ct,
                    "Response Body Text": res_body_info["text"],
                    "Response Body Pretty": res_body_info["pretty"],
                    "Response Fold Regions": res_body_info["fold_regions"],
                    "Response Is Binary": res_body_info["is_binary"],
                    "Response Body MIME": res_body_info["mime"],
                    "res_size": len(body),
                    "res_time": flow.response.timestamp_end,
                    "res_duration": res_duration,
                    "Duration": f"{duration * 1000:.0f} ms",
                    "total_size": total_size,
                    "TLS Version": getattr(flow.server_conn, "tls_version", "N/A")
                    if flow.server_conn
                    else "N/A",
                }
            )

        if state == "error":
            data.update(
                {
                    "Status Code": "Error",
                    "Error Message": flow.error.msg if flow.error else "Unknown",
                }
            )

        if state == "complete":
            try:
                data["curl_command"] = FlowExporter.curl_command(flow)
            except Exception as e:  # noqa: BLE001
                print(f"生成 cURL 命令失败: {e}")
                data["curl_command"] = f"Error generating curl command: {e}"

        return data


# ─────────────────────────────────────────────────────────────
# 自测：直接 `python services.py` 启动一个轻量代理并把流量全打印
# 用法：python services.py [port]   然后浏览器/系统代理指向 127.0.0.1:port
# 停止：Ctrl+C
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    class PrintAddon:
        """只打印响应阶段，用于验证 CaptureMaster。"""

        def response(self, flow: HTTPFlow):
            if flow.response is None:
                print(f"[response] (无响应) {flow.request.pretty_url}")
                return
            print(f"[response] {flow.response.status_code} {flow.request.pretty_url}")
            print("  content-type:", flow.response.headers.get("content-type", ""))
            if flow.response.content:
                preview = flow.response.content[:500]
                print("  body:", preview.decode("utf-8", errors="replace"))

    async def _run():
        opts = Options(listen_host="127.0.0.1", listen_port=PORT)
        master = CaptureMaster(opts)
        master.addons.add(PrintAddon())
        print(f"CaptureMaster 已启动，监听 127.0.0.1:{PORT}")
        print(f"把系统/浏览器代理设为 127.0.0.1:{PORT}，Ctrl+C 停止\n")
        try:
            await master.run()
        except asyncio.CancelledError:
            pass
        finally:
            print("CaptureMaster 已停止")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出")
