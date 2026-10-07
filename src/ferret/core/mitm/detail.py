"""详情面板的纯数据快照，所有活 flow 读取都由 facade 放到 mitm 线程。

选中行只构建 summary；请求体、响应体、消息分别在对应页打开时读取。
这样未打开的 Body 页不解压或美化，WebSocket 概览也不序列化整份帧列表。
``build_flow_detail`` 为导出菜单和 Compose 保留完整详情接口。

交付的容器不带活引用；body / 消息内容的不可变 bytes 可以直接跨线程交付。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from PySide6.QtCore import QCoreApplication

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    FLOW_FORMAT_VERSION,
    HTTPFlow,
    Request,
    Response,
    assemble_request_head,
    assemble_response_head,
)
from ferret.core.mitm.export import FlowExporter
from ferret.core.mitm.sse import (
    SSE_EVENT_COUNT_KEY,
    SseEvent,
    is_event_stream,
    parse_sse,
)
from ferret.core.mitm.wsframe import WsClose, ws_close, ws_frames

# `utils/http_parser.py` 反过来引 `core/mitm/bindings`（AGENTS.md §4 记着这是误引，
# 别扩散）。这一条不构成真环：`bindings.py` 不从 ferret 里 import 任何东西，
# 所以无论谁先被加载都能走通。
from ferret.utils.http_parser import _safe_text, build_body

log = get_logger("mitm.detail")

# 证书主体/签发者里要读的 OID 短名 → 详情字典的键名后缀。
# `certs.Cert.subject` / `.issuer` 返回 ``[(短名, 值)]``，短名就是 OID 的通用缩写。
# 用普通 `#` 而不是 `#:` —— lupdate 把 `#:` 当 extracomment 标记，会把这段注释挂到
# 下一个 translate() 调用的译员提示上（那是 "Pending..."，和证书毫无关系）。
_CERT_NAME_PARTS: tuple[tuple[str, str], ...] = (
    ("CN", "Common Name"),
    ("C", "Country"),
    ("ST", "State"),
    ("L", "Locality"),
    ("O", "Organization"),
    ("OU", "Organizational Unit"),
)

# 证书有效期的显示格式，逐字沿用改造前的写法（末尾那个 ``.000`` 也是原样）。
_CERT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.000"


def flatten_multi(items) -> dict[str, str]:
    """把 mitmproxy 的多值视图压成 ``{key: value}``，重复键用 ", " 连接。

    ``dict(MultiDictView)`` 只会保留最后一个同名键，会静默丢数据，
    因此必须走 ``items(multi=True)``。
    """
    grouped: dict[str, list[str]] = {}
    for key, value in items:
        grouped.setdefault(key, []).append(value)
    return {k: v[0] if len(v) == 1 else ", ".join(v) for k, v in grouped.items()}


def decode_bytes(value: object) -> str:
    """ALPN 之类的 `bytes` 字段解成字符串；非 bytes 原样 `str()`。

    用 ``errors="replace"`` 而不是裸 `.decode()` —— 对端给什么都可能，
    一个畸形 ALPN 值不该让整个详情面板打不开。
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def head_size(message: Request | Response) -> int:
    """报文头的**真实线上字节数**。

    别写成 ``len(str(message.headers))`` —— 那量的是 `multidict.__repr__` 出来的
    Python repr（``Headers[(b'host', b'example.com')]``），既含引号和 ``b`` 前缀、
    又缺请求行/状态行，和线上字节没有任何关系，一条普通请求能虚报一两倍。
    这里走原生 `assemble_request_head` / `assemble_response_head`，拿到的是
    「请求行（状态行）+ 头 + 空行」的完整字节。

    HTTP/2 及以上没有 http1 线格式（头部走 HPACK 压缩），此时这个数字是
    **等效 HTTP/1 头部字节**，不是真正传输的字节数。
    """
    try:
        if isinstance(message, Response):
            return len(assemble_response_head(message))
        return len(assemble_request_head(message))
    except (AttributeError, TypeError, ValueError) as e:
        log.warning("failed to assemble the message head: %s", e)
        return 0


def wire_size(message: Request | Response | None) -> int:
    """报文体的线上字节数（**压缩后**），也就是表格 Size 列的口径。

    表格 Size 列和详情面板的「线上」行都走这里，两处数字才不会互相打脸：
    详情面板早先量的是 `get_content()`（**解压后**），同一条 gzip 响应
    和表格能差出好几倍，两个数字谁也说不清自己在说什么。
    「解压后」是另一个口径，由 `build_body()` 的解码结果单独量。
    """
    if message is None:
        return 0
    return len(message.raw_content or b"")


