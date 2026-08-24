from datetime import UTC, datetime

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
    GatewayPolicy,
    HTTPFlow,
    human,
    wire_size,
)
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
    * 过滤不触发详情字典的构建（性能）；
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
