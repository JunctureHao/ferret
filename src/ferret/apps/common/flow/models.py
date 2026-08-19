from datetime import UTC, datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor
from qfluentwidgets import isDarkTheme

from ferret.core.mitm import FlowExporter, HTTPFlow, human
from ferret.utils.http_parser import build_body

METHOD_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 2
FULL_URL_ROLE = int(Qt.ItemDataRole.UserRole) + 3
MIME_ROLE = int(Qt.ItemDataRole.UserRole) + 4
DURATION_MS_ROLE = int(Qt.ItemDataRole.UserRole) + 5
SIZE_BYTES_ROLE = int(Qt.ItemDataRole.UserRole) + 6
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 7


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
                if flow.error:
                    return "Error"
                if flow.response is None:
                    return "等待中"
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
                if flow.error:
                    return flow.error.msg if flow.error else "Flow error"
                if flow.response:
                    return f"{flow.response.status_code} {flow.response.reason}"
            if column_name == "Type":
                return self._mime(flow) or "未知内容类型"
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
        request_body = flow.request.raw_content or b""
        response_body = flow.response.raw_content if flow.response else b""
        return len(request_body) + len(response_body or b"")

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
        return f"开始：{start_text}\n结束：{end_text}\n耗时：{format_duration(cls._duration_ms(flow)) or '—'}"

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
        """以 mitmproxy 原生 ``flow.get_state()`` 字典为基础，叠加 ferret
        详情面板所需的加工字段（参数/ Cookie/ 进程/ 客户端地址/ pretty body/
        curl 等），就地构造展示字典。下游消费的字段名保持稳定。"""
        state = self.__infer_state(flow)
        data: dict[str, Any] = dict(flow.get_state())
        data["id"] = flow.id
        data["state"] = state

        if state in (
            "request_headers",
            "request",
            "response_headers",
            "complete",
            "error",
        ):
            keep_alive = flow.request.headers.get("keep-alive", None)
            if keep_alive is None and flow.request.http_version == "HTTP/1.1":
                keep_alive = "true"
            elif keep_alive is None:
                keep_alive = "false"

            client_pn = flow.client_conn.peername if flow.client_conn else None
            client_sn = (
                getattr(flow.client_conn, "sockname", None)
                if flow.client_conn
                else None
            )
            conn_time = ""
            if flow.request.timestamp_start:
                conn_time = (
                    datetime.fromtimestamp(flow.request.timestamp_start, tz=UTC)
                    .astimezone()
                    .strftime("%Y-%m-%d %H:%M:%S.%f")
                )

            data.update(
                {
                    "Method": flow.request.method,
                    "URL": flow.request.pretty_url,
                    "Host": flow.request.host,
                    "Path": flow.request.path,
                    "Scheme": flow.request.scheme,
                    "HTTP Version": flow.request.http_version,
                    "Request Headers": dict(flow.request.headers),
                    "req_time": flow.request.timestamp_start,
                    "req_timestamp_end": flow.request.timestamp_end,
                    "req_headers_size": len(str(flow.request.headers)),
                    "Status Code": "等待中...",
                    "Keep Alive": keep_alive,
                    "Connection ID": flow.id,
                    "Connection Time": conn_time,
                    "Front Client Address": client_pn[0] if client_pn else "N/A",
                    "Front Client Port": client_pn[1] if client_pn else "N/A",
                    "Front Server Address": client_sn[0] if client_sn else "N/A",
                    "Front Server Port": client_sn[1] if client_sn else "N/A",
                }
            )

            data["Request Params"] = flatten_multi(flow.request.query.items(multi=True))
            data["Request Cookies"] = flatten_multi(
                flow.request.cookies.items(multi=True)
            )

        if state in ("request", "response_headers", "complete", "error"):
            req_body_info = build_body(flow, flow.request)
            body = req_body_info["raw"]
            req_duration = None
            if flow.request.timestamp_end and flow.request.timestamp_start:
                req_duration = (
                    flow.request.timestamp_end - flow.request.timestamp_start
                ) * 1000
            req_ct = flow.request.headers.get("Content-Type", "-")
            data.update(
                {
                    "req_size": len(body),
                    "req_duration": req_duration,
                    "Request Body": body,
                    "Request Content-Type": req_ct,
                    "Request Body Text": req_body_info["text"],
                    "Request Body Pretty": req_body_info["pretty"],
                    "Request Body View": req_body_info["view"],
                    "Request Body Syntax": req_body_info["syntax"],
                }
            )

        if state in ("response_headers", "complete", "error") and flow.response:
            # Set-Cookie 有独立语法（属性 + 可重复），必须用 Response.cookies；
            # 按 Cookie 头语法拆会把 Path/HttpOnly 当成 cookie 并丢掉后续条目。
            data["Response Cookies"] = flatten_multi(
                (name, value)
                for name, (value, _attrs) in flow.response.cookies.items(multi=True)
            )

            server_addr = "N/A"
            if flow.server_conn and flow.server_conn.peername:
                server_addr = (
                    f"{flow.server_conn.peername[0]}:{flow.server_conn.peername[1]}"
                )

            protocol = flow.request.http_version
            if flow.server_conn and flow.server_conn.alpn:
                protocol = flow.server_conn.alpn.decode()

            proxy_protocol = "http"
            if (
                flow.server_conn
                and hasattr(flow.server_conn, "tls_established")
                and flow.server_conn.tls_established
            ):
                proxy_protocol = "https"

            server_pn = flow.server_conn.peername if flow.server_conn else None
            server_sn = (
                getattr(flow.server_conn, "source_address", None)
                if flow.server_conn
                else None
            )

            data.update(
                {
                    "Status Code": flow.response.status_code,
                    "Reason": flow.response.reason,
                    "Response Headers": dict(flow.response.headers),
                    "Response HTTP Version": flow.response.http_version,
                    "Server Address": server_addr,
                    "Protocol": protocol,
                    "res_headers_size": len(str(flow.response.headers)),
                    "res_timestamp_start": flow.response.timestamp_start,
                    "Proxy Protocol": proxy_protocol,
                    "Back Client Address": server_sn[0] if server_sn else "N/A",
                    "Back Client Port": server_sn[1] if server_sn else "N/A",
                    "Back Server Address": server_pn[0] if server_pn else "N/A",
                    "Back Server Port": server_pn[1] if server_pn else "N/A",
                }
            )

            conn = flow.server_conn
            if conn and getattr(conn, "tls_established", False):
                tls_info = {
                    "TLS Version": getattr(conn, "tls_version", "N/A"),
                    "TLS SNI": getattr(conn, "sni", "N/A"),
                    "TLS ALPN Offers": [
                        a.decode() if isinstance(a, bytes) else str(a)
                        for a in getattr(conn, "alpn_offers", []) or []
                    ],
                    "TLS ALPN Selected": (conn.alpn.decode() if conn.alpn else "N/A"),
                    "TLS Cipher": getattr(conn, "cipher", "N/A"),
                    "TLS Cipher List": list(getattr(conn, "cipher_list", []) or []),
                }
                if hasattr(conn, "certificate_list") and conn.certificate_list:
                    server_cert = conn.certificate_list[0]
                    if server_cert:
                        tls_info["Not Before"] = server_cert.notbefore.strftime(
                            "%Y-%m-%d %H:%M:%S.000"
                        )
                        tls_info["Not After"] = server_cert.notafter.strftime(
                            "%Y-%m-%d %H:%M:%S.000"
                        )
                data.update(tls_info)

        if state in ("complete", "error") and flow.response:
            duration = (flow.response.timestamp_end or 0) - (
                flow.request.timestamp_start or 0
            )
            res_duration = None
            if flow.response.timestamp_end and flow.response.timestamp_start:
                res_duration = (
                    flow.response.timestamp_end - flow.response.timestamp_start
                ) * 1000
            res_body_info = build_body(flow, flow.response)
            body = res_body_info["raw"]
            req_total_size = data.get("req_headers_size", 0) + data.get("req_size", 0)
            res_total_size = data.get("res_headers_size", 0) + len(body)
            total_size = req_total_size + res_total_size
            res_ct = flow.response.headers.get("Content-Type", "-")
            data.update(
                {
                    "Response Body": body,
                    "Response Content-Type": res_ct,
                    "Response Body Text": res_body_info["text"],
                    "Response Body Pretty": res_body_info["pretty"],
                    "Response Body View": res_body_info["view"],
                    "Response Body Syntax": res_body_info["syntax"],
                    "res_size": len(body),
                    "res_time": flow.response.timestamp_end,
                    "res_duration": res_duration,
                    "Duration": f"{duration * 1000:.0f} ms",
                    "total_size": total_size,
                    "TLS Version": getattr(flow.server_conn, "tls_version", "N/A")
                    if flow.server_conn
                    else "N/A",
                }
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
                print(f"生成 cURL 命令失败: {e}")
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
