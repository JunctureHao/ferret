"""HTTP 报文的 UI 派生数据。

解析本身全部交给 mitmproxy 官方能力，这里只保留 mitmproxy 没有、
且纯属界面展示的部分：

- Content-Encoding 解压 + charset 解码 → ``Message.get_text(strict=False)``
- Content-Type 解析                    → ``mitmproxy.net.http.headers.parse_content_type``
- 二进制嗅探                            → ``mitmproxy.utils.strutils.is_mostly_bin``
- query / cookie 解析                   → ``Request.query`` / ``Request.cookies``
                                          / ``Response.cookies``
- 时间戳格式化                          → ``mitmproxy.utils.human.format_timestamp``
"""

import json

from mitmproxy.net.http.headers import parse_content_type
from mitmproxy.utils import strutils

MAX_PRETTY_SIZE = 1024 * 1024  # 1MB 以上跳过 json 美化与折叠计算


def format_bytes(size: int) -> str:
    """人性化字节大小。

    不用 ``mitmproxy.utils.human.pretty_size``：那是给终端用的紧凑格式
    （``512b`` / ``1.0k`` / ``1.0g``，总长不超过 5 字符），与本项目详情面板
    的 ``512 B`` / ``1.00 KB`` 风格不一致。
    """
    value = float(size)
    if value < 1024:
        return f"{size} B"
    for unit in ("KB", "MB"):
        value /= 1024
        if value < 1024:
            return f"{value:.2f} {unit}"
    return f"{value / 1024:.2f} GB"


def compute_folds(text: str) -> list[dict]:
    """栈匹配括号对，返回所有跨行折叠区域。

    mitmproxy 没有等价能力（它只产出扁平文本），这是编辑器折叠专用。

    返回: [{"start": int, "end": int, "brace": str}, ...]
    - start/end 为 0-based 行号
    - brace 为起始括号类型 "{" / "["
    - 单行配对（起止同行）被过滤
    """
    folds: list[dict] = []
    stack: list[tuple[int, str]] = []  # (行号, 括号类型)
    lines = text.split("\n")
    for line_no, line in enumerate(lines):
        for ch in line:
            if ch in ("{", "["):
                stack.append((line_no, ch))
            elif ch in ("}", "]") and stack:
                start_line, brace = stack.pop()
                # 只记录跨行的配对
                if start_line != line_no:
                    folds.append({"start": start_line, "end": line_no, "brace": brace})
    return folds


def build_body(message, max_size: int = MAX_PRETTY_SIZE) -> dict:
    """分类并解码 body，一次性产出 UI 所需的所有派生数据。

    Args:
        message: mitmproxy 的 ``http.Request`` / ``http.Response``。
        max_size: 超过该字节数则跳过 json 美化与折叠计算。

    返回:
    {
        "raw": bytes,              # 解压后的原始字节（Content-Encoding 已还原）
        "text": str | None,        # 解码后文本；二进制为 None
        "pretty": str | None,      # json.dumps(indent=2) 缩进文本；非 json 或过大为 None
        "fold_regions": list,      # compute_folds 结果（仅 json 且未超限）
        "is_binary": bool,
        "mime": str,               # Content-Type 的 type/subtype
    }
    """
    mime = mime_of(message)
    raw = message.get_content(strict=False) or b""

    if _is_binary_mime(mime) or _is_binary_content(raw):
        # 二进制：绝不解码，保留原始字节语义
        return {
            "raw": raw,
            "text": None,
            "pretty": None,
            "fold_regions": [],
            "is_binary": True,
            "mime": mime,
        }

    text = _safe_text(message)

    pretty = None
    fold_regions: list[dict] = []
    if len(raw) <= max_size and _is_json(mime, text):
        try:
            pretty = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError):
            pretty = None
        else:
            fold_regions = compute_folds(pretty)

    return {
        "raw": raw,
        "text": text,
        "pretty": pretty,
        "fold_regions": fold_regions,
        "is_binary": False,
        "mime": mime,
    }


def mime_of(message) -> str:
    """取 Content-Type 的 ``type/subtype``（小写、去参数）。"""
    parsed = parse_content_type(message.headers.get("content-type", ""))
    return f"{parsed[0]}/{parsed[1]}" if parsed else "text/plain"


def _safe_text(message) -> str:
    """``Message.get_text(strict=False)``，并清掉孤立代理字符。

    ``get_text`` 解码失败时兜底用 ``surrogateescape``，产出的孤立代理字符
    无法编码为 UTF-8，交给 Qt 渲染有风险，这里统一折成替换符。
    """
    text = message.get_text(strict=False) or ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text


def _is_binary_content(raw: bytes) -> bool:
    """内容嗅探。

    ``is_mostly_bin`` 只看前 ~100 字节且不看 NUL，因此对“ASCII 开头的长二进制”
    会漏判；补一个 NUL 判定——出现 NUL 基本可断定二进制，且比按文本渲染
    几 MB 乱码要安全得多。
    """
    return b"\x00" in raw[:8192] or strutils.is_mostly_bin(raw)


def _is_binary_mime(mime: str) -> bool:
    """根据 mime 判断是否为二进制内容（不应做文本高亮/折叠）"""
    binary_prefixes = ("image/", "audio/", "video/", "application/octet-stream")
    binary_types = (
        "application/pdf",
        "application/zip",
        "application/gzip",
        "application/x-binary",
        "application/x-protobuf",
        "application/wasm",
        "application/vnd.rar",
        "application/x-7z-compressed",
    )
    if mime in binary_types:
        return True
    return any(mime.startswith(p) for p in binary_prefixes)


def _is_json(mime: str, text: str) -> bool:
    """根据 mime 或首字符判断是否为 JSON"""
    if "json" in mime:
        return True
    stripped = text.strip()
    return bool(stripped) and stripped[0] in ("{", "[")
