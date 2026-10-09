"""cURL 命令粘贴导入：把剪贴板里的 curl 命令行解析成 :class:`RequestEdit`。

纯 stdlib（`shlex` + `base64` + `urllib`），无 Qt、无 mitmproxy 内部 import，
解析器可以脱离界面单测。mitmproxy 上游只有导出方向，导入只能自写；自家导出
（`FlowExporter.curl_command`）在 Windows 上是双引号风格，`shlex.split(posix)`
对双引号与 ``\\"`` 转义都吃得回（roundtrip 回归钉在
tests/apps/compose/test_curl_import.py）。

明确报错而不是静默丢字段：`-F` multipart、`@file` 引用、未知 flag 都是
`ValueError`（英文消息，由调用方进 `show_error` 展示）。
"""

import base64
import shlex
from urllib.parse import quote, urlsplit, urlunsplit

from ferret.core.mitm import RequestEdit

# 认识但忽略：这些 flag 只影响传输/输出行为，不改变请求语义。
_IGNORED_BARE = frozenset(
    {
        "-k",
        "--insecure",
        "-L",
        "--location",
        "-i",
        "--include",
        "-s",
        "--silent",
        "-S",
        "--show-error",
        "-v",
        "--verbose",
    }
)
# 同样忽略，但要吃掉紧跟的值。
_IGNORED_VALUE = frozenset(
    {
        "-o",
        "--output",
        "-w",
        "--write-out",
        "--max-time",
        "--connect-timeout",
    }
)


def _take(args: list[str], i: int, inline: str | None, flag: str) -> tuple[str, int]:
    """取 flag 的值：`--flag=value` 内联与 `--flag value` 分词两种形态都收。"""
    if inline is not None:
        return inline, i
    if i + 1 >= len(args):
        raise ValueError(f"Option {flag} requires a value")
    return args[i + 1], i + 1


# -G 拼 query 时的「安全字符」：RFC 3986 里 query 合法出现的字符原样保留 ——
# `&` `=` 是分隔符、`%` 是既有转义的前缀，都不能再编（真实 curl 的 CURLU_URLENCODE
# 就是这套语义：`-G -d 'a=1&b=2'` 在线上是两个参数，`%20` 不会被二次编码；
# issues #81）。quote 本就恒不碰字母数字与 `-._~`，这里补上其余合法字符。
_QUERY_SAFE = "!$&'()*+,;=:@/?%"


def _data_urlencode_part(part: str) -> str:
    """`--data-urlencode <data>` → `name=content`：只对 content 全量编码。

    curl 约定 `name=content` 编码 content、name 原样（name 默认已是合法 token）；
    `=content` 没有名字；裸值没有 `=`。`@file` 形态在 `-d` 分支已被拦。
    """
    name, sep, value = part.partition("=")
    if not sep:
        return quote(part, safe="")
    return f"{name}={quote(value, safe='')}"


