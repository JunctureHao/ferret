import asyncio
import os
import re
import shlex
import subprocess
import sys
from types import ModuleType

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


from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.flow import Flow
from mitmproxy.http import HTTPFlow
from mitmproxy.master import Master
from mitmproxy.net.http.http1.assemble import assemble_request, assemble_response
from mitmproxy.options import Options


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
    ) -> None:
        # with_termlog=False → 不要终端日志
        super().__init__(opts, event_loop=event_loop, with_termlog=False)

        # 只挂载代理服务必需的最小 addon（5 个底座）：
        # core（事件派发）/ proxyserver（起端口转发）/ tlsconfig（HTTPS 解密）/
        # next_layer（协议分层）/ dns_resolver（DNS 解析）。
        # 其余能力型 addon（改包/映射/拦截/保存/回放等）按需再单独添加。
        self.addons.add(
            Core(),
            Proxyserver(),
            TlsConfig(),
            NextLayer(),
            DnsResolver(),
        )


# ─────────────────────────────────────────────────────────────
# 流量导出（本地化实现，替代原 mitmproxy.addons.export 依赖）
# 逻辑忠实移植自 mitmproxy/addons/export.py，去掉了 ctx.options 依赖
# （export_preserve_original_ip 选项需 Export addon 注册，ferret 未挂载）
# ─────────────────────────────────────────────────────────────


def _cleanup_request(f: Flow):
    """复制并解码请求，无请求时抛错。"""
    if not isinstance(f, HTTPFlow) or not f.request:
        raise ValueError("Can't export flow with no request.")
    request = f.request.copy()
    request.decode(strict=False)
    return request


def _cleanup_response(f: Flow):
    """复制并解码响应，无响应时抛错。"""
    if not isinstance(f, HTTPFlow) or not f.response:
        raise ValueError("Can't export flow with no response.")
    response = f.response.copy()
    response.decode(strict=False)
    return response


def _pop_headers(request) -> None:
    """剔除 curl/httpie 导出时冗余的 header。"""
    request.headers.pop("content-length", None)
    if request.headers.get("host", "") == request.host:
        request.headers.pop("host")
    if request.headers.get(":authority", "") == request.host:
        request.headers.pop(":authority")


def _request_content_for_console(request) -> str:
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


class FlowExporter:
    """流量导出器 - 本地化实现（原 utils/exporter.py 迁入）"""

    @staticmethod
    def to_curl(flow_obj: Flow) -> str:
        """导出为 curl 命令，自动适配当前操作系统

        平台适配规则：
        - Windows (win32): 将单引号转换为双引号，兼容 cmd.exe
        - macOS (darwin): 使用原生格式（单引号），兼容 bash/zsh
        - Linux (linux): 使用原生格式（单引号），兼容 bash/zsh
        """
        request = _cleanup_request(flow_obj)
        _pop_headers(request)

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
            cmd += f" -d {_request_content_for_console(request)}"

        if sys.platform == "win32":
            # Windows: cmd.exe 不支持单引号，转换为双引号
            cmd = FlowExporter._to_windows_curl(cmd)
        return cmd

    @staticmethod
    def _to_windows_curl(cmd: str) -> str:
        """将 Unix curl 命令转为 Windows cmd 兼容格式（单引号→双引号）"""

        # 提取单引号内容，转义内部双引号，再用双引号包裹
        def replace_quotes(match):
            content = match.group(1)
            escaped = content.replace('"', r"\"")
            return f'"{escaped}"'

        return re.sub(r"'([^']*)'", replace_quotes, cmd)

    @staticmethod
    def to_httpie(flow_obj: Flow) -> str:
        """导出为 httpie 命令"""
        request = _cleanup_request(flow_obj)
        _pop_headers(request)

        args = ["http", request.method, request.pretty_url]
        for k, v in request.headers.items(multi=True):
            args.append(f"{k}: {v}")
        cmd = " ".join(shlex.quote(arg) for arg in args)
        if request.content:
            cmd += " <<< " + _request_content_for_console(request)
        return cmd

    @staticmethod
    def to_raw_request(flow_obj: Flow) -> bytes:
        """导出原始请求"""
        request = _cleanup_request(flow_obj)
        if request.raw_content is None:
            raise ValueError("Request content missing.")
        return assemble_request(request)

    @staticmethod
    def to_raw_response(flow_obj: Flow) -> bytes:
        """导出原始响应"""
        response = _cleanup_response(flow_obj)
        if response.raw_content is None:
            raise ValueError("Response content missing.")
        return assemble_response(response)

    @staticmethod
    def to_raw(flow_obj: Flow, separator: bytes = b"\r\n\r\n") -> bytes:
        """导出原始请求和响应（仅一方存在时返回存在的一方）"""
        request_present = (
            isinstance(flow_obj, HTTPFlow)
            and flow_obj.request
            and flow_obj.request.raw_content is not None
        )
        response_present = (
            isinstance(flow_obj, HTTPFlow)
            and flow_obj.response
            and flow_obj.response.raw_content is not None
        )

        if request_present and response_present:
            parts = [
                FlowExporter.to_raw_request(flow_obj),
                FlowExporter.to_raw_response(flow_obj),
            ]
            if flow_obj.websocket:
                parts.append(flow_obj.websocket._get_formatted_messages())
            return separator.join(parts)
        elif request_present:
            return FlowExporter.to_raw_request(flow_obj)
        elif response_present:
            return FlowExporter.to_raw_response(flow_obj)
        else:
            raise ValueError("Can't export flow with no request or response.")


class Cert:
    def check(self) -> bool:
        """检测证书是否安装

        :return bool: 已安装返回 True，否则 False
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
        from mitmproxy.options import CONF_DIR

        cert_dir = os.path.expanduser(CONF_DIR)
        cert_path = os.path.join(cert_dir, "mitmproxy-ca-cert.pem")
        if not os.path.exists(cert_path):
            # 证书不存在 -> 用 mitmproxy 自带 API 生成
            from mitmproxy import certs
            from mitmproxy.options import CONF_BASENAME, KEY_SIZE

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
# 自测：直接 `python services.py` 启动一个轻量代理并把流量全打印
# 用法：python services.py [port]   然后浏览器/系统代理指向 127.0.0.1:port
# 停止：Ctrl+C
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    from mitmproxy.http import HTTPFlow

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