def tls_fields(conn, prefix: str) -> dict[str, Any]:
    """一侧连接的 TLS 六项；没握手成功就一个键都不产出。

    服务端沿用历来的 ``TLS`` 前缀，客户端用 ``Client TLS`` —— 详情面板早先只显示
    服务端一侧，客户端握手用了哪个版本、哪个套件、有没有走 ALPN 全都看不到。
    """
    if not getattr(conn, "tls_established", False):
        return {}
    return {
        f"{prefix} Version": conn.tls_version or "",
        f"{prefix} SNI": conn.sni or "",
        f"{prefix} ALPN Offers": [decode_bytes(v) for v in conn.alpn_offers or ()],
        f"{prefix} ALPN Selected": decode_bytes(conn.alpn) if conn.alpn else "",
        f"{prefix} Cipher": conn.cipher or "",
        f"{prefix} Cipher List": list(conn.cipher_list or ()),
    }


def _via_spec(via) -> str:
    """上游代理 spec → 可读字符串。

    `server_spec.ServerSpec` 是 ``tuple[scheme, (host, port)]`` 的**类型别名**，
    不是 NamedTuple —— 写 `via.scheme` 会直接 `AttributeError`。所以这里按元组解，
    形状不对就退回 `str()`，绝不让一个上游配置把详情面板整个搞崩。
    """
    try:
        scheme, (host, port) = via
        return f"{scheme}://{host}:{port}"
    except (TypeError, ValueError):
        return str(via)


def connection_fields(conn, prefix: str) -> dict[str, Any]:
    """一侧连接的状态与时间戳。前端连接用 ``Front``，后端用 ``Back``。

    和 `tls_fields` 分开是因为这些字段和 TLS 无关：连接状态、传输协议、连接出错
    原因、以及三个握手/结束时刻。它们历来一个都没产出 —— 「这条连接现在还开着吗」
    「走的是 TCP 还是 UDP」「连接是什么时候断的」在界面上完全看不到。

    时间戳原样产出**裸浮点**，格式化留给 `fields.py` 的 `fmt=format_time`：
    那边同一个函数管着所有时刻字段，`None` / `0` 也在那一层统一按「没有」处理。

    `timestamp_tcp_setup` / `via` / `address` 只在服务端连接上有，用 `getattr`
    统一取 —— 一个函数管两侧，比两个近乎一样的函数好维护。
    """
    state = getattr(conn, "state", None)
    fields: dict[str, Any] = {
        f"{prefix} Connection State": getattr(state, "name", "").lower(),
        f"{prefix} Transport Protocol": conn.transport_protocol or "",
        # 连接建立的起点：`Server.timestamp_start` 对域名是「开始 DNS 解析」、对
        # IP 是「发出 TCP SYN」（上游 docstring 原话）——「DNS + 连接」段的分子
        # 全靠它，两侧都原样产出（Server 可能是 None，交给显示层）。
        f"{prefix} Connection Start": conn.timestamp_start,
        f"{prefix} TLS Handshake": conn.timestamp_tls_setup,
        f"{prefix} Connection End": conn.timestamp_end,
    }
    tcp_setup = getattr(conn, "timestamp_tcp_setup", None)
    if tcp_setup:
        fields[f"{prefix} TCP Handshake"] = tcp_setup
    if conn.error:
        fields[f"{prefix} Connection Error"] = conn.error
    # `via` / `address` 只读服务端：`Client.address` 在 mitmproxy 12 是 `peername`
    # 的**废弃别名**，一读就抛 DeprecationWarning，而且值和 `Front Client Address`
    # 完全重复。`via` 是「有没有上游代理」，只有 `Server` 有，正好当判据。
    if hasattr(conn, "via"):
        if conn.via:
            fields[f"{prefix} Via"] = _via_spec(conn.via)
        # 服务端的 `address` 是**请求的**目标（可能是域名），`peername` 是解析后的
        # ip:port —— 两个是不同的东西，走 CDN 或改了 host 时差别就出来了。
        if conn.address:
            fields[f"{prefix} Address"] = f"{conn.address[0]}:{conn.address[1]}"
    return fields


