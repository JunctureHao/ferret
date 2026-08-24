from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from qfluentwidgets import isDarkTheme

from ferret.core.log import get_logger
from ferret.core.mitm import (
    GATEWAY_METADATA_KEY,
    SUSPEND_POLICIES,
    FlowExporter,
    GatewayPolicy,
    HTTPFlow,
    Request,
    Response,
    assemble_request_head,
    assemble_response_head,
    human,
)
from ferret.utils.http_parser import build_body
from ferret.utils.i18n import QT_TRANSLATE_NOOP

log = get_logger("flow")

METHOD_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 2
FULL_URL_ROLE = int(Qt.ItemDataRole.UserRole) + 3
MIME_ROLE = int(Qt.ItemDataRole.UserRole) + 4
DURATION_MS_ROLE = int(Qt.ItemDataRole.UserRole) + 5
SIZE_BYTES_ROLE = int(Qt.ItemDataRole.UserRole) + 6
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 7

# 网关往 flow.metadata 里写的是策略名（`str(GatewayPolicy)`）。这里只认字符串、
# 不导 apps/gateway —— apps/common 不该认识具体页面。
_SUSPEND_MARKS: frozenset[str] = frozenset(str(policy) for policy in SUSPEND_POLICIES)

# 文案在这里只做标记、不求值 —— 模块级求值赶在翻译器安装之前（`core/application.py`
# 顶层就 import 了 MainWindow），译文会永久冻结成英文。求值在 `gateway_note()` 里做。
_GATEWAY_TOOLTIPS: dict[str, str] = {
    str(GatewayPolicy.BLOCK): QT_TRANSLATE_NOOP(
        "FlowTableModel", "blocked by the gateway"
    ),
    str(GatewayPolicy.BLOCK_OUT): QT_TRANSLATE_NOOP(
        "FlowTableModel", "blocked by the gateway: the request never left"
    ),
    str(GatewayPolicy.BLOCK_IN): QT_TRANSLATE_NOOP(
        "FlowTableModel",
        "blocked by the gateway: the response never reached the client",
    ),
    str(GatewayPolicy.SUSPEND_OUT): QT_TRANSLATE_NOOP(
        "FlowTableModel", "suspended by the gateway: the request has not been sent"
    ),
    str(GatewayPolicy.SUSPEND_IN): QT_TRANSLATE_NOOP(
        "FlowTableModel",
        "suspended by the gateway: the response is not being forwarded",
    ),
}


def is_suspended(flow: HTTPFlow) -> bool:
    """这条流量此刻是否停着不动 —— 网关挂起或断点拦下都算。

    两个来源都要认：网关放行时 addon 会把 metadata 标记摘掉，断点则由原生
    `flow.intercepted` 表示（`resume()` 会清成 False），所以两边都是实时状态。
    断点还会拦网关挂起以外的流量，只看 metadata 会让「拦截队列」里明明钉着的那条
    在流量表里显示成「等待中」。
    """
    if flow.intercepted:
        return True
    return flow.metadata.get(GATEWAY_METADATA_KEY) in _SUSPEND_MARKS


def gateway_note(flow: HTTPFlow) -> str:
    """Status 列的悬浮补充说明；没被网关或断点动过就是空串。

    返回的是**不带括号**的短句，加括号由调用点负责 —— 中文用全角括号、英文用半角，
    早先把括号写进文案里，单独显示时还得 `strip("（）")` 把它抠掉，换个语言就漏。
    """
    translate = QCoreApplication.translate
    policy = flow.metadata.get(GATEWAY_METADATA_KEY)
    if policy:
        # 网关的挂起标记比断点更具体（能说清是请求还是响应停住了），优先用它。
        note = _GATEWAY_TOOLTIPS.get(policy)
        if note is None:
            return translate("FlowTableModel", "handled by the gateway")
        return translate("FlowTableModel", note)
    if flow.intercepted:
        # 断点不写 metadata，只能问原生状态。
        return translate("FlowTableModel", "held at a breakpoint, waiting for you")
    # blocklisted 是原生 BlockList addon 的标记。网关已经取代了它，只有从旧会话
    # 文件读回来的 flow 才会带（metadata 随 flow 一起存档）。
    if flow.metadata.get("blocklisted"):
        return translate("FlowTableModel", "blocked by a blocklist rule")
    return ""


