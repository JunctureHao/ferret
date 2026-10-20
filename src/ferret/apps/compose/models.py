"""Compose 已保存请求（请求库）的纯数据模型。

落盘格式 ``compose_requests.json``（Roaming，见
``core.settings.get_compose_requests_file``）::

    {"version": 2, "folders": [...], "requests": [...]}

模型不依赖 QtWidgets，也不引用 mitmproxy 活对象：序列化与 ``RequestEdit``
同一原则——纯数据才能安全地在线程与进程重启之间搬运。``from_dict`` 的
校验消息不上界面，按约定不译（AGENTS.md §7）。

正文同时保留可编辑字段与未修改的原始字节，恢复请求不会重编码签名内容。
文件路径在发送时读取；抓包导入的内存文件保留其 payload。
"""

from __future__ import annotations

import base64
import binascii
import math
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

SCHEMA_VERSION = 2

TEXT_KINDS = frozenset({"json", "text", "html", "xml"})
BODY_KINDS = TEXT_KINDS | {"none", "raw", "urlencoded", "multipart", "file"}

__all__ = [
    "BODY_KINDS",
    "SCHEMA_VERSION",
    "TEXT_KINDS",
    "SavedBody",
    "SavedFormField",
    "SavedRequest",
    "SavedWebSocket",
    "duplicate",
]


def _pairs(value: Any, what: str) -> tuple[tuple[str, str], ...]:
    """校验并归一化有序键值对列表；重复键保留（Cookie 等合法同名头）。"""
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")  # noqa: TRY004
    result: list[tuple[str, str]] = []
    for item in value:
        if (
            not isinstance(item, list | tuple)
            or len(item) != 2
            or not all(isinstance(part, str) for part in item)
        ):
            raise ValueError(f"{what} entries must be [name, value] pairs")
        result.append((item[0], item[1]))
    return tuple(result)


