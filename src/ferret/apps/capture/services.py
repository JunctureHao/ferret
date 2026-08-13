import asyncio
import re
import shlex
import subprocess
import sys
import zlib
from collections.abc import Iterable
from types import ModuleType
from typing import Any

from ferret.core.settings import APP_NAME

_STUBBED_ADDONS = {
    # onboarding.py 顶层 import asgiapp(依赖 asgiref)，整枝屏蔽
    "mitmproxy.addons.onboarding": (),
    # onboarding.py 里有 `from mitmproxy.addons.onboardingapp import app`，桩需提供 app 属性
    "mitmproxy.addons.onboardingapp": ("app",),
    "mitmproxy.addons.proxyauth": (),
    # maplocal.py 顶层 `from werkzeug.security import safe_jMitmproxy 异步循环已结束oin`，werkzeug 被排除需整枝屏蔽
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
from mitmproxy import certs, connection, io
from mitmproxy.addons.clientplayback import ClientPlayback, ReplayHandler  # noqa: F401
from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver

# from mitmproxy.addons.save import Save
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.addons.view import View
from mitmproxy.flow import Flow
from mitmproxy.flowfilter import parse as parse_filter
from mitmproxy.http import HTTPFlow, Request, Response
from mitmproxy.master import Master
from mitmproxy.net.http import status_codes
from mitmproxy.net.http.http1.assemble import assemble_request, assemble_response
from mitmproxy.options import KEY_SIZE, Options
from mitmproxy.proxy import server_hooks
from mitmproxy.utils import human

# ─────────────────────────────────────────────────────────────
# GUI 搜索条件 → mitmproxy flowfilter 表达式
# 把多行 FilterRow 的 {field, logic, value} 翻译成 flowfilter DSL，
# 并与基底 ~http 用 & 组合，整条交给 View.set_filter 做「显示过滤」
# （View._store 保留全部流量，set_filter 只控制 _view 可见列表，无清除效果）。
# ─────────────────────────────────────────────────────────────

# GUI 字段 → flowfilter 运算符
_FIELD_TO_OP: dict[str, str] = {
    "全部": "u",  # 裸 ~u 等价于对 URL 正则；多行 AND 时也能覆盖大部分场景
    "URL": "u",
    "Method": "m",
    "Header": "h",
    "Body": "b",
}


def _escape_regex(text: str) -> str:
    """转义正则特殊字符，使普通包含/等于匹配按字面量处理。

    仅用于「包含 / 等于 / 不包含」模式；「正则表达式」模式不做转义。
    """

    return re.escape(text)


def _quote_value(value: str) -> str:
    """按 flowfilter 语法给值加引号（含空格/引号时必需）。"""
    if not value or (" " in value) or ('"' in value) or ("'" in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _condition_to_expr(cond: dict) -> str | None:
    """把单个 GUI 条件翻译成 flowfilter 原子表达式，无有效值返回 None。"""
    field = cond.get("field", "全部")
    logic = cond.get("logic", "包含")
    value = (cond.get("value") or "").strip()
    if not value:
        return None

    op = _FIELD_TO_OP.get(field, "u")

    if logic == "正则表达式":
        rex = value
    elif logic == "等于":
        # 整串精确匹配：用 ^...$ 锚定
        rex = f"^{_escape_regex(value)}$"
    else:  # 包含 / 不包含
        rex = _escape_regex(value)

    atom = f"~{op} {_quote_value(rex)}"
    if logic == "不包含":
        return f"!{atom}"
    return atom


def build_filter_expression(conditions: list[dict] | None) -> str:
    """把 GUI 多条件合并为 flowfilter 表达式。

    - conditions 为空 / None → 仅返回基底 ``~http``（显示全部 HTTP 流量）。
    - 多条件之间以 ``&`` 连接（AND 语义，与原 GUI 多行行为一致）。
    - 每个条件与基底 ``~http`` 同样以 ``&`` 组合，保证只显示 HTTP 流。

    返回的字符串可直接交给 ``mitmproxy.flowfilter.parse``。
    """
    atoms: list[str] = ["~http"]
    if conditions:
        for cond in conditions:
            expr = _condition_to_expr(cond)
            if expr:
                atoms.append(expr)
    return " & ".join(atoms)


def compile_filter(conditions: list[dict] | None):
    """把 GUI 条件编译为 mitmproxy flowfilter 对象（TFilter）。

    集中在此处调用 ``parse_filter``，保持「单一 mitmproxy 导入出口」约定，
    并让 ``parse_filter`` / ``match_filter`` 的导入在模块内被真实使用。
    表达式非法（如正则语法错误）时抛出 ``ValueError``，由调用方决定回退策略。

    :returns: 可直接 ``__call__(flow)`` 的过滤器；空条件返回 ``~http`` 过滤器。
    """
    return parse_filter(build_filter_expression(conditions))


def _safe_content(message) -> bytes:
    """安全获取解压后的 content，解码失败时回退到 raw_content。"""
    try:
        return message.content or b""
    except (ValueError, zlib.error):
        return message.raw_content or b""


class FerretTlsConfig(TlsConfig):
    """重写证书 basename，使证书文件以 ferret- 为前缀。"""

    def configure(self, updated):
        import mitmproxy.addons.tlsconfig as mod

        original = mod.CONF_BASENAME
        mod.CONF_BASENAME = APP_NAME  # type: ignore
        try:
            super().configure(updated)
        finally:
            mod.CONF_BASENAME = original


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

        self.addons.add(
            Core(),
            Proxyserver(),
            FerretTlsConfig(),
            NextLayer(),
            DnsResolver(),
            self.view,
            ClientPlayback(),
            # Save(),
            LogAddon(),
        )


class UiBridgeAddon:
    def __init__(self, view: View, bridge: Any) -> None:
        self.view = view
        self.bridge = bridge
        view.sig_view_add.connect(self._on_view_add)
        view.sig_view_update.connect(self._on_view_update)
        view.sig_view_remove.connect(self._on_view_remove)
        view.sig_view_refresh.connect(self._on_view_refresh)

    def _on_view_add(self, flow: Flow) -> None:
        self.bridge.flow_added.emit(flow)

    def _on_view_update(self, flow: Flow) -> None:
        self.bridge.flow_updated.emit(flow)

    def _on_view_remove(self, flow: Flow, index: int) -> None:
        self.bridge.flow_removed.emit(flow, index)

    def _on_view_refresh(self) -> None:
        self.bridge.view_refreshed.emit()


class LogAddon:
    """完整链路日志处理器（模拟原生日志）"""

    def __init__(self) -> None:
        from ferret.core.log import get_logger

        self._log = get_logger("mitmproxy")

    def client_connected(self, client: connection.Client) -> None:
        address = f"{client.peername[0]}:{client.peername[1]}"
        self._log.info(f"[{address}] client connect")

    def server_connected(self, data: server_hooks.ServerConnectionHookData):
        client = data.client
        server = data.server
        client_address = f"{client.peername[0]}:{client.peername[1]}"
        server_address = (
            f"{server.address[0]}:{server.address[1]}" if server.address else "unknown"
        )
        ip_port = (
            f"{server.peername[0]}:{server.peername[1]}"
            if server.peername
            else "unknown"
        )
        self._log.info(
            f"[{client_address}] server connect {server_address} ({ip_port})"
        )

    def request(self, flow: HTTPFlow) -> None:
        request = flow.request
        if request is None:
            return

        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        method = request.method
        pretty_url = request.pretty_url
        http_version = request.http_version
        self._log.info(
            f"{client_address} {method} {pretty_url} {http_version}",
            extra={"raw": True},
        )

    def response(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None:
            return

        status = response.status_code
        version = response.http_version
        resonse = response.reason or status_codes.RESPONSES.get(status, "")
        friendly_size = human.pretty_size(
            len(response.content) if response.content else 0
        )
        self._log.info(
            f"      << {version} {status} {resonse} {friendly_size}",
            extra={"raw": True},
        )

    def error(self, flow: HTTPFlow) -> None:
        """请求有始无终（连接中断/超时等）时的兜底输出"""
        if flow.error is None:
            return
        self._log.info(
            f"      << {flow.error.msg}",
            extra={"raw": True},
        )

    def http_connect_error(self, flow: HTTPFlow) -> None:
        """CONNECT 建连失败（上游不可达/被拒绝等）"""
        request = flow.request
        if request is None:
            return

        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        method = request.method
        pretty_url = request.pretty_url
        http_version = request.http_version
        self._log.info(
            f"{client_address} {method} {pretty_url} {http_version}",
            extra={"raw": True},
        )
        msg = flow.error.msg if flow.error else "connection failed"
        self._log.info(
            f"      << {msg}",
            extra={"raw": True},
        )


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
        return APP_NAME in out.stdout

    def install(self):
        """安装证书

        若证书不存在，则先用 mitmproxy 自带 API（CertStore.from_store）
        生成证书到 Ferret 配置目录，再用 certutil 安装到用户受信任根 CA。
        """
        from ferret.core.settings import get_certs_dir

        cert_dir = get_certs_dir()
        cert_path = cert_dir / f"{APP_NAME}-ca-cert.pem"
        if not cert_path.exists():
            certs.CertStore.from_store(str(cert_dir), APP_NAME, KEY_SIZE)

        subprocess.run(
            ["certutil", "-addstore", "-user", "Root", str(cert_path)],
            capture_output=True,
            text=True,
            check=True,
        )


class SessionStore:
    @staticmethod
    def save_flows(flows: Iterable[Flow], path: str):
        with open(path, "wb") as f:
            writer = io.FlowWriter(f)
            for flow in flows:
                writer.add(flow)

    @staticmethod
    def load_flows(path: str) -> list[Flow]:
        with open(path, "rb") as f:
            reader = io.FlowReader(f)
            return list(reader.stream())
