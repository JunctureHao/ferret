"""Export mitmproxy HTTP flows to commands and raw messages.

除 ``curl_command`` 外全部委托给 ``mitmproxy.addons.export`` 的模块级函数，
只在边界上把 ``CommandError`` 归一化成 ``ValueError``。
"""

import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from functools import partial

from ferret.core.mitm.bindings import CommandError, HTTPFlow, SaveHar, export_module

# shlex.quote 视为「安全可不加引号」的字符集（CPython ``shlex._find_unsafe`` 同款
# 判定）。Windows 改写沿用同一判定决定裸写还是包引号，旧命令里原本正确的裸
# token 逐字节不变。
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def _call[Arg, Result](exporter: Callable[[Arg], Result], target: Arg) -> Result:
    """调用上游导出函数，不让 mitmproxy 的异常类型穿出 core 边界。"""
    try:
        return exporter(target)
    except CommandError as exc:
        raise ValueError(str(exc)) from exc


def _to_windows_curl(command: str) -> str:
    """把 shlex 产出的 POSIX 单引号改写成 cmd.exe / PowerShell 可用的双引号。

    必须先 ``shlex.split`` 按 POSIX 语义还原参数表、再逐参数重加引号：
    shlex.quote 对内嵌单引号用 ``'"'"'`` 续接，正则直改吃不下这种转义，
    ``O'Reilly`` 之类整段被切碎（.plans/issues.md #10）。加引号按 MSVCRT
    argv 规则（curl.exe 的命令行即按它解析）：内嵌 ``"`` 转义成 ``\\"``、
    引号前反斜杠翻倍；单引号在 Windows 命令行里是字面字符，落进双引号即
    安全。
    """
    return " ".join(_windows_token(token) for token in shlex.split(command))


def _windows_token(token: str) -> str:
    """单个参数加 Windows 引号。

    安全字符裸写；其余必须包双引号。``list2cmdline`` 只对空白/引号加引号，
    ``&`` ``?`` 这类 cmd 元字符会被裸放（裸 ``&`` 在 cmd 里是命令分隔符），
    所以它说「不用引号」而非安全字符时得自己补——此时 token 必无内嵌引号
    （有引号 list2cmdline 已包），唯一要补的是尾部反斜杠翻倍：撞上新增的
    闭合引号若不翻倍，会被 CRT 读成字面引号、参数粘连。
    """
    quoted = subprocess.list2cmdline([token])
    if quoted != token or _SAFE_TOKEN.fullmatch(token):
        return quoted
    return '"' + re.sub(r"(\\+)$", r"\1\1", token) + '"'


class FlowExporter:
    """Export flows without depending on mitmproxy's runtime context."""

    @staticmethod
    def curl_command(flow: HTTPFlow) -> str:
        """``mitmproxy.addons.export.curl_command`` 的本地分叉。

        不能委托上游：上游会读 ``ctx.options.export_preserve_original_ip``，
        该选项由 Export addon 注册而 FerretMaster 不加载它；且上游只产出 POSIX
        引号，Windows 的 cmd.exe / PowerShell 会把单引号当字面字符。
        """
        request = _call(export_module.cleanup_request, flow)
        export_module.pop_headers(request)
        args = ["curl"]
        for key, value in request.headers.items(multi=True):
            if key.lower() == "accept-encoding":
                args.append("--compressed")
            else:
                args += ["-H", f"{key}: {value}"]
        if request.method != "GET":
            if not request.content:
                args += ["-H", "content-length: 0"]
            args += ["-X", request.method]
        args.append(request.pretty_url)
        command = " ".join(shlex.quote(argument) for argument in args)
        if request.content:
            body = _call(export_module.request_content_for_console, request)
            command += f" -d {body}"
        if sys.platform == "win32":
            command = _to_windows_curl(command)
        return command

    @staticmethod
    def httpie_command(flow: HTTPFlow) -> str:
        return _call(export_module.httpie_command, flow)

    @staticmethod
    def raw_request(flow: HTTPFlow) -> bytes:
        return _call(export_module.raw_request, flow)

    @staticmethod
    def raw_response(flow: HTTPFlow) -> bytes:
        return _call(export_module.raw_response, flow)

    @staticmethod
    def raw(flow: HTTPFlow, separator: bytes = b"\r\n\r\n") -> bytes:
        return _call(partial(export_module.raw, separator=separator), flow)

    @staticmethod
    def request_body(flow: HTTPFlow) -> bytes:
        """请求体的**解压后**字节（``get_content(strict=False)``，永不抛）。

        与 ``raw_request`` 的线上字节是两个口径：gzip 的 body 这里拿到的是解压
        内容。挂起请求 / GET 无体 → ``b""``（比 raw 三件的 CommandError 宽容，
        界面统一走「空数据」警告路径）。
        """
        if flow.request is None:
            return b""
        return flow.request.get_content(strict=False) or b""

    @staticmethod
    def response_body(flow: HTTPFlow) -> bytes:
        """响应体的**解压后**字节；语义与 ``request_body`` 逐字相同。"""
        if flow.response is None:
            return b""
        return flow.response.get_content(strict=False) or b""

    @staticmethod
    def save_har(flows: Sequence[HTTPFlow], path: str) -> None:
        """把流量导出为标准 HAR 文件。

        复用 ``mitmproxy.addons.savehar.SaveHar.make_har``，该函数是纯函数、
        不依赖 ``ctx``，可在 GUI 线程直接调用。单条与多条流量均可，均写入
        同一个 ``.har`` 文件（``entries`` 数组长度不同）。
        """

        har = json.dumps(SaveHar().make_har(flows), indent=2).encode()
        with open(path, "wb") as file:
            file.write(har)