def _bytes(value: Any, what: str) -> bytes | None:
    """JSON null means absent; an empty base64 string represents empty bytes."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} must be a base64 string or null")  # noqa: TRY004
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{what} is not valid base64") from exc


def _encoded(value: bytes | None) -> str | None:
    return base64.b64encode(value).decode("ascii") if value is not None else None


@dataclass(frozen=True)
class SavedFormField:
    """A form field including disabled rows and imported in-memory files."""

    enabled: bool = True
    kind: str = "text"  # "text" | "file"
    name: str = ""
    value: str = ""
    file_path: str = ""
    filename: str = ""
    content_type: str = ""
    payload: bytes | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
            "file_path": self.file_path,
            "filename": self.filename,
            "content_type": self.content_type,
            "payload_b64": _encoded(self.payload),
        }

    @staticmethod
    def from_dict(data: Any) -> SavedFormField:
        if not isinstance(data, dict):
            raise ValueError("form field must be an object")  # noqa: TRY004
        kind = data.get("kind", "text")
        if kind not in ("text", "file"):
            raise ValueError(f"unsupported form field kind: {kind!r}")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("form field enabled must be a bool")  # noqa: TRY004
        strings = {
            key: data.get(key, "")
            for key in ("name", "value", "file_path", "filename", "content_type")
        }
        if not all(isinstance(part, str) for part in strings.values()):
            raise ValueError("form field values must be strings")
        return SavedFormField(
            enabled=enabled,
            kind=kind,
            payload=_bytes(data.get("payload_b64"), "form field payload"),
            **strings,
        )


@dataclass(frozen=True)
class SavedBody:
    """Editable body documents plus exact unedited form bytes.

    Text keeps its original line endings; binary text uses raw_b64. Inactive
    documents survive kind changes, including temporarily selecting no body.
    Raw form bytes are usable only with the accompanying unchanged fields;
    multipart also retains its source Content-Type for boundary matching.
    """

    kind: str = "none"
    text: str = ""
    raw_b64: str = ""
    urlencoded: tuple[tuple[str, str], ...] = ()
    multipart: tuple[SavedFormField, ...] = ()
    file: SavedFormField | None = None
    suspended_kind: str = "none"
    form_raw: bytes | None = None
    multipart_raw: bytes | None = None
    multipart_content_type: str = ""

    def raw_content(self) -> bytes:
        """raw 栏的字节；保存时已经校验过 base64，正常路径不会抛。"""
        return base64.b64decode(self.raw_b64) if self.raw_b64 else b""

    def text_bytes(self) -> bytes:
        return self.raw_content() if self.raw_b64 else self.text.encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "raw_b64": self.raw_b64,
            "urlencoded": [list(pair) for pair in self.urlencoded],
            "multipart": [item.to_dict() for item in self.multipart],
            "file": self.file.to_dict() if self.file is not None else None,
            "suspended_kind": self.suspended_kind,
            "form_raw_b64": _encoded(self.form_raw),
            "multipart_raw_b64": _encoded(self.multipart_raw),
            "multipart_content_type": self.multipart_content_type,
        }

    @staticmethod
    def from_dict(data: Any) -> SavedBody:
        if not isinstance(data, dict):
            raise ValueError("body must be an object")  # noqa: TRY004
        kind = data.get("kind", "none")
        if not isinstance(kind, str) or kind not in BODY_KINDS:
            raise ValueError(f"unsupported body kind: {kind!r}")
        suspended_kind = data.get("suspended_kind", "none")
        if not isinstance(suspended_kind, str) or suspended_kind not in BODY_KINDS:
            raise ValueError(f"unsupported suspended body kind: {suspended_kind!r}")
        text = data.get("text", "")
        raw_b64 = data.get("raw_b64", "")
        if not isinstance(text, str) or not isinstance(raw_b64, str):
            raise ValueError("body text/raw_b64 must be strings")  # noqa: TRY004
        _bytes(raw_b64, "body raw_b64")
        multipart = data.get("multipart", [])
        if not isinstance(multipart, list):
            raise ValueError("body multipart must be a list")  # noqa: TRY004
        raw_file = data.get("file")
        content_type = data.get("multipart_content_type", "")
        if not isinstance(content_type, str):
            raise ValueError("multipart Content-Type must be a string")  # noqa: TRY004
        return SavedBody(
            kind=kind,
            text=text,
            raw_b64=raw_b64,
            urlencoded=_pairs(data.get("urlencoded", []), "body urlencoded"),
            multipart=tuple(SavedFormField.from_dict(item) for item in multipart),
            file=SavedFormField.from_dict(raw_file) if raw_file is not None else None,
            suspended_kind=suspended_kind,
            form_raw=_bytes(data.get("form_raw_b64"), "body form_raw"),
            multipart_raw=_bytes(data.get("multipart_raw_b64"), "body multipart_raw"),
            multipart_content_type=content_type,
        )


@dataclass(frozen=True)
class SavedWebSocket:
    """Connection options and editable message drafts, never a live session.

    Keep unfinished hex input so saving a draft does not require sending it.
    Text and binary documents survive switching the message editor's mode.
    """

    subprotocols: tuple[str, ...] = ()
    kind: str = "text"
    text: str = ""
    binary_hex: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subprotocols": list(self.subprotocols),
            "kind": self.kind,
            "text": self.text,
            "binary_hex": self.binary_hex,
        }

    @staticmethod
    def from_dict(data: Any) -> SavedWebSocket:
        if not isinstance(data, dict):
            raise ValueError("websocket must be an object")  # noqa: TRY004
        protocols = data.get("subprotocols", [])
        if not isinstance(protocols, list) or not all(
            isinstance(item, str) for item in protocols
        ):
            raise ValueError("websocket subprotocols must be a list of strings")
        kind = data.get("kind", "text")
        if not isinstance(kind, str) or kind not in ("text", "json", "binary"):
            raise ValueError("unsupported websocket message kind")
        text = data.get("text", "")
        binary_hex = data.get("binary_hex", "")
        if not isinstance(text, str) or not isinstance(binary_hex, str):
            raise ValueError("websocket drafts must be strings")  # noqa: TRY004
        return SavedWebSocket(tuple(protocols), kind, text, binary_hex)


@dataclass(frozen=True)
class SavedRequest:
    """一条已保存请求：列表寻址以 ``id`` 为唯一依据，名称可重复。

    ``url`` contains the complete original query string. ``params`` is its
    editable representation, never a reason to re-encode an untouched URL.
    Header ownership uses row positions in this snapshot, never Qt row IDs.
    """

    id: str = ""
    name: str = ""
    folder: str = ""  # 单层文件夹；"" = 未分组
    method: str = "GET"
    url: str = ""
    params: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    managed_headers: tuple[int, ...] = ()
    body: SavedBody = field(default_factory=SavedBody)
    record: bool = True  # 「记录流量」勾选随草稿保存
    updated_at: float = 0.0
    protocol: str = "http"
    websocket: SavedWebSocket = field(default_factory=SavedWebSocket)

    @staticmethod
    def create(
        *,
        name: str,
        method: str,
        url: str,
        params: tuple[tuple[str, str], ...] = (),
        headers: tuple[tuple[str, str], ...] = (),
        managed_headers: tuple[int, ...] = (),
        body: SavedBody | None = None,
        record: bool = True,
        folder: str = "",
        protocol: str = "http",
        websocket: SavedWebSocket | None = None,
    ) -> SavedRequest:
        return SavedRequest(
            id=uuid.uuid4().hex,
            name=name,
            folder=folder,
            method=method,
            url=url,
            params=params,
            headers=headers,
            managed_headers=managed_headers,
            body=body if body is not None else SavedBody(),
            record=record,
            updated_at=time.time(),
            protocol=protocol,
            websocket=websocket if websocket is not None else SavedWebSocket(),
        )

    def touched(self, **changes: Any) -> SavedRequest:
        """原地语义的不变更新：改字段同时刷新 ``updated_at``，id 不动。"""
        return replace(self, **changes, updated_at=time.time())

    @property
    def display_method(self) -> str:
        return "WS" if self.protocol == "websocket" else self.method

    def summary(self) -> str:
        """保存对话框里的只读摘要，如 ``POST /api/orders``。"""
        try:
            path = urlsplit(self.url if "://" in self.url else "//" + self.url).path
        except ValueError:
            path = self.url
        path = path or "/"
        return f"{self.display_method} {path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "folder": self.folder,
            "method": self.method,
            "url": self.url,
            "params": [list(pair) for pair in self.params],
            "headers": [list(pair) for pair in self.headers],
            "managed_headers": list(self.managed_headers),
            "body": self.body.to_dict(),
            "record": self.record,
            "updated_at": self.updated_at,
            "protocol": self.protocol,
            "websocket": self.websocket.to_dict(),
        }

    @staticmethod
    def from_dict(data: Any) -> SavedRequest:
        if not isinstance(data, dict):
            raise ValueError("request must be an object")  # noqa: TRY004
        request_id = data.get("id")
        name = data.get("name")
        method = data.get("method")
        url = data.get("url")
        protocol = data.get("protocol", "http")
        if not isinstance(protocol, str) or protocol not in ("http", "websocket"):
            raise ValueError("unsupported request protocol")
        if not all(
            isinstance(part, str) and part for part in (request_id, name, method)
        ):
            raise ValueError("request id/name/method must be non-empty strings")
        if not isinstance(url, str):
            raise ValueError("request url must be a string")  # noqa: TRY004
        folder = data.get("folder", "")
        if not isinstance(folder, str):
            raise ValueError("request folder must be a string")  # noqa: TRY004
        record = data.get("record", True)
        if not isinstance(record, bool):
            raise ValueError("request record must be a bool")  # noqa: TRY004
        updated_at = data.get("updated_at", 0.0)
        if isinstance(updated_at, bool) or not isinstance(updated_at, int | float):
            raise ValueError("request updated_at must be a number")  # noqa: TRY004
        try:
            updated_at = float(updated_at)
        except (OverflowError, ValueError) as exc:
            raise ValueError("request updated_at must be finite") from exc
        if not math.isfinite(updated_at):
            raise ValueError("request updated_at must be finite")
        headers = _pairs(data.get("headers", []), "request headers")
        managed = data.get("managed_headers", [])
        if not isinstance(managed, list) or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(headers)
            for index in managed
        ):
            raise ValueError("managed headers must be valid row positions")
        if len(set(managed)) != len(managed):
            raise ValueError("managed header positions must be unique")
        return SavedRequest(
            id=request_id,
            name=name,
            folder=folder,
            method=method,
            url=url,
            params=_pairs(data.get("params", []), "request params"),
            headers=headers,
            managed_headers=tuple(managed),
            body=SavedBody.from_dict(data.get("body", {})),
            record=record,
            updated_at=float(updated_at),
            protocol=protocol,
            websocket=SavedWebSocket.from_dict(data.get("websocket", {})),
        )


def duplicate(request: SavedRequest, *, name: str) -> SavedRequest:
    """复制副本：新 id、新时间，其余内容原样。"""
    return replace(request, id=uuid.uuid4().hex, name=name, updated_at=time.time())
