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

# cmd 在**引号外**当语法解释、必须补 ^ 转义的元字符。``!`` 不在内：只在开启延迟
# 展开的 cmd 里才活，交互式默认关；``%`` 转义不了（见 _to_windows_curl 的边界）。
_CMD_METACHARACTERS = frozenset("&|<>^()")


def _call[Arg, Result](exporter: Callable[[Arg], Result], target: Arg) -> Result:
    """调用上游导出函数，不让 mitmproxy 的异常类型穿出 core 边界。"""
    try:
        return exporter(target)
    except CommandError as exc:
        raise ValueError(str(exc)) from exc


def _to_windows_curl(command: str) -> str:
    """把 shlex 产出的 POSIX 单引号改写成 **cmd.exe** 可用的双引号。

    目标 shell 明确为 cmd.exe（issues #82）：这是 Windows 的公共下限，且
    PowerShell 5.1 里裸 ``curl`` 是 ``Invoke-WebRequest`` 的别名，粘贴即错 ——
    面向 PowerShell 产出反而要求用户先改命令。加引号按 MSVCRT argv 规则
    （curl.exe 的命令行即按它解析）。

    cmd 边界（实测，属目标 shell 的表达能力，不是本函数的缺陷）：

    - ``%VAR%``：cmd 的百分号展开发生在转义处理之前，交互式 cmd **没有任何
      转义手段**，头值里长得像环境变量引用的 ``%...%`` 回放时会被展开；
    - 控制字符：cmd 无法在参数里携带，沿用上游 ``"$(printf '…')"`` 的 POSIX
      形态 —— 回放时 body 不对，但单行、无注入，胜于把字面换行粘进 cmd
      （会拆行执行半截命令）；
    - ``& | < > ( ) ^`` 在引号内是字面字符，**但引号状态在每个 ``"`` 上翻转**
      （含 ``\\"`` 里的那个）：内嵌引号会把它们翻到引号外，``_windows_token``
      对引号外的元字符补 ``^`` 转义兜底（实测缺这步可构成命令注入）。

    必须先 ``shlex.split`` 按 POSIX 语义还原参数表、再逐参数重加引号：
    shlex.quote 对内嵌单引号用 ``'"'"'`` 续接，正则直改吃不下这种转义，
    ``O'Reilly`` 之类整段被切碎（docs/design.md#export #10）。加引号按 MSVCRT
    argv 规则（curl.exe 的命令行即按它解析）：内嵌 ``"`` 转义成 ``\\"``、
    引号前反斜杠翻倍；单引号在 Windows 命令行里是字面字符，落进双引号即
    安全。

    cmd 的引号状态是**整行一个计数器**（在每一个 ``"`` 上翻转，含 ``\\"`` 里
    的那个；CRT 则把 ``\\"`` 当转义不翻 —— 两套规则正好在这个差别上错位），
    内嵌引号的 token 会把后面所有参数翻到引号外。所以 ``^`` 转义必须等整行
    拼完后全局扫一遍（`_escape_cmd_metacharacters`），逐 token 扫描兜不住
    「前一个 token 欠的翻转账」。
    """
    emitted = " ".join(_windows_token(token) for token in shlex.split(command))
    return _escape_cmd_metacharacters(emitted)


def _windows_token(token: str) -> str:
    """单个参数加 Windows 引号。

    安全字符裸写；其余必须包双引号。``list2cmdline`` 只对空白/引号加引号，
    ``&`` ``?`` 这类 cmd 元字符会被裸放（裸 ``&`` 在 cmd 里是命令分隔符），
    所以它说「不用引号」而非安全字符时得自己补——此时 token 必无内嵌引号
    （有引号 list2cmdline 已包），唯一要补的是尾部反斜杠翻倍：撞上新增的
    闭合引号若不翻倍，会被 CRT 读成字面引号、参数粘连。

    内嵌引号另有一步 ``^`` 转义（`_escape_cmd_metacharacters`）：cmd 的引号
    状态在**每一个** ``"``（含 ``\\"`` 里的那个）上翻转，内嵌引号会把 token 里的
    ``&`` ``|`` 等翻到引号外 —— 实测该处会被 cmd 当命令语法**执行**（导出的
    命令来自抓到的流量，这是一条真注入路径）。转义在 `_to_windows_curl` 拼
    完整行之后全局做一遍，这里不重复。
    """
    quoted = subprocess.list2cmdline([token])
    if quoted != token or _SAFE_TOKEN.fullmatch(token):
        return quoted
    return '"' + re.sub(r"(\\+)$", r"\1\1", token) + '"'


def _escape_cmd_metacharacters(emitted: str) -> str:
    """给 cmd 引号状态外的元字符补 ``^``。

    沿改写后的命令行逐字符跟踪引号状态（每个 ``"`` 翻转一次），元字符落在
    引号外就前置 ``^``。cmd 的 ``^`` 只在引号外是转义符、在 `CreateProcess`
    前被剥掉 —— 子进程 CRT 的 argv 不受影响；落在引号内的元字符本就是字面
    字符，补了 ``^`` 反而会被 CRT 收进参数。
    """
    if not _CMD_METACHARACTERS.intersection(emitted):
        return emitted
    parts: list[str] = []
    in_quotes = False
    for char in emitted:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char in _CMD_METACHARACTERS:
            parts.append("^")
        parts.append(char)
    return "".join(parts)


class FlowExporter:
    """Export flows without depending on mitmproxy's runtime context."""

    @staticmethod
    def curl_command(flow: HTTPFlow) -> str:
        """``mitmproxy.addons.export.curl_command`` 的本地分叉。

        不能委托上游：上游会读 ``ctx.options.export_preserve_original_ip``，
        该选项由 Export addon 注册而 FerretMaster 不加载它；且上游只产出 POSIX
        引号，Windows 的 cmd.exe 会把单引号当字面字符。Windows 产出的目标
        shell 与边界见 ``_to_windows_curl``。
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
