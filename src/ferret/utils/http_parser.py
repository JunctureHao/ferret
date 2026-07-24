"""HTTP 报文解析工具函数"""

import contextlib
import gzip
import json
import zlib
from datetime import UTC
from urllib.parse import parse_qs, urlparse


def decode_body(body: bytes, content_type: str = "") -> str:
    """解码响应体：尝试 gzip/deflate 解压 + 编码检测"""
    if not body:
        return ""

    # 1. 尝试解压 gzip
    try:
        body = gzip.decompress(body)
    except (gzip.BadGzipFile, OSError):
        pass

    # 2. 尝试解压 deflate
    try:
        body = zlib.decompress(body, -zlib.MAX_WBITS)
    except zlib.error:
        pass

    # 3. 从 Content-Type 提取编码
    encoding = "utf-8"
    if "charset=" in content_type:
        for part in content_type.split(";"):
            if "charset=" in part:
                encoding = part.split("charset=")[-1].strip().strip('"')
                break

    # 4. 解码
    try:
        return body.decode(encoding, errors="replace")
    except (UnicodeDecodeError, LookupError):
        return body.decode("utf-8", errors="replace")


def parse_cookies_from_headers(headers: dict, header_key: str) -> dict:
    """从请求/响应头中解析 Cookie（大小写不敏感）"""
    result = {}
    raw = ""
    header_key_lower = header_key.lower()
    for k, v in headers.items():
        if k.lower() == header_key_lower:
            raw = v
            break
    if not raw:
        return result
    # 忽略非字符串等异常值，保留已解析的部分
    with contextlib.suppress(AttributeError, TypeError):
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                result[key.strip()] = value.strip()
    return result


def parse_set_cookies(headers: dict) -> list[dict]:
    """从响应头中解析 Set-Cookie（大小写不敏感，支持多个 Set-Cookie）"""
    result = []
    set_cookie_values = []
    for k, v in headers.items():
        if k.lower() == "set-cookie":
            set_cookie_values.append(v)
    if not set_cookie_values:
        return result
    for raw in set_cookie_values:
        # 跳过格式异常的单个 cookie，继续处理其余
        with contextlib.suppress(AttributeError, TypeError):
            parts = raw.split(";")
            if parts:
                first = parts[0].strip()
                if "=" in first:
                    name, _, value = first.partition("=")
                    item = {"name": name.strip(), "value": value.strip()}
                    for p in parts[1:]:
                        p = p.strip()
                        if "=" in p:
                            k2, _, v2 = p.partition("=")
                            item[k2.strip().lower()] = v2.strip()
                    result.append(item)
    return result


def format_time(ts) -> str:
    """格式化时间戳（本地时区显示）"""
    from datetime import datetime

    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_ms(ts) -> str:
    """格式化毫秒"""
    if not ts:
        return "-"
    return f"{ts:.6f}"


def format_bytes(size: int) -> str:
    """人性化字节大小"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 * 1024:
        return f"{size / 1024:.2f} KB"
    else:
        return f"{size / (1024 * 1024):.2f} MB"


# ──────────────────────────────────────────────────────────────
# 结构化解析：在 mitmproxy 阶段一次性完成，UI 只消费不解析
# ──────────────────────────────────────────────────────────────

MAX_PRETTY_SIZE = 1024 * 1024  # 1MB 以上跳过 json 美化与折叠计算


def compute_folds(text: str) -> list[dict]:
    """栈匹配括号对，返回所有跨行折叠区域。

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


def build_body(
    raw: bytes, content_type: str = "", max_size: int = MAX_PRETTY_SIZE
) -> dict:
    """解码并分类 body，一次性产出 UI 所需的所有派生数据。

    返回:
    {
        "text": str | None,        # 解码后文本；二进制为 None
        "pretty": str | None,      # json.dumps(indent=2) 缩进文本；非 json 或过大为 None
        "fold_regions": list,      # compute_folds 结果（仅 json 且未超限）
        "is_binary": bool,
        "mime": str,               # 推测的 mime 类型
    }
    """
    mime = _guess_mime(content_type)
    is_binary = _is_binary_mime(mime)

    if is_binary:
        # 二进制：绝不解码，保留原始字节语义
        return {
            "text": None,
            "pretty": None,
            "fold_regions": [],
            "is_binary": True,
            "mime": mime,
        }

    # 文本类：尝试解码（decode_body 内部已处理 gzip/deflate/charset）
    text = decode_body(raw, content_type)
    if text is None:
        # 解码失败也按二进制处理，避免渲染乱码
        return {
            "text": None,
            "pretty": None,
            "fold_regions": [],
            "is_binary": True,
            "mime": mime,
        }

    size = len(raw)
    pretty = None
    fold_regions = []
    if _is_json(content_type, text) and size <= max_size:
        try:
            parsed = json.loads(text)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            fold_regions = compute_folds(pretty)
        except (json.JSONDecodeError, ValueError):
            pretty = None
            fold_regions = []

    return {
        "text": text,
        "pretty": pretty,
        "fold_regions": fold_regions,
        "is_binary": False,
        "mime": mime,
    }


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


def _is_json(content_type: str, text: str) -> bool:
    """根据 Content-Type 或首字符判断是否为 JSON"""
    ct = (content_type or "").lower()
    if "json" in ct:
        return True
    stripped = text.strip()
    return bool(stripped) and stripped[0] in ("{", "[")


def _guess_mime(content_type: str) -> str:
    """从 Content-Type 提取 mime（小写，去参数）"""
    ct = (content_type or "").lower().strip()
    if ";" in ct:
        ct = ct.split(";", 1)[0].strip()
    return ct or "text/plain"


def parse_params(url: str) -> dict:
    """解析 URL query 参数为字典，替代 UI 中的实时拆分。

    返回: {key: value}，多值用 ", " 连接
    """
    if not url:
        return {}
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        return {k: (v[0] if len(v) == 1 else ", ".join(v)) for k, v in qs.items()}
    except (ValueError, TypeError, AttributeError):
        return {}
