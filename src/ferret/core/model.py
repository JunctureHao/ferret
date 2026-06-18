from datetime import datetime
from typing import Any, Dict, List

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    Slot,
)
from PySide6.QtGui import QColor
from qfluentwidgets import isDarkTheme

from ferret.utils.process_resolver import resolve_process


class PacketTableModel(QAbstractTableModel):
    def __init__(self, parent: QObject, traffic_addon=None):
        super().__init__(parent)

        self._headers = ["ID", "Method", "URL", "Status Code", "Duration", ""]
        self._data: List[Dict[str, Any]] = []  # 解析后的显示数据
        self._id_map = {}
        self._traffic_addon = traffic_addon  # UITrafficAddon 实例引用

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

    def rowCount(self, parent: QModelIndex | None = None):
        return len(self._data)

    def columnCount(self, parent: QModelIndex | None = None):
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        col = index.column()
        column_name = self._headers[col]

        value = self._data[row].get(column_name, "")
        if role == Qt.ItemDataRole.DisplayRole:
            if column_name == "ID":
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return 0

            if column_name == "Status Code":
                try:
                    return int(value)
                except (ValueError, TypeError):
                    return value  # 返回 "..."
            return str(value)

        # 进阶：处理对齐（可选，建议数字右对齐或居中）
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        # 语义着色
        if role == Qt.ItemDataRole.ForegroundRole:
            # 错误行：整行红色
            if self._data[row].get("state") == "error":
                return QColor("#ff4444")

            dark = isDarkTheme()

            # Method 列着色
            if column_name == "Method":
                method_colors = {
                    "GET":    ("#0550AE", "#7EE787"),
                    "POST":   ("#8250DF", "#D2A8FF"),
                    "PUT":    ("#9A6700", "#E3B341"),
                    "PATCH":  ("#9A6700", "#E3B341"),
                    "DELETE": ("#CF222E", "#FFA198"),
                    "HEAD":   ("#0E7490", "#56D4DD"),
                    "OPTIONS": ("#6E7781", "#8B949E"),
                }
                v = str(value).upper()
                if v in method_colors:
                    light_c, dark_c = method_colors[v]
                    return QColor(dark_c if dark else light_c)

            # Status Code 列着色
            if column_name == "Status Code":
                try:
                    code = int(value)
                except (ValueError, TypeError):
                    return None
                if 200 <= code < 300:
                    return QColor("#57AB5A" if dark else "#1A7F37")
                if 300 <= code < 400:
                    return QColor("#C69026" if dark else "#9A6700")
                if 400 <= code < 500:
                    return QColor("#E5534B" if dark else "#CF222E")
                if code >= 500:
                    return QColor("#FF7B72" if dark else "#A40E26")

            return None

    @Slot(dict)
    def set_data(self, data):
        """接收预处理后的字典数据"""
        flow_id = data.get("id")
        state = data.get("state")

        if not flow_id or not state:
            return

        # 插入类状态：第一次出现
        if state == "request_headers":
            last_row = len(self._data)
            self.beginInsertRows(QModelIndex(), last_row, last_row)
            self._id_map[flow_id] = last_row
            data["ID"] = int(last_row + 1)
            self._data.append(data)
            self.endInsertRows()
        # 更新类状态：合并到已有行
        else:
            if flow_id in self._id_map:
                row = self._id_map[flow_id]
                item = self._data[row]
                if isinstance(item, dict):
                    item.update(data)
                start_idx = self.index(row, 0)
                end_idx = self.index(row, self.columnCount() - 1)
                self.dataChanged.emit(start_idx, end_idx)

    def clear_data(self):
        """清空表格内容"""
        self.beginResetModel()
        self._data.clear()
        self._id_map.clear()
        self.endResetModel()

    def get_row_data(self, row: int) -> dict:
        """根据行号获取该行的原始字典数据"""
        if 0 <= row < len(self._data):
            return self._data[row]
        return {}

    def get_flow(self, row: int):
        """根据行号获取原始 flow 对象（从 UITrafficAddon 缓存中获取）"""
        if 0 <= row < len(self._data):
            flow_id = self._data[row].get("id")
            if flow_id and self._traffic_addon:
                return self._traffic_addon.get_flow(str(flow_id))
        return None

    def remove_row(self, row: int):
        if 0 <= row < len(self._data):
            self.beginRemoveRows(QModelIndex(), row, row)
            self._data.pop(row)
            self.__rebuild_id_map()
            self.endRemoveRows()

    def __rebuild_id_map(self):
        """辅助方法：重新建立 flow_id 到行号的映射"""
        self._id_map.clear()
        for i, item in enumerate(self._data):
            flow_id = item.get("id")
            if flow_id:
                self._id_map[flow_id] = i


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
            req_body = data.get("Request Body", b"")
            res_body = data.get("Response Body", b"")
            if isinstance(req_body, bytes):
                try:
                    values.append(req_body.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            else:
                values.append(str(req_body))
            if isinstance(res_body, bytes):
                try:
                    values.append(res_body.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            else:
                values.append(str(res_body))
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
            req_body = data.get("Request Body", b"")
            res_body = data.get("Response Body", b"")
            parts = []
            if isinstance(req_body, bytes):
                try:
                    parts.append(req_body.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            else:
                parts.append(str(req_body))
            if isinstance(res_body, bytes):
                try:
                    parts.append(res_body.decode("utf-8", errors="replace"))
                except Exception:
                    pass
            else:
                parts.append(str(res_body))
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
    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not hasattr(model, "_data"):
            return True
        data = model._data[source_row] if source_row < len(model._data) else {}
        if not data:
            return True

        # 5. 多条件搜索（AND 逻辑）- 放在最前面快速排除
        if self._multi_conditions:
            for cond in self._multi_conditions:
                if not self._check_single_condition(data, cond):
                    return False
        # 兼容旧的单条件搜索
        elif self._search_text:
            if not self._check_single_condition(
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
                if p == "HTTP" and scheme == "http":
                    matched = True
                elif p == "HTTPS" and scheme == "https":
                    matched = True
                elif p == "WebSocket" and "websocket" in url.lower():
                    matched = True
                elif p == "HTTP1" and "1.1" in data.get("HTTP Version", ""):
                    matched = True
                elif p == "HTTP2" and "2" in data.get("HTTP Version", ""):
                    matched = True
                elif p == "SSE" and (
                    "text/event-stream" in data.get("Response Content-Type", "")
                    or "text/event-stream" in data.get("Request Content-Type", "")
                ):
                    matched = True
                elif p == "iOS" and data.get("App Name", ""):
                    matched = True
            if not matched:
                return False

        # 2. 内容类型过滤
        if self._content_type_filter:
            resp_ct = data.get("Response Content-Type", "") or ""
            req_ct = data.get("Request Content-Type", "") or ""
            ct = resp_ct.lower()
            matched = False
            for t in self._content_type_filter:
                if t == "JSON" and "json" in ct:
                    matched = True
                elif t == "XML" and "xml" in ct:
                    matched = True
                elif t == "文本" and ("text/" in ct or "plain" in ct):
                    matched = True
                elif t == "HTML" and "html" in ct:
                    matched = True
                elif t == "JS" and ("javascript" in ct or "/js" in ct):
                    matched = True
                elif t == "图片" and ("image/" in ct):
                    matched = True
                elif t == "媒体" and ("video/" in ct or "audio/" in ct):
                    matched = True
                elif t == "二进制" and (
                    "octet-stream" in ct or "pdf" in ct or "zip" in ct
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
                if g == "1xx" and 100 <= code_int < 200:
                    matched = True
                elif g == "2xx" and 200 <= code_int < 300:
                    matched = True
                elif g == "3xx" and 300 <= code_int < 400:
                    matched = True
                elif g == "4xx" and 400 <= code_int < 500:
                    matched = True
                elif g == "5xx" and 500 <= code_int < 600:
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
            elif self._search_mode == "等于":
                if search_value != text:
                    return False

        return True
