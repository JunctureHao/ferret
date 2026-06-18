"""HTTP 报文解析工具函数"""

import gzip
import zlib


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
    try:
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                result[key.strip()] = value.strip()
    except Exception:
        pass
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
        try:
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
        except Exception:
            pass
    return result


def format_time(ts) -> str:
    """格式化时间戳"""
    from datetime import datetime

    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


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
