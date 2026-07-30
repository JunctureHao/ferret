"""HTTP 报文解析工具函数"""

import contextlib
import gzip
import json
import zlib
from datetime import UTC
from urllib.parse import parse_qs, urlparse


def _looks_like_text(data: bytes) -> bool:
    """粗略判断字节流是否为可显示的文本（而非二进制）。

    策略：
    1. 常见文本 BOM（UTF-8/UTF-16/UTF-32）→ 视为文本；
    2. 含 NUL 字节（\\x00）→ 大概率二进制；
    3. 统计非 ASCII 控制字符（0x00-0x08, 0x0E-0x1F 且非 \\t\\n\\r）
       占比，超过阈值视为二进制，避免把二进制按 UTF-8 解成乱码。
    """
    if not data:
        return True
    # BOM 识别
    if data[:3] == b"\xef\xbb\xbf":  # UTF-8 BOM
        return True
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 BOM
        return True
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):  # UTF-32 BOM
        return True

    # 含 NUL 字节基本可判定为二进制
    if b"\x00" in data:
        return False

    # 控制字符比例（排除常见的 \t \n \r \f \v）
    control = 0
    sample = data[:8192]  # 仅取样，性能考虑
    for b in sample:
        if b < 0x09 or (0x0E <= b <= 0x1F):
            control += 1
    if sample:
        ratio = control / len(sample)
        # 超过 1% 的控制字符即视为二进制
        if ratio > 0.01:
            return False

    # UTF-8 可解码性：真实文本能干净地解码，替换符占比极低；
    # 而“把明文误当 deflate 解压出来的乱码”是随机字节，UTF-8 解码会产生
    # 大量 \ufffd（替换符），借此把这类误判挡在二进制一侧。
    try:
        decoded = data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return False
    if decoded:
        repl = decoded.count("\ufffd")
        if repl / len(decoded) > 0.01:
            return False
    return True


def decode_body(
    body: bytes, content_type: str = "", decompress: bool = True
) -> str | None:
    """解码响应体：可选 gzip/deflate 解压 + 编码检测。

    Args:
        body: 响应体字节流。
        content_type: Content-Type 头，用于提取 charset。
        decompress: 是否尝试 gzip/deflate 解压。

            当调用方传入的已经是“解压后的内容”（如 mitmproxy 的
            ``message.content``）时必须为 ``False``。否则对明文 JSON 误跑
            raw-deflate 解压会“成功”产出乱码（zlib 对巧合数据不报错），
            导致嗅探判定为二进制 / 渲染成 ``\\ufffd``。

            仅当传入的是“线上原始字节”（如 ``to_raw_response`` 拼出的含
            ``Content-Encoding`` 的报文）时才用默认的 ``True``。

    若解压后的内容经嗅探判定为二进制（如图片/压缩包/非 UTF-8 字节流），
    返回 None，交由 build_body 走二进制展示路径，避免渲染乱码。
    """
    if not body:
        return ""

    if decompress:
        # 1. 尝试解压 gzip
        decompressed = False
        try:
            body = gzip.decompress(body)
            decompressed = True
        except (gzip.BadGzipFile, OSError):
            pass

        # 2. 尝试解压 deflate（仅当 gzip 未命中时）
        #    Content-Encoding: deflate 在现实中既可能是带 zlib 头的流，
        #    也可能是裸 deflate 流，因此两者都试。
        #    注意：deflate 对“非压缩的明文数据”可能不抛异常而产出乱码，
        #    因此解压后必须做一次二进制嗅探（含 UTF-8 可解码性），
        #    嗅探不过则回退到原始字节，绝不直接采用解压结果。
        if not decompressed:
            for attempt in (
                lambda b: zlib.decompress(b),
                lambda b: zlib.decompress(b, -zlib.MAX_WBITS),
            ):
                try:
                    candidate = attempt(body)
                except zlib.error:
                    candidate = None
                if candidate is not None and _looks_like_text(candidate):
                    body = candidate
                    break

    # 3. 二进制嗅探：非文本直接返回 None（走二进制展示）
    if not _looks_like_text(body):
        return None

    # 4. 从 Content-Type 提取编码
    encoding = "utf-8"
    if "charset=" in content_type:
        for part in content_type.split(";"):
            if "charset=" in part:
                encoding = part.split("charset=")[-1].strip().strip('"')
                break

    # 5. 解码；若编码不支持或解码失败，回退 UTF-8（仍可能含少量替换符）
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

    # 文本类：尝试解码。
    # 注意：raw 来自 mitmproxy 的 message.content，已是解压后的内容，
    # 因此 decompress=False，避免 decode_body 对明文再跑 deflate 误产乱码。
    text = decode_body(raw, content_type, decompress=False)
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