def flatten_multi(items) -> dict[str, str]:
    """把 mitmproxy 的多值视图压成 ``{key: value}``，重复键用 ", " 连接。

    ``dict(MultiDictView)`` 只会保留最后一个同名键，会静默丢数据，
    因此必须走 ``items(multi=True)``。
    """
    grouped: dict[str, list[str]] = {}
    for key, value in items:
        grouped.setdefault(key, []).append(value)
    return {k: v[0] if len(v) == 1 else ", ".join(v) for k, v in grouped.items()}


def format_duration(duration_ms: float | None) -> str:
    if duration_ms is None:
        return ""
    if duration_ms < 1:
        return "< 1 ms"
    if duration_ms < 1000:
        return f"{duration_ms:.0f} ms"
    return f"{duration_ms / 1000:.2f} s"


#: 证书主体/签发者里要读的 OID 短名 → 详情字典的键名后缀。
#: `certs.Cert.subject` / `.issuer` 返回 ``[(短名, 值)]``，短名就是 OID 的通用缩写。
_CERT_NAME_PARTS: tuple[tuple[str, str], ...] = (
    ("CN", "Common Name"),
    ("C", "Country"),
    ("ST", "State"),
    ("L", "Locality"),
    ("O", "Organization"),
    ("OU", "Organizational Unit"),
)

#: 证书有效期的显示格式，逐字沿用改造前的写法（末尾那个 ``.000`` 也是原样）。
_CERT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.000"


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
    把 `FlowTableModel.__infer_state()` 可能返回的状态全列上了，等于恒真。
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
        "Status Code": QCoreApplication.translate("FlowTableModel", "Pending..."),
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
        fields.update(tls_fields(client_conn, "Client TLS"))
        if client_conn.mitmcert is not None:
            fields["Client Mitm Certificate"] = client_conn.mitmcert.cn or ""
        if client_conn.proxy_mode is not None:
            fields["Client Proxy Mode"] = client_conn.proxy_mode.full_spec
    if request.trailers:
        fields["Request Trailers"] = flatten_multi(request.trailers.items(multi=True))
    return fields


