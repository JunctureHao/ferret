"""Body previews and Compose payload codecs, without mutating captured flows."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

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


@dataclass(frozen=True)
class FormField:
    """A Compose form row, independent of widgets and live mitmproxy objects.

    A nonempty file_path is read at send time. Imported files instead retain
    their payload in memory; None means no file selected, while b"" is a file.
    """

    enabled: bool = True
    kind: Literal["text", "file"] = "text"
    name: str = ""
    value: str = ""
    file_path: str = ""
    filename: str = ""
    content_type: str = ""
    payload: bytes | None = None


_MIME_TOKEN = r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+"
_MIME_PARAMETER = re.compile(
    rf'\s*;\s*({_MIME_TOKEN})\s*=\s*(?:"((?:[^"\\\r\n]|\\[^\r\n])*)"|({_MIME_TOKEN}))'
)
_MIME_TYPE = re.compile(rf"{_MIME_TOKEN}/{_MIME_TOKEN}")
_BOUNDARY_VALUE = re.compile(r"[0-9a-zA-Z'()+_,./:=? -]{1,70}")
_FORM_TEXT_CHARSETS = {"utf-8", "utf8", "us-ascii", "ascii"}


def _parameterized_header(value: str) -> tuple[str, dict[str, str]] | None:
    """Strict MIME parameters; ambiguous/unsupported input stays in Raw mode.

    Email parsers recover malformed headers and decode extended parameters.
    That recovery would silently discard information our field model cannot
    represent, so accept only complete token/quoted-string parameters here.
    """
    if any(ord(character) < 32 and character != "\t" for character in value):
        return None
    if "\x7f" in value:
        return None
    head, separator, _ = value.partition(";")
    parameters: dict[str, str] = {}
    offset = len(head)
    while separator and offset < len(value):
        if not value[offset:].strip():
            break
        match = _MIME_PARAMETER.match(value, offset)
        if match is None:
            return None
        key = match[1].lower()
        if key in parameters:
            return None
        parameter = match[2]
        parameters[key] = (
            re.sub(r"\\(.)", r"\1", parameter) if parameter is not None else match[3]
        )
        offset = match.end()
    return head.strip().lower(), parameters


def _checked_boundary(boundary: str) -> bytes:
    # RFC 2046 bchars: internal spaces are allowed, a trailing space is not.
    if not _BOUNDARY_VALUE.fullmatch(boundary) or boundary.endswith(" "):
        raise ValueError(
            QCoreApplication.translate(
                "ComposeBody",
                "表单边界无效：须为 1 至 70 个有效 ASCII 字符，且不能以空格结尾。",
            )
        )
    return boundary.encode("ascii")


def _multipart_delimiter(boundary: bytes) -> re.Pattern[bytes]:
    # RFC 2046 allows transport padding after a delimiter. Prefixes such as
    # --boundaryX are ordinary payload bytes, not collisions or new parts.
    return re.compile(
        rb"(?:\A|\r\n)--" + re.escape(boundary) + rb"(--)?[ \t]*(?:\r\n|\Z)"
    )


def multipart_boundary(content_type: str) -> str | None:
    """Read a multipart boundary, rejecting malformed/duplicate parameters."""
    media_type = content_type.partition(";")[0].strip().lower()
    if not media_type.startswith("multipart/"):
        return None
    parsed = _parameterized_header(content_type)
    if parsed is None or not _MIME_TYPE.fullmatch(parsed[0]):
        raise ValueError(
            QCoreApplication.translate("ComposeBody", "multipart 请求头格式无效。")
        )
    boundary = parsed[1].get("boundary")
    if boundary is not None:
        _checked_boundary(boundary)
    return boundary


def read_body_file(path: str) -> bytes:
    """Read a selected regular file. Call from a background task, never Qt UI."""
    if not path:
        raise ValueError(
            QCoreApplication.translate("ComposeBody", "请先选择要发送的文件。")
        )
    file = Path(path)
    try:
        if not file.is_file():
            raise ValueError(
                QCoreApplication.translate(
                    "ComposeBody", "文件不存在或不是普通文件：{}"
                ).format(path)
            )
        return file.read_bytes()
    except OSError as exc:
        raise ValueError(
            QCoreApplication.translate("ComposeBody", "无法读取文件：{}（{}）").format(
                path, str(exc)
            )
        ) from exc


def _quoted_form_parameter(value: str) -> bytes:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(
            QCoreApplication.translate(
                "ComposeBody", "表单字段名和文件名不能包含换行或控制字符。"
            )
        )
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"').encode("utf-8")


def _form_part(field: FormField) -> bytes:
    head = b"Content-Disposition: form-data; name=" + _quoted_form_parameter(field.name)
    content_type = field.content_type
    if field.kind == "file":
        if field.file_path:
            payload = read_body_file(field.file_path)
        elif field.payload is not None:
            payload = field.payload
        else:
            raise ValueError(
                QCoreApplication.translate(
                    "ComposeBody", "表单文件字段“{}”尚未选择文件。"
                ).format(field.name)
            )
        filename = field.filename or (
            Path(field.file_path).name if field.file_path else ""
        )
        head += b"; filename=" + _quoted_form_parameter(filename)
        content_type = content_type or "application/octet-stream"
    elif field.kind == "text":
        payload = field.value.encode("utf-8")
    else:
        raise ValueError(
            QCoreApplication.translate("ComposeBody", "不支持的表单字段类型。")
        )
    if content_type:
        parsed_type = _parameterized_header(content_type)
        if parsed_type is None or not _MIME_TYPE.fullmatch(parsed_type[0]):
            raise ValueError(
                QCoreApplication.translate(
                    "ComposeBody", "表单字段“{}”的 Content-Type 无效。"
                ).format(field.name)
            )
        if field.kind == "text":
            charset = parsed_type[1].get("charset", "utf-8").lower()
            if charset not in _FORM_TEXT_CHARSETS:
                raise ValueError(
                    QCoreApplication.translate(
                        "ComposeBody", "表单字段“{}”使用了不支持的字符集：{}。"
                    ).format(field.name, charset)
                )
            try:
                payload = field.value.encode(charset)
            except UnicodeEncodeError as exc:
                raise ValueError(
                    QCoreApplication.translate(
                        "ComposeBody", "表单字段“{}”的文本无法使用字符集 {} 编码。"
                    ).format(field.name, charset)
                ) from exc
        head += b"\r\nContent-Type: " + content_type.encode("utf-8")
    return head + b"\r\n\r\n" + payload


def encode_multipart(
    fields: Sequence[FormField], boundary: str | None = None
) -> tuple[bytes, str]:
    """Serialize one immutable send snapshot using bytes and exact CRLF framing.

    File reads happen here for path-backed rows, so the caller must schedule
    this operation in FunctionTask. Disabled rows are not validated or read.
    """
    try:
        parts = [_form_part(field) for field in fields if field.enabled]
    except UnicodeEncodeError as exc:
        raise ValueError(
            QCoreApplication.translate(
                "ComposeBody", "表单内容包含无法编码为 UTF-8 的字符。"
            )
        ) from exc
    # A caller-supplied boundary must be honoured or fail. A generated one can
    # be retried if the payload happens to contain its delimiter on a new line.
    for _ in range(16):
        candidate = boundary if boundary is not None else secrets.token_hex(16)
        encoded = _checked_boundary(candidate)
        delimiter_pattern = _multipart_delimiter(encoded)
        if not any(delimiter_pattern.search(part) for part in parts):
            break
        if boundary is not None:
            raise ValueError(
                QCoreApplication.translate(
                    "ComposeBody",
                    "表单内容与 boundary 冲突，请修改请求头中的 boundary。",
                )
            )
    else:
        raise ValueError(
            QCoreApplication.translate(
                "ComposeBody", "无法生成与表单内容兼容的 boundary。"
            )
        )
    delimiter = b"--" + encoded
    body = b"".join(delimiter + b"\r\n" + part + b"\r\n" for part in parts)
    body += delimiter + b"--\r\n"
    parameter = (
        candidate if re.fullmatch(_MIME_TOKEN, candidate) else '"' + candidate + '"'
    )
    return body, "multipart/form-data; boundary=" + parameter


def _parse_form_part(part: bytes) -> FormField | None:
    raw_headers, separator, payload = part.partition(b"\r\n\r\n")
    if not separator:
        return None
    headers: dict[str, str] = {}
    try:
        for line in raw_headers.split(b"\r\n"):
            name, separator, value = line.partition(b":")
            key = name.decode("ascii").lower()
            if (
                not separator
                or key not in ("content-disposition", "content-type")
                or key in headers
            ):
                return None
            headers[key] = value.decode("utf-8").strip(" \t")
        disposition = _parameterized_header(headers.get("content-disposition", ""))
        if disposition is None or disposition[0] != "form-data":
            return None
        parameters = disposition[1]
        if "name" not in parameters or parameters.keys() - {"name", "filename"}:
            return None
        # Header parameters with controls cannot later be safely regenerated.
        for value in parameters.values():
            _quoted_form_parameter(value)
        content_type = headers.get("content-type", "")
        type_parameters: dict[str, str] = {}
        if content_type:
            parsed_type = _parameterized_header(content_type)
            if parsed_type is None or not _MIME_TYPE.fullmatch(parsed_type[0]):
                return None
            type_parameters = parsed_type[1]
        if "filename" in parameters:
            return FormField(
                kind="file",
                name=parameters["name"],
                filename=parameters["filename"],
                content_type=content_type,
                payload=payload,
            )
        charset = type_parameters.get("charset", "utf-8").lower()
        if charset not in _FORM_TEXT_CHARSETS:
            return None
        return FormField(
            name=parameters["name"],
            value=payload.decode(charset),
            content_type=content_type,
        )
    except (UnicodeError, ValueError):
        return None


def parse_multipart(content: bytes, content_type: str) -> list[FormField] | None:
    """Return fields only when the body fits the editable model without loss.

    Raw mode remains available for unknown part headers, non-UTF-8 text,
    extended dispositions, preambles/epilogues and malformed framing. Original
    body bytes and the original boundary remain the UI's unedited baseline.
    """
    if content_type.partition(";")[0].strip().lower() != "multipart/form-data":
        return None
    try:
        boundary = multipart_boundary(content_type)
    except ValueError:
        return None
    if boundary is None:
        return None
    delimiter = _multipart_delimiter(boundary.encode("ascii"))
    markers = list(delimiter.finditer(content))
    if (
        not markers
        or markers[0].start() != 0
        or not markers[-1][1]
        or markers[-1].end() != len(content)
        or any(marker[1] for marker in markers[:-1])
    ):
        return None
    fields: list[FormField] = []
    for opening, closing in pairwise(markers):
        field = _parse_form_part(content[opening.end() : closing.start()])
        if field is None:
            return None
        fields.append(field)
    return fields


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
    if side == "Request" and not head_message.authority:
        # origin-form 请求 authority 为空，原生拼装只会在请求行里放路径，
        # host 只剩 Host 头。展示层统一补成完整 URL，直观且与 h2 一致。
        authority = head_message.host_header or head_message.pretty_host
        default_port = 443 if head_message.scheme == "https" else 80
        if authority and ":" not in authority and head_message.port != default_port:
            authority = f"{authority}:{head_message.port}"
        if authority:
            head_message = _message_copy(head_message, head_message.raw_content or b"")
            head_message.authority = authority
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
