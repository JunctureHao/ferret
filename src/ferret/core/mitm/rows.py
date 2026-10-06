"""跨线程行快照：Qt 表格只认 ``FlowRow``，不认内核线程的活 flow（#90）。

mitm 线程在**发信号前**把 flow 折成这份不可变标量集（mitmweb ``flow_to_json``
的同款位置：序列化发生在内核侧，前端只见过某一时刻的一致快照）。Qt 侧对行内容
的一切读取都发生在自己的线程、自己的数据上——「只读自觉」换成结构保证。

字段清单＝两个表格模型（平铺 + 连接树）、右键菜单与详情字典寻址所需的并集；
只进标量，body 与可变消息结构一律不进（按需走 ``get_*_body`` / ``flow_detail``
的 id 化通道）。构建是 O(小标量) 的，每条变更路径在既有信号点上多花一次折叠，
不新增跨线程往返。
"""

from __future__ import annotations

from dataclasses import dataclass

from ferret.core.mitm.bindings import Flow, HTTPFlow
from ferret.core.mitm.detail import wire_size
from ferret.core.mitm.gateway import GATEWAY_METADATA_KEY


@dataclass(frozen=True, slots=True)
class FlowRow:
    """一条流量在某一时刻的展示快照（不可变）。

    ``is_http=False`` 的行来自 TCP/UDP/DNS 等非 HTTP 流量（会话页从文件读回的
    旧会话可能带）；HTTP 专属字段（method/url/status…）置空值，渲染侧按
    ``type_label`` 分流，与旧 ``flow_cell`` 对非 HTTPFlow 的分支同形。
    """

    id: str
    is_http: bool
    # 非 HTTP 流量的类型短标（"TCP"/"UDP"/"DNS"）；HTTP 行为空串。
    type_label: str
    # 客户端物理连接 id（连接树分组键）；老会话缺 client_conn.id 时回落兜底组。
    conn_id: str
    marked: str
    method: str
    url: str
    host: str
    port: int | None
    req_mime: str
    resp_mime: str
    status_code: int | None
    reason: str | None
    error_msg: str | None
    intercepted: bool
    gateway_policy: str | None
    blocklisted: bool
    has_response: bool
    req_start: float | None
    resp_end: float | None
    req_wire: int
    resp_wire: int
    timestamp_created: float | None
    client_peername: str
    client_alpn: str
    client_tls: str
    client_sni: str
    client_cipher: str


# 与 models.py 的 _UNKNOWN_CONN_ID 同一个兜底值：老会话文件、非常规通道可能缺
# client_conn.id，统一归入兜底组，不崩不丢流。
UNKNOWN_CONN_ID = "__ferret_unknown_conn__"


def _peername(client_conn) -> str:
    peer = getattr(client_conn, "peername", None) if client_conn is not None else None
    if not peer:
        return ""
    try:
        return f"{peer[0]}:{peer[1]}"
    except (IndexError, TypeError):
        return str(peer)


def _alpn(client_conn) -> str:
    alpn = getattr(client_conn, "alpn", None) if client_conn is not None else None
    if not alpn:
        return ""
    try:
        return bytes(alpn).decode("ascii", "replace")
    except (UnicodeDecodeError, TypeError):
        return str(alpn)


def _header_mime(headers, key: str) -> str:
    if headers is None:
        return ""
    return (headers.get(key, "") or "").split(";", 1)[0].strip()


def flow_row(flow: Flow) -> FlowRow:
    """把 flow 折成不可变行快照。**必须在拥有该 flow 的线程上调用**——内核侧
    的信号发射点（桥接/重发收口）在 mitm 线程调它；会话页那批死 flow 在 GUI
    线程调它同样安全。"""
    client_conn = getattr(flow, "client_conn", None)
    metadata = getattr(flow, "metadata", None) or {}
    is_http = isinstance(flow, HTTPFlow)
    request = flow.request if is_http else None
    response = flow.response if is_http else None
    error = flow.error
    return FlowRow(
        id=flow.id,
        is_http=is_http,
        type_label="" if is_http else type(flow).__name__.replace("Flow", "").upper(),
        conn_id=getattr(client_conn, "id", None) or UNKNOWN_CONN_ID,
        marked=flow.marked or "",
        method=request.method if request is not None else "",
        url=request.pretty_url if request is not None else "",
        host=(getattr(request, "pretty_host", None) or request.host)
        if request is not None
        else "",
        port=getattr(request, "port", None) if request is not None else None,
        req_mime=_header_mime(
            request.headers if request is not None else None, "Content-Type"
        ),
        resp_mime=_header_mime(
            response.headers if response is not None else None, "Content-Type"
        ),
        status_code=response.status_code if response is not None else None,
        reason=response.reason if response is not None else None,
        error_msg=error.msg if error is not None else None,
        intercepted=bool(flow.intercepted),
        gateway_policy=metadata.get(GATEWAY_METADATA_KEY),
        blocklisted=bool(metadata.get("blocklisted")),
        has_response=response is not None,
        req_start=request.timestamp_start if request is not None else None,
        resp_end=response.timestamp_end if response is not None else None,
        req_wire=wire_size(request) if request is not None else 0,
        resp_wire=wire_size(response) if response is not None else 0,
        timestamp_created=flow.timestamp_created,
        client_peername=_peername(client_conn),
        client_alpn=_alpn(client_conn),
        client_tls=str(getattr(client_conn, "tls_version", None) or ""),
        client_sni=str(getattr(client_conn, "sni", None) or ""),
        client_cipher=str(getattr(client_conn, "cipher", None) or ""),
    )