def response_fields(flow: HTTPFlow, response: Response) -> dict[str, Any]:
    """响应一侧的详情字段。

    改造前这些字段分在两个分支里：一个判 ``("response_headers", "complete",
    "error")``，一个判 ``("complete", "error")``，两个都还要再 `and flow.response`。
    `__infer_state()` 只会给出 ``request`` / ``complete`` / ``error`` 三种，
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
        fields.update(tls_fields(server_conn, "TLS"))
        fields.update(certificate_fields(server_conn))
    if response.trailers:
        fields["Response Trailers"] = flatten_multi(response.trailers.items(multi=True))
    return fields


class FlowTableModel(QAbstractTableModel):
    HEADERS = ("#", "Method", "URL", "Status", "Type", "Size", "Time")

    def __init__(self, parent: QObject, view=None):
        super().__init__(parent)
        self._headers = list(self.HEADERS)
        self.view = view
        # 稳定行号列表：model 自己的"行号→flow"映射，不依赖 View 的 SortedList
        # 排序位置（并发重排会导致插入声明位置与取数位置失配 → 空行/错数据）。
        # View 仅作为 flow 存储/过滤后端，行号由此列表自治。
        self._rows: list[HTTPFlow] = []

    def set_view(self, view):
        """设置 mitmproxy View 实例并重置模型"""
        self.beginResetModel()
        self.view = view
        self._rows = list(view) if view else []
        self.endResetModel()

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self._headers[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                # 横向表头统一左对齐（垂直居中），不按列名区分。
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._rows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._headers)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not self._rows or not index.isValid():
            return None

        row = index.row()
        col = index.column()
        if not (0 <= row < len(self._rows)):
            return None

        flow = self._rows[row]
        column_name = self._headers[col]

        if not isinstance(flow, HTTPFlow):
            if role == Qt.ItemDataRole.DisplayRole:
                if column_name == "#":
                    return row + 1
                if column_name == "Method":
                    return type(flow).__name__.replace("Flow", "").upper()
                return "—"
            return None

        if role == Qt.ItemDataRole.DisplayRole:
            if column_name == "#":
                return row + 1
            if column_name == "Method":
                return flow.request.method
            if column_name == "URL":
                return flow.request.pretty_url
            if column_name == "Status":
                # 挂起优先于响应码：挂起（入）时响应已经回来了，但客户端一个字节
                # 都没拿到，显示 200 会骗人。真实码进悬浮提示。
                if is_suspended(flow):
                    return self.tr("Suspended")
                if flow.error:
                    return "Error"
                if flow.response is None:
                    return self.tr("Pending")
                return flow.response.status_code
            if column_name == "Type":
                return self._mime_label(self._mime(flow))
            if column_name == "Size":
                return human.pretty_size(self._size_bytes(flow))
            if column_name == "Time":
                return format_duration(self._duration_ms(flow))
            return ""

        if role == SORT_ROLE:
            if column_name == "#":
                return row + 1
            if column_name == "Method":
                return flow.request.method.upper()
            if column_name == "URL":
                return flow.request.pretty_url.lower()
            if column_name == "Status":
                if is_suspended(flow):
                    return -1
                if flow.error:
                    return 600
                return flow.response.status_code if flow.response else -1
            if column_name == "Type":
                return self._mime(flow).lower()
            if column_name == "Size":
                return self._size_bytes(flow)
            if column_name == "Time":
                duration = self._duration_ms(flow)
                return duration if duration is not None else -1.0

        if role == METHOD_ROLE:
            return flow.request.method.upper()
        if role == STATUS_KIND_ROLE:
            return self._status_kind(flow)
        if role == FULL_URL_ROLE:
            return flow.request.pretty_url
        if role == MIME_ROLE:
            return self._mime(flow)
        if role == DURATION_MS_ROLE:
            return self._duration_ms(flow)
        if role == SIZE_BYTES_ROLE:
            return self._size_bytes(flow)

        if role == Qt.ItemDataRole.ToolTipRole:
            if column_name == "URL":
                return flow.request.pretty_url
            if column_name == "Status":
                note = gateway_note(flow)
                suffix = f" ({note})" if note else ""
                if flow.error:
                    msg = flow.error.msg if flow.error else "Flow error"
                    return f"{msg}{suffix}"
                if flow.response:
                    status = f"{flow.response.status_code} {flow.response.reason}"
                    return f"{status}{suffix}"
                if note:
                    return note
            if column_name == "Type":
                return self._mime(flow) or self.tr("Unknown content type")
            if column_name == "Size":
                return self._size_tooltip(flow)
            if column_name == "Time":
                return self._time_tooltip(flow)

        if role == Qt.ItemDataRole.ForegroundRole:
            if column_name == "Method":
                return self._semantic_color(self._method_kind(flow.request.method))
            if column_name == "Status":
                return self._semantic_color(self._status_kind(flow))

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if column_name in ("#", "Status", "Size", "Time"):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if column_name == "Method":
                return int(Qt.AlignmentFlag.AlignCenter)

        return None

    @staticmethod
    def _host(flow: HTTPFlow) -> str:
        return getattr(flow.request, "pretty_host", None) or flow.request.host

    @classmethod
    def _host_with_port(cls, flow: HTTPFlow) -> str:
        host = cls._host(flow)
        port = getattr(flow.request, "port", None)
        return f"{host}:{port}" if port else host

    @staticmethod
    def _mime(flow: HTTPFlow) -> str:
        value = ""
        if flow.response is not None:
            value = flow.response.headers.get("Content-Type", "")
        if not value:
            value = flow.request.headers.get("Content-Type", "")
        return value.split(";", 1)[0].strip()

    @staticmethod
    def _mime_label(mime: str) -> str:
        value = mime.lower()
        if not value:
            return "—"
        if "json" in value:
            return "JSON"
        if "html" in value:
            return "HTML"
        if "xml" in value:
            return "XML"
        if "javascript" in value:
            return "JS"
        if "css" in value:
            return "CSS"
        if value.startswith("image/"):
            return value.split("/", 1)[1].upper()
        if value.startswith("text/"):
            return "Text"
        if "form" in value:
            return "Form"
        return value.split("/", 1)[-1].upper()

    @staticmethod
    def _size_bytes(flow: HTTPFlow) -> int:
        """Size 列的字节数 —— 请求体 + 响应体的**线上**字节（压缩后）。

        和详情面板的「线上」行同走 `wire_size()`，两处数字才必然一致。
        """
        return wire_size(flow.request) + wire_size(flow.response)

    @staticmethod
    def _duration_ms(flow: HTTPFlow) -> float | None:
        if flow.response is None or flow.request.timestamp_start is None:
            return None
        end = flow.response.timestamp_end
        if end is None:
            return None
        return max(0.0, (end - flow.request.timestamp_start) * 1000)

    @staticmethod
    def _method_kind(method: str) -> str:
        value = method.upper()
        if value == "GET":
            return "success"
        if value == "POST":
            return "info"
        if value in ("PUT", "PATCH"):
            return "warning"
        if value == "DELETE":
            return "error"
        return "neutral"

    @staticmethod
    def _status_kind(flow: HTTPFlow) -> str:
        if is_suspended(flow):
            return "pending"
        if flow.error:
            return "error"
        if flow.response is None:
            return "pending"
        code = flow.response.status_code
        if 200 <= code < 300:
            return "success"
        if 300 <= code < 400:
            return "info"
        if 400 <= code < 500:
            return "warning"
        if code >= 500:
            return "error"
        return "neutral"

    @staticmethod
    def _semantic_color(kind: str) -> QColor:
        dark = isDarkTheme()
        colors = {
            "success": "#62c174" if dark else "#22863a",
            "info": "#6ea8fe" if dark else "#1769aa",
            "warning": "#e5b64b" if dark else "#a15c00",
            "error": "#ff7b72" if dark else "#c62828",
            "pending": "#9a9a9a" if dark else "#6b6b6b",
            "neutral": "#b0b0b0" if dark else "#555555",
        }
        return QColor(colors.get(kind, colors["neutral"]))

    @staticmethod
    def _size_tooltip(flow: HTTPFlow) -> str:
        """Size 列的口径说明 —— 这一列量的是**线上**字节，压缩后。

        列宽只放得下一个总数，而「384b」到底是压缩前还是压缩后，差一个 gzip 就差
        好几倍。拆成请求/响应两行摆出来，再点明口径，读的人才不用猜；详情面板的
        「解压后」是另一行，两处口径在 `wire_size()` 上是同一个函数。
        """
        translate = QCoreApplication.translate
        request = translate("FlowTableModel", "Request")
        response = translate("FlowTableModel", "Response")
        note = translate("FlowTableModel", "Body bytes on the wire (compressed)")
        return "\n".join(
            (
                f"{request}: {human.pretty_size(wire_size(flow.request))}",
                f"{response}: {human.pretty_size(wire_size(flow.response))}",
                note,
            )
        )

    @classmethod
    def _time_tooltip(cls, flow: HTTPFlow) -> str:
        start = flow.request.timestamp_start
        end = flow.response.timestamp_end if flow.response else None
        start_text = (
            datetime.fromtimestamp(start, tz=UTC)
            .astimezone()
            .isoformat(timespec="milliseconds")
            if start
            else "—"
        )
        end_text = (
            datetime.fromtimestamp(end, tz=UTC)
            .astimezone()
            .isoformat(timespec="milliseconds")
            if end
            else "—"
        )
        # 标签单独取：lupdate 的 Python 解析器不往 f-string 里看，写成
        # f"{translate(...)}: …" 这三条就一条都提不出来（实测填译文时才发现）。
        translate = QCoreApplication.translate
        started = translate("FlowTableModel", "Started")
        ended = translate("FlowTableModel", "Ended")
        elapsed = translate("FlowTableModel", "Elapsed")
        duration_text = format_duration(cls._duration_ms(flow)) or "—"
        return "\n".join(
            (
                f"{started}: {start_text}",
                f"{ended}: {end_text}",
                f"{elapsed}: {duration_text}",
            )
        )

    # ------------------------------------------------------------------
    # 数据变化处理（由 View 桥接信号驱动）
    # ------------------------------------------------------------------
    def _row_of(self, flow: HTTPFlow) -> int:
        """在稳定行号列表中查找 flow 的索引（不依赖 View 排序位置）"""
        try:
            return self._rows.index(flow)
        except ValueError:
            return -1

    def handle_add(self, flow: HTTPFlow) -> None:
        """处理 View 新增 flow：追加到末尾，行号由 _rows 自治"""
        if not self.view:
            return
        if flow in self._rows:
            return  # 防重复
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append(flow)
        self.endInsertRows()

    def handle_update(self, flow: HTTPFlow) -> None:
        """处理 View 更新 flow"""
        row = self._row_of(flow)
        if row < 0:
            return
        start_idx = self.index(row, 0)
        end_idx = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(start_idx, end_idx)

    def handle_remove(self, flow: HTTPFlow, index: int) -> None:
        """处理 View 移除 flow：按 flow 反查 _rows 下标，避免 View 源索引错位"""
        row = self._row_of(flow)
        if row < 0:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._rows.pop(row)
        self.endRemoveRows()

    def handle_refresh(self) -> None:
        """处理 View 整体刷新：同步重建 _rows"""
        self.beginResetModel()
        self._rows = list(self.view) if self.view else []
        self.endResetModel()

    # ------------------------------------------------------------------
    # 数据访问
    # ------------------------------------------------------------------
    def clear_data(self):
        """清空表格内容"""
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()
        if self.view:
            self.view.clear()

    def get_row_data(self, row: int) -> dict[str, Any]:
        """根据行号获取该行的完整展示字典（供详情面板使用）"""
        if 0 <= row < len(self._rows):
            return self._build_row_data(self._rows[row])
        return {}

    def _build_row_data(self, flow: HTTPFlow) -> dict[str, Any]:
        """以 mitmproxy 原生 ``flow.get_state()`` 字典为基础，叠加 ferret 详情面板
        所需的加工字段（参数/ Cookie/ 证书/ 双向 TLS/ trailers/ pretty body/ curl
        等），就地构造展示字典。下游消费的字段名保持稳定。

        分支只按「有没有响应」分，不再按 `__infer_state()` 的状态字符串分 ——
        它只会返回 ``request`` / ``complete`` / ``error``，改造前那两条
        ``request_headers`` / ``response_headers`` 分支永远进不去。
        """
        state = self.__infer_state(flow)
        data: dict[str, Any] = dict(flow.get_state())
        data["id"] = flow.id
        data["state"] = state
        data.update(request_fields(flow))

        response = flow.response
        if response is not None:
            data.update(response_fields(flow, response))

        # 合计无条件算：只抓到请求的流量早先会显示「请求 384b / 合计 0b」，
        # 两个数字自己打自己。
        data["total_size"] = data.get("req_total_size", 0) + data.get(
            "res_total_size", 0
        )

        if state == "error":
            data.update(
                {
                    "Status Code": "Error",
                    "Error Message": flow.error.msg if flow.error else "Unknown",
                }
            )

        if state == "complete":
            try:
                data["curl_command"] = FlowExporter.curl_command(flow)
            except Exception as e:  # noqa: BLE001
                log.warning("curl command generation failed: %s", e)
                data["curl_command"] = f"Error generating curl command: {e}"

        return data

    @staticmethod
    def __infer_state(flow: HTTPFlow) -> str:
        if flow.error:
            return "error"
        if flow.response:
            return "complete"
        return "request"

    def get_flow(self, row: int) -> HTTPFlow | None:
        """根据行号获取原始 HTTPFlow"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def remove_row(self, row: int):
        """删除指定行"""
        if not self.view or not (0 <= row < len(self._rows)):
            return
        flow = self._rows[row]
        self.view.remove([flow])


class FlowProxyModel(QSortFilterProxyModel):
    """排序代理（透明过滤）。

    搜索/协议/状态码/内容类型等过滤已统一下沉到 mitmproxy 的 ``View.set_filter``，
    由 flowfilter 表达式表达。因此本代理
    **不再做任何行级过滤**，只负责表格排序。这样：
    * 过滤不触发 _build_row_data() 的详情解析（性能）；
    * 过滤只影响 View 可见列表（_view），_store 保留全部流量（无清除效果）。
    """

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        # 透明：保留源模型所有行（过滤已由 View.set_filter 完成）
        return True