def certificate_fields(conn) -> dict[str, Any]:
    """服务端证书链 → 详情面板的证书字段；拿不到证书就一个键都不产出。

    「证书组渲染出 12 行字面 ``-``」是两处凑出来的：models 从来只写
    ``Not Before`` / ``Not After``，而界面那侧空值也硬占一行。这里补齐产出，
    `fields.py` 那侧去掉 `always` —— 缺哪项就少哪行，整组都没有就整组不显示。

    空值不写进字典（而不是写成 ``""``）是刻意的：详情字段的语义是「没有这个键
    就不显示这一行」，写个空串等于把判断推给每个渲染器各自实现一遍。
    """
    chain = list(getattr(conn, "certificate_list", None) or [])
    if not chain:
        return {}
    fields: dict[str, Any] = {}
    # 链级信息先落袋：任何一张证书解析失败都不该把它一起拖下水。
    fields["Certificate Chain Depth"] = len(chain)
    chain_names = [entry.cn for entry in chain if getattr(entry, "cn", None)]
    if chain_names:
        fields["Certificate Chain"] = chain_names
    fields.update(_leaf_certificate_fields(chain[0]))
    for index, entry in enumerate(chain[1:], start=1):
        fields.update(_chain_certificate_fields(index, entry))
    return fields


def _leaf_certificate_fields(cert) -> dict[str, Any]:
    """叶证书全量字段。键名一个不改 —— 现有测试和 fields.py 声明钉着它们。"""
    fields: dict[str, Any] = {}
    try:
        for prefix, pairs in (("Subject", cert.subject), ("Issuer", cert.issuer)):
            names: dict[str, str] = {}
            for short_name, value in pairs:
                # 同一个短名可以出现多次（多值 OU），这一行是给人扫的，取第一个够用。
                names.setdefault(short_name, value)
            for short_name, suffix in _CERT_NAME_PARTS:
                if names.get(short_name):
                    fields[f"{prefix} {suffix}"] = names[short_name]
        fields["Not Before"] = cert.notbefore.strftime(_CERT_TIME_FORMAT)
        fields["Not After"] = cert.notafter.strftime(_CERT_TIME_FORMAT)
        # `Cert.fingerprint()` 给的是 SHA-256（32 字节）。键名照实写 —— 早先这里叫
        # `Fingerprint SHA1`，但因为从来没有值，名字错了也没人发现。
        fields["Fingerprint SHA256"] = cert.fingerprint().hex(":").upper()
        fields["Serial Number Hex"] = format(cert.serial, "X")
        fields["Certificate Expired"] = "true" if cert.has_expired() else "false"
        fields["Certificate Is CA"] = "true" if cert.is_ca else "false"
        algorithm, bits = cert.keyinfo
        fields["Certificate Key"] = f"{algorithm} {bits}"
        altnames = [str(getattr(name, "value", name)) for name in cert.altnames]
        if altnames:
            fields["Certificate Alt Names"] = altnames
    except (AttributeError, TypeError, ValueError) as e:
        # 证书是对端给的，畸形字段不该让整个详情面板打不开。
        log.warning("failed to read the server certificate: %s", e)
    return fields


def _chain_certificate_fields(index: int, cert) -> dict[str, Any]:
    """中间/根证书的判别性 5 项 —— 定位「哪一环过期 / 指纹错 / 信任链断在哪」。

    不做全量：链可深到几十层，全量会让证书卡失控难扫（docs/design.md#tls）。
    每张独立 try/except，坏一张不塌整链。
    """
    fields: dict[str, Any] = {}
    prefix = f"Chain[{index}]"
    try:
        for label, pairs in (("Subject", cert.subject), ("Issuer", cert.issuer)):
            names: dict[str, str] = {}
            for short_name, value in pairs:
                names.setdefault(short_name, value)
            if names.get("CN"):
                fields[f"{prefix} {label} CN"] = names["CN"]
        fields[f"{prefix} Not Before"] = cert.notbefore.strftime(_CERT_TIME_FORMAT)
        fields[f"{prefix} Not After"] = cert.notafter.strftime(_CERT_TIME_FORMAT)
        fields[f"{prefix} Fingerprint SHA256"] = cert.fingerprint().hex(":").upper()
    except (AttributeError, TypeError, ValueError) as e:
        log.warning("failed to read chain certificate %d: %s", index, e)
    return fields


