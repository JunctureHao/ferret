"""Export mitmproxy HTTP flows to commands and raw messages.

除 ``curl_command`` 外全部委托给 ``mitmproxy.addons.export`` 的模块级函数，
只在边界上把 ``CommandError`` 归一化成 ``ValueError``。
"""

import json
import re
import shlex
import sys
from collections.abc import Callable, Sequence
from functools import partial

from ferret.core.mitm.bindings import CommandError, HTTPFlow, SaveHar, export_module


def _call[Arg, Result](exporter: Callable[[Arg], Result], target: Arg) -> Result:
    """调用上游导出函数，不让 mitmproxy 的异常类型穿出 core 边界。"""
    try:
        return exporter(target)
    except CommandError as exc:
        raise ValueError(str(exc)) from exc


def _to_windows_curl(command: str) -> str:
    """把 shlex 产出的 POSIX 单引号改写成 cmd.exe / PowerShell 可用的双引号。"""

    def replace_quotes(match: re.Match[str]) -> str:
        escaped = match.group(1).replace('"', r"\"")
        return f'"{escaped}"'

    return re.sub(r"'([^']*)'", replace_quotes, command)


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
    def save_har(flows: Sequence[HTTPFlow], path: str) -> None:
        """把流量导出为标准 HAR 文件。

        复用 ``mitmproxy.addons.savehar.SaveHar.make_har``，该函数是纯函数、
        不依赖 ``ctx``，可在 GUI 线程直接调用。单条与多条流量均可，均写入
        同一个 ``.har`` 文件（``entries`` 数组长度不同）。
        """

        har = json.dumps(SaveHar().make_har(flows), indent=4).encode()
        with open(path, "wb") as file:
            file.write(har)
