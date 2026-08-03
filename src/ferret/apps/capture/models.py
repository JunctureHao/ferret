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

from ferret.apps.capture.services import FlowExporter, HTTPFlow, _safe_content
from ferret.utils.http_parser import (
    build_body,
    parse_cookies_from_headers,
    parse_params,
)
from ferret.utils.process_resolver import resolve_process


class PacketTableModel(QAbstractTableModel):
    def __init__(self, parent: QObject, view=None):
        super().__init__(parent)
        self._headers = ["ID", "Method", "URL", "Status Code", "Duration", ""]
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
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return self._headers[section]
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

        if role == Qt.ItemDataRole.DisplayRole:
            if column_name == "ID":
                return row + 1
            # 非 HTTP 流（TCPFlow/UDPFlow/DNSFlow）没有 .request，统一降级展示，
            # 避免 AttributeError 在 Qt 重绘时反复抛出（见 services._HTTPOnlyFilter）。
            if not isinstance(flow, HTTPFlow):
                if column_name == "Method":
                    return type(flow).__name__.replace("Flow", "").upper()
                if column_name in ("URL", "Status Code", "Duration"):
                    return "—"
                return ""
            if column_name == "Method":
                return flow.request.method
            if column_name == "URL":
                return flow.request.pretty_url
            if column_name == "Status Code":
                if flow.error:
                    return "Error"
                if flow.response is None:
                    return "等待中..."
                return flow.response.status_code
            if column_name == "Duration":
                if flow.response is None or flow.request.timestamp_start is None:
                    return ""
                duration = (
                    flow.response.timestamp_end or 0
                ) - flow.request.timestamp_start
                return f"{duration * 1000:.0f} ms"
            return ""

        return None

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
        self._rows.clear()
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

            client_addr = flow.client_conn.peername if flow.client_conn else None
            proc_info = resolve_process(client_addr) if client_addr else None
            app = proc_info.to_dict() if proc_info else {}

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
                    **app,
                    "Connection ID": flow.id,
                    "Connection Time": conn_time,
                    "Front Client Address": client_pn[0] if client_pn else "N/A",
                    "Front Client Port": client_pn[1] if client_pn else "N/A",
                    "Front Server Address": client_sn[0] if client_sn else "N/A",
                    "Front Server Port": client_sn[1] if client_sn else "N/A",
                }
            )

            data["Request Params"] = parse_params(flow.request.url)
            data["Request Cookies"] = parse_cookies_from_headers(
                dict(flow.request.headers), "Cookie"
            )

        if state in ("request", "response_headers", "complete", "error"):
            body = _safe_content(flow.request)
            req_duration = None
            if flow.request.timestamp_end and flow.request.timestamp_start:
                req_duration = (
                    flow.request.timestamp_end - flow.request.timestamp_start
                ) * 1000
            req_ct = flow.request.headers.get("Content-Type", "-")
            req_body_info = build_body(body, req_ct)
            data.update(
                {
                    "req_size": len(body),
                    "req_duration": req_duration,
                    "Request Body": body,
                    "Request Content-Type": req_ct,
                    "Request Body Text": req_body_info["text"],
                    "Request Body Pretty": req_body_info["pretty"],
                    "Request Fold Regions": req_body_info["fold_regions"],
                    "Request Is Binary": req_body_info["is_binary"],
                    "Request Body MIME": req_body_info["mime"],
                }
            )

        if state in ("response_headers", "complete", "error") and flow.response:
            data["Response Cookies"] = parse_cookies_from_headers(
                dict(flow.response.headers), "Set-Cookie"
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
            body = _safe_content(flow.response)
            req_total_size = data.get("req_headers_size", 0) + data.get("req_size", 0)
            res_total_size = data.get("res_headers_size", 0) + len(body)
            total_size = req_total_size + res_total_size
            res_ct = flow.response.headers.get("Content-Type", "-")
            res_body_info = build_body(body, res_ct)
            data.update(
                {
                    "Response Body": body,
                    "Response Content-Type": res_ct,
                    "Response Body Text": res_body_info["text"],
                    "Response Body Pretty": res_body_info["pretty"],
                    "Response Fold Regions": res_body_info["fold_regions"],
                    "Response Is Binary": res_body_info["is_binary"],
                    "Response Body MIME": res_body_info["mime"],
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


class PacketProxyModel(QSortFilterProxyModel):
    """排序代理（透明过滤）。

    搜索/协议/状态码/内容类型等过滤已统一下沉到 mitmproxy 的 ``View.set_filter``
    （见 ``CaptureController.apply_filter``），由 flowfilter 表达式表达。因此本代理
    **不再做任何行级过滤**，只负责表格排序。这样：
    * 过滤不触发 _build_row_data() 的详情解析（性能）；
    * 过滤只影响 View 可见列表（_view），_store 保留全部流量（无清除效果）。
    """

    def __init__(self, parent: QObject):
        super().__init__(parent)

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        # 透明：保留源模型所有行（过滤已由 View.set_filter 完成）
        return True