def response_cookies(response: Response) -> list[dict[str, Any]]:
    """响应 Cookie，连属性一起产出。

    `Response.cookies` 的值是 ``(value, attrs)``，早先只留了 ``(name, value)``，
    Path/Domain/Max-Age/Expires/HttpOnly/Secure/SameSite 全丢了。而且 Set-Cookie
    允许同名重复，压成字典会把后一条覆盖掉，所以这里是 `list[dict]` 不是字典。

    HttpOnly/Secure 这类标志属性没有值（`CookieAttrs` 给 `None`），统一归成空串，
    否则 `flatten_multi` 拼接时会撞上 `None`。
    """
    cookies: list[dict[str, Any]] = []
    for name, (value, attrs) in response.cookies.items(multi=True):
        cookies.append(
            {
                "name": name,
                "value": value,
                "attrs": flatten_multi(
                    (key, "" if attr is None else attr)
                    for key, attr in attrs.items(multi=True)
                ),
            }
        )
    return cookies


def request_fields(flow: HTTPFlow) -> dict[str, Any]:
    """请求一侧不依赖 body 的详情字段。

    这里不做状态判断：每条流量都有请求，而改造前那个
    ``state in ("request_headers", "request", "response_headers", "complete", "error")``
    把 `infer_state()` 可能返回的状态全列上了，等于恒真。
    """
    request = flow.request
    keep_alive = request.headers.get("keep-alive", None)
    if keep_alive is None:
        keep_alive = "true" if request.http_version == "HTTP/1.1" else "false"

    headers_size = head_size(request)
    wire = wire_size(request)

    req_duration = None
    if request.timestamp_end and request.timestamp_start:
        req_duration = (request.timestamp_end - request.timestamp_start) * 1000

    client_conn = flow.client_conn
    peername = client_conn.peername if client_conn else None
    sockname = getattr(client_conn, "sockname", None) if client_conn else None

    fields: dict[str, Any] = {
        "Method": request.method,
        "URL": request.pretty_url,
        "Host": request.host,
        "Path": request.path,
        "Scheme": request.scheme,
        "Authority": request.authority,
        "HTTP Version": request.http_version,
        "Request Headers": dict(request.headers),
        "Request Params": flatten_multi(request.query.items(multi=True)),
        "Request Cookies": flatten_multi(request.cookies.items(multi=True)),
        "Request Content-Type": request.headers.get("Content-Type", "-"),
        "Status Code": QCoreApplication.translate("FlowDetail", "等待中..."),
        "Keep Alive": keep_alive,
        # `Flow ID` 是新键。改造前叫 `Connection ID`，存的却是 `flow.id` —— 名字
        # 占着连接的位置，真正的 `Connection.id` 反而无处可放。
        "Flow ID": flow.id,
        "Front Client Address": peername[0] if peername else "N/A",
        "Front Client Port": peername[1] if peername else "N/A",
        "Front Server Address": sockname[0] if sockname else "N/A",
        "Front Server Port": sockname[1] if sockname else "N/A",
        "req_time": request.timestamp_start,
        "req_timestamp_end": request.timestamp_end,
        "req_duration": req_duration,
        "req_headers_size": headers_size,
        "req_wire_size": wire,
        "req_total_size": headers_size + wire,
    }
    if client_conn is not None:
        fields["Connection ID"] = client_conn.id
        fields.update(connection_fields(client_conn, "Front"))
        fields.update(tls_fields(client_conn, "Client TLS"))
        if client_conn.mitmcert is not None:
            fields["Client Mitm Certificate"] = client_conn.mitmcert.cn or ""
        if client_conn.proxy_mode is not None:
            fields["Client Proxy Mode"] = client_conn.proxy_mode.full_spec
    if request.trailers:
        fields["Request Trailers"] = flatten_multi(request.trailers.items(multi=True))
    return fields