def parse_curl(command: str) -> RequestEdit:
    """解析一条 curl 命令行。

    Raises:
        ValueError: 空输入 / 不是 curl 开头 / 缺 URL / 未知 flag / `-F` multipart /
            `@file` 引用 / flag 缺值。
    """
    text = command.strip()
    if not text:
        raise ValueError("The clipboard is empty")
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError as exc:
        raise ValueError(f"Could not parse the command: {exc}") from exc
    if not tokens or tokens[0].lower() not in ("curl", "curl.exe"):
        raise ValueError("The clipboard does not contain a curl command")

    method = ""
    url = ""
    headers: list[tuple[str, str]] = []
    content_type_overridden = False
    # 每段数据带各自的编码语义：-d 原样、--data-urlencode 全量编码（curl 允许
    # 两种混用，必须逐段记）。
    data_parts: list[tuple[str, bool]] = []
    data_is_query = False  # -G：-d 数据并进 URL query，方法保持 GET
    compressed = False

    args = tokens[1:]
    i = 0
    while i < len(args):
        token = args[i]
        inline: str | None = None
        if token.startswith("--") and "=" in token:
            token, inline = token.split("=", 1)

        if token in ("-X", "--request"):
            value, i = _take(args, i, inline, token)
            method = value.upper()
        elif token in ("-H", "--header"):
            value, i = _take(args, i, inline, token)
            # `Key:` 禁用默认头，`Key;` 才发送空值头；不带分隔符的项被 curl 忽略。
            name, sep, header_value = value.partition(":")
            if not sep and value.endswith(";"):
                name, sep, header_value = value[:-1], ";", ""
            if sep:
                name = name.strip()
                if name.lower() == "content-type":
                    content_type_overridden = True
                if header_value.strip() or sep == ";":
                    headers.append((name, header_value.strip()))
        elif token in ("-d", "--data", "--data-ascii", "--data-binary", "--data-raw"):
            value, i = _take(args, i, inline, token)
            # `@file` 是 curl 的「从文件读体」语法；--data-raw 除外（它按字面发）。
            if token != "--data-raw" and value.startswith("@"):
                raise ValueError(
                    "Reading the request body from a file (@file) is not "
                    "supported; paste the content instead"
                )
            data_parts.append((value, False))
        elif token == "--data-urlencode":
            value, i = _take(args, i, inline, token)
            if value.startswith("@"):
                raise ValueError(
                    "Reading the request body from a file (@file) is not "
                    "supported; paste the content instead"
                )
            data_parts.append((value, True))
        elif token in ("-G", "--get"):
            data_is_query = True
        elif token in ("-u", "--user"):
            value, i = _take(args, i, inline, token)
            credentials = base64.b64encode(value.encode("utf-8")).decode("ascii")
            headers.append(("Authorization", f"Basic {credentials}"))
        elif token == "--compressed":
            compressed = True
        elif token == "--url":
            url, i = _take(args, i, inline, token)
        elif token in ("-F", "--form"):
            raise ValueError(
                "Multipart forms (-F) are not supported; "
                "paste the body as raw data instead"
            )
        elif token in _IGNORED_BARE:
            pass
        elif token in _IGNORED_VALUE:
            _, i = _take(args, i, inline, token)
        elif token.startswith("-") and token != "-":
            raise ValueError(f"Unsupported curl option: {token}")
        elif not url:
            # 第一个位置参数记 URL；再往后的位置参数忽略（curl 会依次请求多个
            # URL，compose 一次只发一条）。
            url = token
        i += 1

    if not url:
        raise ValueError("The curl command has no URL")

    content = b""
    if data_parts and data_is_query:
        # -G：数据并进查询串。逐段按 curl 语义编码 —— `-d` 只编 query 里非法的
        # 字符（`&` `=` 保持分隔符、既有 `%20` 不二次编），`--data-urlencode`
        # 全量编码 content（issues #81：之前对整段 urlencode，`a=1&b=2` 变成了
        # 一个值 `a%3D1%26b%3D2`，线上 curl 发的是两个参数）。
        parts = urlsplit(url)
        query = parts.query
        for part, urlencoded in data_parts:
            if urlencoded:
                piece = _data_urlencode_part(part)
            else:
                piece = quote(part, safe=_QUERY_SAFE)
            query = f"{query}&{piece}" if query else piece
        url = urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )
    elif data_parts:
        # 多个 -d 按 curl 语义用 & 拼接；--data-urlencode 段先编码。
        content = "&".join(
            _data_urlencode_part(part) if urlencoded else part
            for part, urlencoded in data_parts
        ).encode("utf-8")
        if not content_type_overridden:
            headers.append(("Content-Type", "application/x-www-form-urlencoded"))

    if compressed:
        # 与导出侧对称：`FlowExporter.curl_command` 见到 Accept-Encoding 就写
        # --compressed，这里还原成同一组值（curl 实发列表因版本而异，近似够用）。
        headers.append(("Accept-Encoding", "gzip, deflate"))

    if not method:
        # 有 -d 且无 -X → curl 推 POST；-G 保持 GET。
        method = "POST" if data_parts and not data_is_query else "GET"

    return RequestEdit(method=method, url=url, headers=headers, content=content)
