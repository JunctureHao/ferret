"""Bounded body previews; captured bytes and native full-body exports stay intact."""

from __future__ import annotations

import json
from copy import copy

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import (
    PreviewDecodingUnavailable,
    Request,
    assemble_request_head,
    assemble_response_head,
    bounded_content_decoding,
    contentviews,
    human,
)

MAX_PREVIEW_SIZE = 1024 * 1024
MAX_TEXT_SIZE = 256 * 1024
MAX_PRETTY_SIZE = 64 * 1024
_PREVIEW_ENCODINGS = {"", "none", "identity", "gzip", "deflate", "deflateraw", "zstd"}


def build_raw_preview(flow, side: str) -> dict[str, str]:
    """Bounded display message; full raw export remains a separate API.

    The body is decoded (gzip/deflate/zstd) before display: the fallback path in
    the detail panel also fills the Raw tab with decoded body text, and showing
    wire bytes here would render compressed bodies as garbage. Copy/save-as
    still exports the untouched wire bytes via `get_raw_request/response`.
    """
    message = flow.request if side == "Request" else flow.response
    if message is None:
        return {"text": "", "notice": ""}
    wire = message.raw_content or b""
    decoded = _bounded_decoded_content(message, wire)
    if decoded is None:
        # 编码不支持有界解码：退回线上字节，至少头部仍然可读。
        head_message, body = message, wire
    elif decoded != wire:
        head_message = _message_copy(message, decoded, decoded=True)
        head_message.headers["content-length"] = str(len(decoded))
        body = decoded
    else:
        head_message, body = message, wire
    head = (
        assemble_request_head(head_message)
        if side == "Request"
        else assemble_response_head(head_message)
    )
    total = len(head) + len(body)
    preview = head[:MAX_TEXT_SIZE] + body[: max(0, MAX_TEXT_SIZE - len(head))]
    notice = ""
    if total > MAX_TEXT_SIZE:
        notice = QCoreApplication.translate(
            "FlowDetail", "原始报文仅显示前 {} 字节；完整内容可通过导出查看。"
        ).format(MAX_TEXT_SIZE)
    return {"text": preview.decode("utf-8", "replace"), "notice": notice}


def _bounded_decoded_content(message, wire: bytes) -> bytes | None:
    """Decode the wire body within budget; None means unsupported encoding."""
    encoding = message.headers.get("content-encoding", "").lower()
    if encoding in ("", "none", "identity"):
        return wire
    if encoding not in _PREVIEW_ENCODINGS:
        return None
    preview = _message_copy(message, wire[: MAX_PREVIEW_SIZE + 1])
    try:
        with bounded_content_decoding(
            MAX_PREVIEW_SIZE, incomplete_input=len(wire) > MAX_PREVIEW_SIZE
        ):
            return preview.get_content(strict=False) or b""
    except (PreviewDecodingUnavailable, ValueError):
        return None


def _message_copy(message, raw: bytes, *, decoded: bool = False):
    # A Message owns a mutable data dataclass. Copy both shells and headers,
    # sharing only immutable bytes; never change content/headers on the flow.
    result = copy(message)
    result.data = copy(message.data)
    result.headers = message.headers.copy()
    result.raw_content = raw
    if decoded:
        result.headers.pop("content-encoding", None)
    return result