def response_fields(flow: HTTPFlow, response: Response) -> dict[str, Any]:
    """响应一侧不依赖 body 的详情字段。

    改造前这些字段分在两个分支里：一个判 ``("response_headers", "complete",
    "error")``，一个判 ``("complete", "error")``，两个都还要再 `and flow.response`。
    `infer_state()` 只会给出 ``request`` / ``complete`` / ``error`` 三种，
    ``complete`` 必然有响应、``request`` 必然没有、``error`` 两种都可能 ——
    所以两个条件都等价于「有响应」，合成一个分支后逐字等价。
    """
    headers_size = head_size(response)
    wire = wire_size(response)

    res_duration = None
    if response.timestamp_end and response.timestamp_start:
        res_duration = (response.timestamp_end - response.timestamp_start) * 1000

    # 总耗时与表格 Time 列（`FlowTableModel._duration_ms`）同一条减法、同一个
    # None 判据，两处才必然一致。产出**裸毫秒**：原来这里直接格式化好字符串，
    # 是同字典里唯一例外，而且 `(timestamp_end or 0) - start` 在响应未收完时
    # 算出 -9.4e11 ms —— 响应存在但没收完（SSE/流式）就是 `None`，不是 0。
    duration_ms = None
    if response.timestamp_end is not None and flow.request.timestamp_start is not None:
        duration_ms = max(
            0.0, (response.timestamp_end - flow.request.timestamp_start) * 1000
        )

    server_conn = flow.server_conn
    peername = server_conn.peername if server_conn else None
    sockname = getattr(server_conn, "sockname", None) if server_conn else None
    server_addr = f"{peername[0]}:{peername[1]}" if peername else "N/A"

    protocol = flow.request.http_version
    if server_conn and server_conn.alpn:
        protocol = decode_bytes(server_conn.alpn)

    proxy_protocol = "http"
    if server_conn and getattr(server_conn, "tls_established", False):
        proxy_protocol = "https"

    fields: dict[str, Any] = {
        "Status Code": response.status_code,
        "Reason": response.reason,
        "Response Headers": dict(response.headers),
        "Response Cookies": response_cookies(response),
        "Response HTTP Version": response.http_version,
        "Response Content-Type": response.headers.get("Content-Type", "-"),
        # 「线上 3.1 KB / 解压后 12 KB」旁边总得说清是谁压的，否则那两个数字看着像 bug。
        "Response Content-Encoding": response.headers.get("Content-Encoding", ""),
        "Server Address": server_addr,
        "Protocol": protocol,
        "Proxy Protocol": proxy_protocol,
        "duration_ms": duration_ms,
        # `source_address` 在 mitmproxy 12 的 `Connection` 上已经不存在了，
        # 这四行历来全是 "N/A"；本机出口地址现在从 `sockname` 读。
        "Back Client Address": sockname[0] if sockname else "N/A",
        "Back Client Port": sockname[1] if sockname else "N/A",
        "Back Server Address": peername[0] if peername else "N/A",
        "Back Server Port": peername[1] if peername else "N/A",
        "res_time": response.timestamp_end,
        "res_timestamp_start": response.timestamp_start,
        "res_duration": res_duration,
        "res_headers_size": headers_size,
        "res_wire_size": wire,
        "res_total_size": headers_size + wire,
    }
    if server_conn is not None:
        fields["Back Connection ID"] = server_conn.id
        fields.update(connection_fields(server_conn, "Back"))
        fields.update(tls_fields(server_conn, "TLS"))
        fields.update(certificate_fields(server_conn))
    if response.trailers:
        fields["Response Trailers"] = flatten_multi(response.trailers.items(multi=True))
    return fields


def infer_state(flow: HTTPFlow) -> str:
    """流量走到哪一步了。只有这三种 —— 详情面板的 State 行按它取文案。"""
    if flow.error:
        return "error"
    if flow.response:
        return "complete"
    return "request"


def build_flow_summary(flow: HTTPFlow) -> dict[str, Any]:
    """详情页的轻量字段；不解 body、不序列化 flow / WebSocket 消息。

    活 flow 只在 mitm 线程读取，会话页可直接读取自己持有的离线 flow。
    ``Modified`` 在有 backup 时需要原生状态比较，留给概览按需补齐。
    """
    state = infer_state(flow)
    websocket = flow.websocket
    sse = flow.response is not None and is_event_stream(
        flow.response.headers.get("content-type")
    )
    data: dict[str, Any] = {
        "id": flow.id,
        "state": state,
        "comment": flow.comment,
        "marked": flow.marked,
        "is_replay": flow.is_replay or "",
        "live": "true" if flow.live else "false",
        "Flow Type": flow.type,
        "Flow Version": FLOW_FORMAT_VERSION,
        "Flow Created": flow.timestamp_created,
        "Intercepted": "true" if flow.intercepted else "false",
        "Flow Metadata": deepcopy(flow.metadata),
        "is_websocket": websocket is not None,
        "message_kind": "websocket" if websocket is not None else "sse" if sse else "",
        "message_count": (
            len(websocket.messages)
            if websocket is not None
            else flow.metadata.get(SSE_EVENT_COUNT_KEY)
            if sse
            else 0
        ),
    }
    if not flow._backup:
        data["Modified"] = "false"
    else:
        data["overview_pending"] = True
    data.update(request_fields(flow))

    response = flow.response
    if response is not None:
        data.update(response_fields(flow, response))

    # 合计无条件算：只抓到请求的流量早先会显示「请求 384b / 合计 0b」，
    # 两个数字自己打自己。
    data["total_size"] = data.get("req_total_size", 0) + data.get("res_total_size", 0)

    if state == "error":
        data["Status Code"] = "Error"
        data["Error Message"] = flow.error.msg if flow.error else "Unknown"
        if flow.error is not None:
            data["Error Time"] = flow.error.timestamp

    return data


