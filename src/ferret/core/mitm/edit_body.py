"""Body availability and protocol constraints for held-message editing.

纯数据模块：不依赖 QtWidgets、不产生面向界面的异常文案（AGENTS.md §4）。
`inspect_edit_body` 只读报文，不改活对象；`response_body_allowed` 是 UI 与
写回共用的无正文响应判定（docs/.plans intercept-body-kinds.md §7）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ferret.core.mitm.bindings import Request, Response, encoding


@dataclass(frozen=True)
class BodySource:
    """Editing inputs of one message body; unavailable is distinct from empty.

    ``content`` 是 `get_content(strict=False)` 口径的容错解码结果：解不开时是
    **原压缩字节**，`decode_ok=False` 就是为此存在的。``available=False`` 表示
    ``raw_content is None``（未取得/已流出），不能当合法空正文。
    """

    content: bytes | None
    available: bool
    decode_ok: bool


def inspect_edit_body(message: Request | Response) -> BodySource:
    """Read one message body for editing, without mutating the live object.

    空 raw（``b""``）恒为 ``decode_ok=True``：原生 ``Message.decode()`` 对空体是
    no-op，空体不是解码失败。缺失（``None``）恒为 ``available=False``，content
    一并 None —— 缺失不是空。
    """
    raw = message.raw_content
    if raw is None:
        return BodySource(content=None, available=False, decode_ok=True)
    # 与原生 get_content 同一套判定：无声明或空体，线上字节就是解码结果。
    ce = message.headers.get("content-encoding")
    if not raw or not ce:
        return BodySource(content=raw, available=True, decode_ok=True)
    try:
        content = encoding.decode(raw, ce)
    except ValueError:
        return BodySource(content=raw, available=True, decode_ok=False)
    if not isinstance(content, bytes):
        # 客户端非法声明的 byte→str 编码（如 utf8）：get_content 的同一道闸。
        return BodySource(content=raw, available=True, decode_ok=False)
    return BodySource(content=content, available=True, decode_ok=True)


def response_body_allowed(request_method: str, status_code: int) -> bool:
    """Whether a response with this method/status may carry a body.

    判据是**最终**状态码 + 请求方法。HEAD 请求本身不受此限（只有响应）。
    205 在原生 ``expected_http_body_size`` 里不返回 0，但协议明确禁止正文，
    这里按协议来。
    """
    method = request_method.strip().upper()
    if method == "HEAD":
        return False
    if 100 <= status_code < 200 or status_code in (204, 205, 304):
        return False
    return not (method == "CONNECT" and 200 <= status_code < 300)
