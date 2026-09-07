"""详情面板的数据产出：一条 `HTTPFlow` → 一个纯数据字典。

这个模块存在的理由是**线程**，不是分层洁癖。改造前这套逻辑长在
`apps/common/flow/models.py::FlowTableModel._build_row_data` 里，每次选中行都在
**Qt 线程**上直接读活 flow —— AGENTS.md §3 记着的红线违规。而且它还顺手做了
body 美化（上限 1 MiB）和证书解析，卡的是界面线程。

搬到 `core/mitm/` 之后，`MitmFacade.flow_detail()` 用 `runtime.call` 把整个构建
过程放进 mitm 线程内完成，界面拿到的是 str/int/bytes/list/dict 组成的纯数据。
副作用是构建期间 mitm 的 event loop 被占住 —— 但那是一次几十毫秒的解析，
而且 body 美化本来就有 1 MiB 上限；换到的是界面不再直读活 flow。

`bytes` 会原样穿过线程边界（`Request Body` / `Response Body` 是 `bytes`）——
它不可变，界面只读，不需要再拷一层。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import QCoreApplication

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    HTTPFlow,
    Request,
    Response,
    assemble_request_head,
    assemble_response_head,
)
from ferret.core.mitm.export import FlowExporter

# `utils/http_parser.py` 反过来引 `core/mitm/bindings`（AGENTS.md §4 记着这是误引，
# 别扩散）。这一条不构成真环：`bindings.py` 不从 ferret 里 import 任何东西，
# 所以无论谁先被加载都能走通。
from ferret.utils.http_parser import build_body

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
    cert = chain[0]
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
        fields["Certificate Chain Depth"] = len(chain)
        chain_names = [entry.cn for entry in chain if entry.cn]
        if chain_names:
            fields["Certificate Chain"] = chain_names
    except (AttributeError, TypeError, ValueError) as e:
        # 证书是对端给的，畸形字段不该让整个详情面板打不开。
        log.warning("failed to read the server certificate: %s", e)
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
    """请求一侧的详情字段。

    这里不做状态判断：每条流量都有请求，而改造前那个
    ``state in ("request_headers", "request", "response_headers", "complete", "error")``
    把 `infer_state()` 可能返回的状态全列上了，等于恒真。
    """
    request = flow.request
    keep_alive = request.headers.get("keep-alive", None)
    if keep_alive is None:
        keep_alive = "true" if request.http_version == "HTTP/1.1" else "false"

    body_info = build_body(flow, request)
    body = body_info["raw"]
    headers_size = head_size(request)
    wire = wire_size(request)

    req_duration = None
    if request.timestamp_end and request.timestamp_start:
        req_duration = (request.timestamp_end - request.timestamp_start) * 1000

    conn_time = ""
    if request.timestamp_start:
        conn_time = (
            datetime.fromtimestamp(request.timestamp_start, tz=UTC)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M:%S.%f")
        )

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
        "Request Body": body,
        "Request Content-Type": request.headers.get("Content-Type", "-"),
        "Request Body Text": body_info["text"],
        "Request Body Pretty": body_info["pretty"],
        "Request Body View": body_info["view"],
        "Request Body Syntax": body_info["syntax"],
        "Status Code": QCoreApplication.translate("FlowDetail", "等待中..."),
        "Keep Alive": keep_alive,
        # `Flow ID` 是新键。改造前叫 `Connection ID`，存的却是 `flow.id` —— 名字
        # 占着连接的位置，真正的 `Connection.id` 反而无处可放。
        "Flow ID": flow.id,
        "Connection Time": conn_time,
        "Front Client Address": peername[0] if peername else "N/A",
        "Front Client Port": peername[1] if peername else "N/A",
        "Front Server Address": sockname[0] if sockname else "N/A",
        "Front Server Port": sockname[1] if sockname else "N/A",
        "req_time": request.timestamp_start,
        "req_timestamp_end": request.timestamp_end,
        "req_duration": req_duration,
        "req_headers_size": headers_size,
        "req_wire_size": wire,
        "req_decoded_size": len(body),
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
    # urlencoded 表单单独产出一份：`Request Params` 是 URL 上的查询串，表单在 body 里，
    # 两个都被叫做「参数」但来源完全不同，混在一页里看根本分不清哪个是哪个。
    # multipart 刻意不产出 —— 它的键和值都是 `bytes`，值还可能是整个上传文件，
    # 而 Body 页的 Multipart Form 视图本来就把它排得更清楚。
    form = flatten_multi(request.urlencoded_form.items(multi=True))
    if form:
        fields["Request Form"] = form
    if request.trailers:
        fields["Request Trailers"] = flatten_multi(request.trailers.items(multi=True))
    return fields


def response_fields(flow: HTTPFlow, response: Response) -> dict[str, Any]:
    """响应一侧的详情字段。

    改造前这些字段分在两个分支里：一个判 ``("response_headers", "complete",
    "error")``，一个判 ``("complete", "error")``，两个都还要再 `and flow.response`。
    `infer_state()` 只会给出 ``request`` / ``complete`` / ``error`` 三种，
    ``complete`` 必然有响应、``request`` 必然没有、``error`` 两种都可能 ——
    所以两个条件都等价于「有响应」，合成一个分支后逐字等价。
    """
    body_info = build_body(flow, response)
    body = body_info["raw"]
    headers_size = head_size(response)
    wire = wire_size(response)

    res_duration = None
    if response.timestamp_end and response.timestamp_start:
        res_duration = (response.timestamp_end - response.timestamp_start) * 1000
    duration = (response.timestamp_end or 0) - (flow.request.timestamp_start or 0)

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
        "Response Body": body,
        "Response Content-Type": response.headers.get("Content-Type", "-"),
        # 「线上 3.1 KB / 解压后 12 KB」旁边总得说清是谁压的，否则那两个数字看着像 bug。
        "Response Content-Encoding": response.headers.get("Content-Encoding", ""),
        "Response Body Text": body_info["text"],
        "Response Body Pretty": body_info["pretty"],
        "Response Body View": body_info["view"],
        "Response Body Syntax": body_info["syntax"],
        "Server Address": server_addr,
        "Protocol": protocol,
        "Proxy Protocol": proxy_protocol,
        "Duration": f"{duration * 1000:.0f} ms",
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
        "res_decoded_size": len(body),
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


def build_flow_detail(flow: HTTPFlow) -> dict[str, Any]:
    """一条流量 → 详情面板的完整字典。**只在 mitm 线程内调用**。

    改造前这里是 `dict(flow.get_state())` 打底再往上叠加工字段，等于把原生的
    小写键（``id`` / ``type`` / ``version`` / ``client_conn`` / ``request`` …）
    和面板自己的 CamelCase 键混在同一层命名空间里，谁盖谁全看叠加顺序。现在
    原生状态整体挪进 `raw_state` 一个键 —— 「原始状态」页正好要的就是它，
    面板需要的几项（`id` / `comment` / `marked` …）单独显式产出。

    分支只按「有没有响应」分，不再按状态字符串分：`infer_state()` 只会返回
    ``request`` / ``complete`` / ``error``，改造前那两条 ``request_headers`` /
    ``response_headers`` 分支永远进不去。
    """
    state = infer_state(flow)
    raw_state = flow.get_state()
    data: dict[str, Any] = {
        "id": flow.id,
        "state": state,
        "comment": flow.comment,
        "marked": flow.marked,
        "is_replay": flow.is_replay or "",
        "live": "true" if flow.live else "false",
        # 「流量元数据」卡要的几项。`version` 只在原生状态里有（`Flow` 上没有同名
        # 属性），所以从 `raw_state` 读；`modified()` 是**方法**不是属性。
        "Flow Type": flow.type,
        "Flow Version": raw_state.get("version"),
        "Flow Created": flow.timestamp_created,
        "Intercepted": "true" if flow.intercepted else "false",
        "Modified": "true" if flow.modified() else "false",
        "Flow Metadata": dict(flow.metadata),
        # 完整性兜底：以后新增的 flow 字段不改一行代码就已经在这棵子树里。
        "raw_state": raw_state,
    }
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

    if state == "complete":
        try:
            data["curl_command"] = FlowExporter.curl_command(flow)
        except Exception as e:  # noqa: BLE001
            log.warning("curl command generation failed: %s", e)
            data["curl_command"] = f"Error generating curl command: {e}"

    return data