def build_body(flow, message, max_size: int = MAX_PRETTY_SIZE) -> dict:
    """Return a preview with explicit truncation/deferral and exact sizes if known.

    Compression is bounded while native get_content executes. Text is decoded
    from a bounded prefix of those bytes with native charset/BOM inference.
    Neither the preview copy nor a partial form is written back to the flow.
    """
    wire = message.raw_content or b""
    body = {
        "raw": b"",
        "raw_complete": False,
        "text": "",
        "pretty": None,
        "view": "",
        "syntax": "none",
        "truncated": False,
        "notice": "",
        "decoded_size": None,
        "form": [],
    }
    if not wire:
        body["decoded_size"] = 0
        body["raw_complete"] = True
        return body

    encoding = message.headers.get("content-encoding", "").lower()
    if encoding not in _PREVIEW_ENCODINGS:
        body["notice"] = QCoreApplication.translate(
            "FlowDetail", "此正文编码暂不支持有界预览，请导出完整正文查看。"
        )
        return body

    encoded_limited = len(wire) > MAX_PREVIEW_SIZE
    preview = _message_copy(message, wire[: MAX_PREVIEW_SIZE + 1])
    try:
        with bounded_content_decoding(
            MAX_PREVIEW_SIZE, incomplete_input=encoded_limited
        ):
            decoded = preview.get_content(strict=False) or b""
    except PreviewDecodingUnavailable:
        body["notice"] = QCoreApplication.translate(
            "FlowDetail", "此正文编码暂不支持有界预览，请导出完整正文查看。"
        )
        return body

    raw = decoded[:MAX_PREVIEW_SIZE]
    truncated = encoded_limited or len(decoded) > MAX_PREVIEW_SIZE
    # Binary previews need every byte, but the smaller text budget may truncate
    # its display while the decoded bytes still contain a complete image.
    body["raw_complete"] = not truncated
    # For identity bodies the full size is already known without decoding.
    if encoding in ("", "none", "identity"):
        body["decoded_size"] = len(wire)
    elif not truncated:
        body["decoded_size"] = len(decoded)

    text_message = _message_copy(message, raw[:MAX_TEXT_SIZE], decoded=True)
    try:
        with bounded_content_decoding(MAX_TEXT_SIZE):
            text = _safe_text(text_message)[:MAX_TEXT_SIZE]
    except PreviewDecodingUnavailable:
        text = raw[:MAX_TEXT_SIZE].decode("utf-8", "replace")
    truncated = truncated or len(raw) > MAX_TEXT_SIZE
    body.update(raw=raw, text=text, truncated=truncated)
    if truncated:
        body["notice"] = QCoreApplication.translate(
            "FlowDetail", "仅显示正文预览（文本最多 {size}），完整正文可通过导出查看。"
        ).format(size=human.pretty_size(MAX_TEXT_SIZE))
        return body

    # Native form extraction must read the bounded decoded copy too: reading
    # flow.request.urlencoded_form here would silently decompress the full body.
    if isinstance(text_message, Request) and len(raw) <= MAX_PRETTY_SIZE:
        body["form"] = list(text_message.urlencoded_form.items(multi=True))

    if len(raw) > min(max_size, MAX_PRETTY_SIZE) or not _pretty_depth_allowed(raw):
        return body
    result = contentviews.prettify_message(text_message, flow)
    pretty = result.text
    if result.view_name == "JSON":
        try:
            pretty = json.dumps(json.loads(pretty), indent=2, ensure_ascii=False)
        except (ValueError, RecursionError):
            pass
    if len(pretty) > MAX_TEXT_SIZE:
        body["notice"] = QCoreApplication.translate(
            "FlowDetail", "美化结果超过预览限制，已显示原文。"
        )
        return body
    body.update(
        pretty=pretty, view=result.view_name or "", syntax=result.syntax_highlight
    )
    return body


def _pretty_depth_allowed(raw: bytes) -> bool:
    """Cap JSON indentation amplification before invoking a contentview."""
    if not raw.lstrip().startswith((b"{", b"[")):
        return True
    depth = 0
    quoted = escaped = False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 64:
                return False
        elif byte in (93, 125):
            depth -= 1
    return True


def _safe_text(message) -> str:
    """Native charset decoding, replacing surrogate escapes before Qt sees them."""
    text = message.get_text(strict=False) or ""
    if not isinstance(text, str):
        text = (message.raw_content or b"").decode("utf-8", "replace")
    # str.translate avoids allocating a second UTF-8 encoding of the text.
    return text.translate(_SURROGATE_REPLACEMENTS)


_SURROGATE_REPLACEMENTS = dict.fromkeys(range(0xD800, 0xE000), ord("?"))
