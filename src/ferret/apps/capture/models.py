import zlib
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from ferret.apps.capture.services import FlowView, HTTPFlow


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
            flow = self._rows[row]
            return FlowView(flow).to_dict()
        return {}

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
    def __init__(self, parent: QObject):
        super().__init__(parent)
        # 过滤器状态
        self._protocol_filter: set[str] = set()  # 空 = 全部
        self._status_group: set[str] = set()  # 空 = 全部
        self._content_type_filter: set[str] = set()  # 空 = 全部
        self._search_text: str = ""
        self._search_field: str = "全部"  # 全部/URL/Method/Host
        self._search_mode: str = "包含"  # 包含/等于/不包含
        # 多条件搜索
        self._multi_conditions: list[dict] = []

    # ── 设置过滤器 ──
    def set_protocol_filter(self, protocols: set[str]):
        self._protocol_filter = protocols
        self.invalidateFilter()

    def set_status_group(self, groups: set[str]):
        self._status_group = groups
        self.invalidateFilter()

    def set_content_type_filter(self, types_: set[str]):
        self._content_type_filter = types_
        self.invalidateFilter()

    def set_search(self, text: str, field: str = "全部", mode: str = "包含"):
        self._search_text = text
        self._search_field = field
        self._search_mode = mode
        self.invalidateFilter()

    def set_multi_search(self, conditions: list[dict]):
        """设置多条件搜索，conditions 格式: [{"field": "URL", "logic": "包含", "value": "api"}, ...]"""
        self._multi_conditions = conditions
        self.invalidateFilter()

    def _get_field_text(self, data: dict, field: str) -> str:
        """根据字段名获取搜索文本"""
        if field == "全部":
            values = [
                str(data.get("URL", "")),
                str(data.get("Method", "")),
                str(data.get("Host", "")),
                str(data.get("Status Code", "")),
                str(data.get("Response Content-Type", "")),
            ]
            # 也搜索 Header 和 Body
            req_headers = data.get("Request Headers", {})
            res_headers = data.get("Response Headers", {})
            if isinstance(req_headers, dict):
                values.extend([str(v) for v in req_headers.values()])
            if isinstance(res_headers, dict):
                values.extend([str(v) for v in res_headers.values()])
            req_body = data.get("Request Body Text")
            res_body = data.get("Response Body Text")
            if req_body is not None:
                values.append(str(req_body))
            elif isinstance(data.get("Request Body"), bytes):
                # errors="replace" 保证 decode 不会抛异常
                values.append(data["Request Body"].decode("utf-8", errors="replace"))
            if res_body is not None:
                values.append(str(res_body))
            elif isinstance(data.get("Response Body"), bytes):
                values.append(data["Response Body"].decode("utf-8", errors="replace"))
            return " ".join(values).lower()
        elif field == "URL":
            return str(data.get("URL", "")).lower()
        elif field == "Method":
            return str(data.get("Method", "")).lower()
        elif field == "Header":
            req_headers = data.get("Request Headers", {})
            res_headers = data.get("Response Headers", {})
            parts = []
            if isinstance(req_headers, dict):
                for k, v in req_headers.items():
                    parts.append(f"{k}: {v}")
            if isinstance(res_headers, dict):
                for k, v in res_headers.items():
                    parts.append(f"{k}: {v}")
            return " ".join(parts).lower()
        elif field == "Body":
            req_body = data.get("Request Body Text")
            res_body = data.get("Response Body Text")
            parts = []
            if req_body is not None:
                parts.append(str(req_body))
            elif isinstance(data.get("Request Body"), bytes):
                parts.append(data["Request Body"].decode("utf-8", errors="replace"))
            if res_body is not None:
                parts.append(str(res_body))
            elif isinstance(data.get("Response Body"), bytes):
                parts.append(data["Response Body"].decode("utf-8", errors="replace"))
            return " ".join(parts).lower()
        elif field == "Host":
            return str(data.get("Host", "")).lower()
        elif field == "Status Code":
            return str(data.get("Status Code", "")).lower()
        return ""

    def _check_single_condition(self, data: dict, condition: dict) -> bool:
        """检查单个过滤条件是否匹配"""
        field = condition.get("field", "全部")
        logic = condition.get("logic", "包含")
        value = condition.get("value", "").lower()
        if not value:
            return True

        text = self._get_field_text(data, field)

        if logic == "包含":
            return value in text
        elif logic == "不包含":
            return value not in text
        elif logic == "等于":
            return value == text
        elif logic == "正则表达式":
            import re

            try:
                return bool(re.search(value, text))
            except re.error:
                return False
        return True

    # ── 核心过滤逻辑 ──
    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        model = self.sourceModel()
        if not isinstance(model, PacketTableModel):
            return True
        if source_row >= len(model._rows):
            return False  # 超出范围的行不显示

        flow = model._rows[source_row]
        try:
            data = FlowView(flow).to_dict()
        except (ValueError, zlib.error, KeyError, AttributeError):
            return True
        if not data:
            return True

        # 5. 多条件搜索（AND 逻辑）- 放在最前面快速排除
        if self._multi_conditions:
            for cond in self._multi_conditions:
                if not self._check_single_condition(data, cond):
                    return False
        # 兼容旧的单条件搜索
        elif self._search_text and not self._check_single_condition(
            data,
            {
                "field": self._search_field,
                "logic": self._search_mode,
                "value": self._search_text,
            },
        ):
            return False

        # 1. 协议过滤
        if self._protocol_filter:
            url = data.get("URL", "")
            scheme = data.get("Scheme", "").lower()
            matched = False
            for p in self._protocol_filter:
                if (
                    p == "HTTP"
                    and scheme == "http"
                    or p == "HTTPS"
                    and scheme == "https"
                    or p == "WebSocket"
                    and "websocket" in url.lower()
                    or p == "HTTP1"
                    and "1.1" in data.get("HTTP Version", "")
                    or p == "HTTP2"
                    and "2" in data.get("HTTP Version", "")
                    or p == "SSE"
                    and (
                        "text/event-stream" in data.get("Response Content-Type", "")
                        or "text/event-stream" in data.get("Request Content-Type", "")
                    )
                    or p == "iOS"
                    and data.get("App Name", "")
                ):
                    matched = True
            if not matched:
                return False

        # 2. 内容类型过滤
        if self._content_type_filter:
            resp_ct = data.get("Response Content-Type", "") or ""
            ct = resp_ct.lower()
            matched = False
            for t in self._content_type_filter:
                if (
                    t == "JSON"
                    and "json" in ct
                    or t == "XML"
                    and "xml" in ct
                    or t == "文本"
                    and ("text/" in ct or "plain" in ct)
                    or t == "HTML"
                    and "html" in ct
                    or t == "JS"
                    and ("javascript" in ct or "/js" in ct)
                    or t == "图片"
                    and ("image/" in ct)
                    or t == "媒体"
                    and ("video/" in ct or "audio/" in ct)
                    or t == "二进制"
                    and ("octet-stream" in ct or "pdf" in ct or "zip" in ct)
                ):
                    matched = True
            if not matched:
                return False

        # 3. 状态码分组过滤
        if self._status_group:
            code = data.get("Status Code", "")
            try:
                code_int = int(code)
            except (ValueError, TypeError):
                return False
            matched = False
            for g in self._status_group:
                if (
                    g == "1xx"
                    and 100 <= code_int < 200
                    or g == "2xx"
                    and 200 <= code_int < 300
                    or g == "3xx"
                    and 300 <= code_int < 400
                    or g == "4xx"
                    and 400 <= code_int < 500
                    or g == "5xx"
                    and 500 <= code_int < 600
                ):
                    matched = True
            if not matched:
                return False

        # 4. 搜索文本过滤（单条件，向后兼容）
        if self._search_text:
            search_value = self._search_text.lower()
            text = self._get_field_text(data, self._search_field)
            if self._search_mode == "包含":
                if search_value not in text:
                    return False
            elif self._search_mode == "不包含":
                if search_value in text:
                    return False
            elif self._search_mode == "等于" and search_value != text:
                return False

        return True