def build_flow_body(
    flow: HTTPFlow, side: Literal["Request", "Response"]
) -> dict[str, Any]:
    """只为一侧 Body 页解码、美化；不触碰另一侧报文或 WebSocket 帧。"""
    if side not in ("Request", "Response"):
        raise ValueError(f"Unknown HTTP message side: {side}")
    message = flow.request if side == "Request" else flow.response
    if message is None:
        return {}
    body = build_body(flow, message)
    fields = {
        f"{side} Body": body["raw"],
        f"{side} Body Text": body["text"],
        f"{side} Body Pretty": body["pretty"],
        f"{side} Body View": body["view"],
        f"{side} Body Syntax": body["syntax"],
        f"{side} Content-Type": message.headers.get("Content-Type", "-"),
        "req_decoded_size" if side == "Request" else "res_decoded_size": len(
            body["raw"]
        ),
    }
    if side == "Request":
        # 查询串不需要解 body；urlencoded 表单与 Body 一起按需读取。
        # multipart 仍由原生 contentview 显示，避免另拷整份上传文件。
        form = flatten_multi(flow.request.urlencoded_form.items(multi=True))
        if form:
            fields["Request Form"] = form
    return fields


def build_flow_overview_metadata(flow: HTTPFlow) -> dict[str, Any]:
    """概览按需读取原生修改状态；有 backup 才会进行完整状态比较。"""
    return {"Modified": "true" if flow.modified() else "false"}


def build_flow_messages(
    flow: HTTPFlow | None, events: list[SseEvent] | None = None
) -> dict[str, Any]:
    """消息页的一次原子快照，事件/帧 index 可用于忽略已在途的重复信号。

    ``events=None`` 表示没有实时存档，才从历史 SSE body 解码；空列表则是
    已接通但尚无事件的流式响应，不能因此重复解析 body。
    """
    data: dict[str, Any] = {
        "kind": "",
        "count": 0,
        "frames": [],
        "close": WsClose(),
        "events": [],
    }
    if flow is None:
        return data
    if flow.websocket is not None:
        frames = ws_frames(flow.websocket)
        data.update(
            kind="websocket",
            count=len(frames),
            frames=frames,
            close=ws_close(flow.websocket),
        )
    elif flow.response is not None and is_event_stream(
        flow.response.headers.get("content-type")
    ):
        if events is None:
            events = parse_sse(_safe_text(flow.response))
        data.update(
            kind="sse",
            count=max(
                events[-1].index + 1 if events else 0,
                flow.metadata.get(SSE_EVENT_COUNT_KEY, 0),
            ),
            events=events,
        )
    return data


def build_flow_detail(flow: HTTPFlow) -> dict[str, Any]:
    """兼容导出 / Compose 的完整详情；界面选择行应使用轻量 summary。"""
    data = build_flow_summary(flow)
    data.update(build_flow_body(flow, "Request"))
    data.update(build_flow_body(flow, "Response"))
    data.update(build_flow_overview_metadata(flow))
    data.pop("overview_pending", None)
    # 完整原生状态只由明确请求完整详情的调用方承担，不进入选中行的热路径。
    data["raw_state"] = flow.get_state()
    if data["state"] == "complete":
        try:
            data["curl_command"] = FlowExporter.curl_command(flow)
        except Exception as e:  # noqa: BLE001
            log.warning("curl command generation failed: %s", e)
            data["curl_command"] = f"Error generating curl command: {e}"

    return data
