from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Protocol

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractTableModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QColor, QFont
from qfluentwidgets import isDarkTheme

# format_duration 的家在 fields.py（与 format_time / _ms 同处），这里只是
# re-export：表格 Time 列与详情页总时长第一次走同一个格式化器，旧导入点不动。
# 冗余别名是 PEP 484 的显式 re-export 写法（ty 认这个标记）。
from ferret.apps.common.flow.columns import (
    DEFAULT_ORDER,
    column_display_title,
    header_of,
    key_of_header,
)
from ferret.apps.common.flow.fields import (
    format_duration as format_duration,  # noqa: PLC0414
)
from ferret.apps.common.flow.marks import emoji_font, marker_glyph
from ferret.core.log import get_logger
from ferret.core.mitm import SUSPEND_POLICIES, FlowRow, human

log = get_logger("flow")

METHOD_ROLE = int(Qt.ItemDataRole.UserRole) + 1
STATUS_KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 2
FULL_URL_ROLE = int(Qt.ItemDataRole.UserRole) + 3
MIME_ROLE = int(Qt.ItemDataRole.UserRole) + 4
DURATION_MS_ROLE = int(Qt.ItemDataRole.UserRole) + 5
SIZE_BYTES_ROLE = int(Qt.ItemDataRole.UserRole) + 6
SORT_ROLE = int(Qt.ItemDataRole.UserRole) + 7
# 「这一行是否命中当前搜索表达式」——搜索高亮模式用。命中判定在 mitm 线程算好
# 一份 flow.id 集回推（见 set_highlight_ids），Qt 侧只做 O(1) 查表。
HIGHLIGHT_ROLE = int(Qt.ItemDataRole.UserRole) + 8

# 网关往 flow.metadata 里写的是策略名（`str(GatewayPolicy)`）；行快照把它折成
# `gateway_policy` 标量带过来。这里只认字符串、不导 apps/gateway —— apps/common
# 不该认识具体页面。
_SUSPEND_MARKS: frozenset[str] = frozenset(str(policy) for policy in SUSPEND_POLICIES)


def is_suspended(row: FlowRow) -> bool:
    """这条流量此刻是否停着不动 —— 网关挂起或断点拦下都算。

    两个来源都要认：网关放行时 addon 会把 metadata 标记摘掉（快照随之不再带
    `gateway_policy`），断点则由原生 `flow.intercepted` 表示（`resume()` 会清成
    False）。两个状态都随每次变更重新折进行快照，所以照旧是实时状态。断点还会
    拦网关挂起以外的流量，只看网关标记会让「拦截队列」里明明钉着的那条在流量表
    里显示成「等待中」。
    """
    if row.intercepted:
        return True
    return row.gateway_policy in _SUSPEND_MARKS


class FlowSource(Protocol):
    """流量表的数据源：形状即 mitmproxy ``View``（Sequence + clear + remove）。

    行集是 `FlowRow` 快照而非 flow 对象（#90）：抓包路径用适配器包住 facade
    （迭代/写都投 mitm 线程执行，快照在内核侧折好，见
    `apps/capture/views.py::_CaptureFlowSource`）；会话路径包住自己的 View ——
    那批 flow 从文件读回、没有 mitm 线程，在 GUI 线程就地折叠同样安全。
    model 自己不碰线程策略。
    """

    def __iter__(self) -> Iterator[FlowRow]: ...
    def clear(self) -> None: ...
    def remove(self, flow_ids: Sequence[str]) -> None: ...


class FlowTableModel(QAbstractTableModel):
    # 表头分派串由 columns.py 派生（稳定 key → header），逻辑列顺序恒＝DEFAULT_ORDER。
    # Mark 列紧随 #：标记载的是 emoji 短码（`flow.marked`），显示经 `marker_glyph`
    # 翻译成图形字符；只占一个字符位，宽度在视图侧钉死（columns.py 的 fixed_width）。
    HEADERS = tuple(header_of(key) for key in DEFAULT_ORDER)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._headers = list(self.HEADERS)
        self._source: FlowSource | None = None
        # 稳定行号列表：model 自己的"行号→行快照"映射，不依赖 View 的 SortedList
        # 排序位置（并发重排会导致插入声明位置与取数位置失配 → 空行/错数据）。
        # 数据源只作为行快照存储/过滤后端，行号由此列表自治。行按 flow.id 寻址
        # （与连接树同键），更新到达时整体替换快照。
        self._rows: list[FlowRow] = []
        self._row_by_id: dict[str, int] = {}
        # 搜索高亮模式下命中当前表达式的 flow.id 集（在 mitm 线程算好后回推）。
        self._highlight_ids: set[str] = set()

    def set_source(self, source: FlowSource) -> None:
        """注入数据源（FlowSource 协议）并重置模型"""
        self.beginResetModel()
        self._source = source
        self._rows = list(source)
        self._reindex()
        self.endResetModel()

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                # 显示标题走 columns.column_display_title：只 "Mark"→「标记」，其余列头
                # 用原文，context 钉死 "FlowTableModel"，与列设置对话框共用一条路径。
                return column_display_title(key_of_header(self._headers[section]))
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

        # 高亮命中：任何流量类型都只查一份 id 集（O(1)），每格都返回同值，委托
        # 据此给命中行整行铺底。放在类型分流之前，非 HTTP 行也能正确回 False
        # （其 id 本就不会进命中集）。
        if role == HIGHLIGHT_ROLE:
            return flow.id in self._highlight_ids

        # `#` 列的行号是模型形状（平铺给全局行号），DisplayRole/SORT_ROLE 自算；
        # 其余列与角色统一交给共享渲染 flow_cell —— 与连接树的子行同走一套逻辑。
        if column_name == "#":
            if role == Qt.ItemDataRole.DisplayRole:
                return row + 1
            if role == SORT_ROLE:
                return row + 1
        return flow_cell(flow, column_name, role)

    @staticmethod
    def _host(row: FlowRow) -> str:
        return row.host

    @classmethod
    def _host_with_port(cls, row: FlowRow) -> str:
        host = row.host
        return f"{host}:{row.port}" if row.port else host

    @staticmethod
    def _mime(row: FlowRow) -> str:
        return row.resp_mime or row.req_mime

    @staticmethod
    def _mime_label(mime: str) -> str:
        """Type 列的短标。统一小写（用户约定）：json / html / xml / js / css …"""
        value = mime.lower()
        if not value:
            return "—"
        if "json" in value:
            return "json"
        if "html" in value:
            return "html"
        if "xml" in value:
            return "xml"
        if "javascript" in value:
            return "js"
        if "css" in value:
            return "css"
        if value.startswith("image/"):
            return value.split("/", 1)[1]
        if value.startswith("text/"):
            return "text"
        if "form" in value:
            return "form"
        return value.split("/", 1)[-1]

    @staticmethod
    def _size_bytes(row: FlowRow) -> int:
        """Size 列的字节数 —— 请求体 + 响应体的**线上**字节（压缩后）。

        和详情面板的「线上」行同走 `wire_size()`，两处数字才必然一致；字节在
        内核侧折叠快照时算好（`req_wire` / `resp_wire`），Qt 侧只做加法。
        """
        return row.req_wire + row.resp_wire

    @staticmethod
    def _duration_ms(row: FlowRow) -> float | None:
        if not row.has_response or row.req_start is None:
            return None
        if row.resp_end is None:
            return None
        return max(0.0, (row.resp_end - row.req_start) * 1000)

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
    def _status_kind(row: FlowRow) -> str:
        if is_suspended(row):
            return "pending"
        if row.error_msg:
            return "error"
        if not row.has_response:
            return "pending"
        code = row.status_code
        if code is None:
            return "pending"
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

    # ------------------------------------------------------------------
    # 数据变化处理（由 View 桥接信号驱动）
    # ------------------------------------------------------------------
    def _row_of(self, row: FlowRow) -> int:
        """在稳定行号列表中查找快照所属行的索引（按 flow.id，不依赖 View 排序）"""
        return self._row_by_id.get(row.id, -1)

    def _reindex(self) -> None:
        self._row_by_id = {item.id: row for row, item in enumerate(self._rows)}

    def handle_add(self, row: FlowRow) -> None:
        """处理桥接信号送来的新增行快照：追加到末尾，行号由 _rows 自治"""
        if not self._source:
            return
        if row.id in self._row_by_id:
            return  # 防重复
        index = len(self._rows)
        self.beginInsertRows(QModelIndex(), index, index)
        self._rows.append(row)
        self._row_by_id[row.id] = index
        self.endInsertRows()

    def handle_update(self, row: FlowRow) -> None:
        """处理更新：按 id 整体替换该行快照，再刷整行"""
        index = self._row_of(row)
        if index < 0:
            return
        self._rows[index] = row
        start_idx = self.index(index, 0)
        end_idx = self.index(index, self.columnCount() - 1)
        self.dataChanged.emit(start_idx, end_idx)

    def handle_remove(self, row: FlowRow, index: int) -> None:
        """处理移除：按快照 id 反查 _rows 下标，避免 View 源索引错位"""
        position = self._row_of(row)
        if position < 0:
            return
        self.beginRemoveRows(QModelIndex(), position, position)
        self._rows.pop(position)
        self._reindex()
        self.endRemoveRows()

    def handle_refresh(self) -> None:
        """处理 View 整体刷新：同步重建 _rows"""
        self.beginResetModel()
        self._rows = list(self._source) if self._source else []
        self._reindex()
        self.endResetModel()

    def set_highlight_ids(self, ids: set[str]) -> None:
        """回推「命中搜索表达式」的 flow.id 集，只刷背景不动行集。

        搜索高亮模式专用：命中集由上层在 mitm 线程算好（facade.match_ids），这里仅
        存下并对全表发一次 HIGHLIGHT_ROLE 的 dataChanged 触发重绘。集合相等则短路，
        避免直播重算时无谓刷屏。
        """
        if ids == self._highlight_ids:
            return
        self._highlight_ids = ids
        if self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, self.columnCount() - 1)
            self.dataChanged.emit(top, bottom, [HIGHLIGHT_ROLE])

    # ------------------------------------------------------------------
    # 数据访问
    # ------------------------------------------------------------------
    def clear_data(self):
        """清空表格内容"""
        self.beginResetModel()
        self._rows.clear()
        self._row_by_id.clear()
        self.endResetModel()
        if self._source:
            self._source.clear()

    def get_row(self, row: int) -> FlowRow | None:
        """根据行号获取行快照"""
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def remove_row(self, row: int):
        """删除指定行"""
        if not self._source or not (0 <= row < len(self._rows)):
            return
        self._source.remove([self._rows[row].id])

    def remove_flows(self, flow_ids: list[str]) -> None:
        """批量删除：一次 remove 调用（按 id 寻址），逐行移除走桥接信号回路。"""
        if not self._source or not flow_ids:
            return
        self._source.remove(flow_ids)


def flow_cell(row: FlowRow, column_name: str, role: int):
    """FlowTableModel 与 FlowConnTreeModel 子行共用的单元格渲染。

    覆盖除 ``#`` 列外的所有列：``#`` 依赖模型形状（平铺给全局行号、树给组内序号），
    由各模型自算；HIGHLIGHT_ROLE 命中集也各自持有，均不进这里。翻译 context 一律
    钉死 "FlowTableModel"——本函数是从 `FlowTableModel.data` 抽出的纯搬迁，改 context
    会让既有译文对不上号（`self.tr` 的隐式 context 正是类名）。入参是行快照：
    渲染只读 Qt 侧自己的不可变数据，不再触碰任何 flow 对象。
    """
    translate = QCoreApplication.translate

    if not row.is_http:
        if role == Qt.ItemDataRole.DisplayRole:
            if column_name == "Method":
                return row.type_label
            return ""
        return None

    if role == Qt.ItemDataRole.DisplayRole:
        if column_name == "Mark":
            return marker_glyph(row.marked)
        if column_name == "Method":
            return row.method
        if column_name == "URL":
            return row.url
        if column_name == "Status":
            # 挂起优先于响应码：挂起（入）时响应已经回来了，但客户端一个字节
            # 都没拿到，显示 200 会骗人。
            if is_suspended(row):
                return translate("FlowTableModel", "挂起中")
            if row.error_msg:
                return "Error"
            if not row.has_response:
                return translate("FlowTableModel", "等待中")
            return row.status_code
        if column_name == "Type":
            return FlowTableModel._mime_label(FlowTableModel._mime(row))
        if column_name == "Size":
            return human.pretty_size(FlowTableModel._size_bytes(row))
        if column_name == "Time":
            return format_duration(FlowTableModel._duration_ms(row))
        return ""
    if role == SORT_ROLE:
        if column_name == "Mark":
            # 短码字符串本身：空串与有值天然分堆，同类短码聚族。
            return row.marked
        if column_name == "Method":
            return row.method.upper()
        if column_name == "URL":
            return row.url.lower()
        if column_name == "Status":
            if is_suspended(row):
                return -1
            if row.error_msg:
                return 600
            return row.status_code if row.has_response else -1
        if column_name == "Type":
            return FlowTableModel._mime(row).lower()
        if column_name == "Size":
            return FlowTableModel._size_bytes(row)
        if column_name == "Time":
            duration = FlowTableModel._duration_ms(row)
            return duration if duration is not None else -1.0

    if role == METHOD_ROLE:
        return row.method.upper()
    if role == STATUS_KIND_ROLE:
        return FlowTableModel._status_kind(row)
    if role == FULL_URL_ROLE:
        return row.url
    if role == MIME_ROLE:
        return FlowTableModel._mime(row)
    if role == DURATION_MS_ROLE:
        return FlowTableModel._duration_ms(row)
    if role == SIZE_BYTES_ROLE:
        return FlowTableModel._size_bytes(row)
    if role == Qt.ItemDataRole.ToolTipRole:
        if column_name == "Mark":
            # 认不出图形的人悬浮看短码原文；未标记不弹空提示。
            return row.marked or None
        return None

    if role == Qt.ItemDataRole.ForegroundRole and column_name == "Status":
        return FlowTableModel._semantic_color(FlowTableModel._status_kind(row))

    if role == Qt.ItemDataRole.FontRole and column_name == "Mark":
        # emoji-first 字体，让 ✈ ♉ 这类文本态符号也画成彩色（见 marks.py）；
        # delegate 靠 FontRole 生效，设在视图上会被盖掉。行高 34px，字号取 18。
        if row.marked:
            return emoji_font(18)
        return None

    if role == Qt.ItemDataRole.TextAlignmentRole:
        if column_name == "Mark":
            return int(Qt.AlignmentFlag.AlignCenter)
        return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return None


def _conn_id(row: FlowRow) -> str:
    """flow 的分组键：客户端物理连接 id；缺失回落兜底组（折叠已在内核侧完成）。"""
    return row.conn_id


class _ConnNode:
    """一条客户端物理连接的聚合节点（连接树的顶层节点）。

    `flows` 保持到达序（append），排序交给代理；聚合值都从**行快照**现算、不缓存
    ——子流 update（pending→完成）后聚合要跟着变，缓存反而要额外失效逻辑。快照
    替换按 id 整体进行，聚合读到的永远是同一次变更的一致标量集。
    """

    __slots__ = ("conn_id", "flows")

    def __init__(self, conn_id: str) -> None:
        self.conn_id = conn_id
        self.flows: list[FlowRow] = []

    def size_bytes(self) -> int:
        return sum(FlowTableModel._size_bytes(f) for f in self.flows)

    def _starts(self) -> list[float]:
        out: list[float] = []
        for f in self.flows:
            if f.req_start:
                out.append(f.req_start)
        return out

    def _ends(self) -> list[float]:
        out: list[float] = []
        for f in self.flows:
            if f.resp_end:
                out.append(f.resp_end)
        return out

    def span_ms(self) -> float | None:
        starts = self._starts()
        ends = self._ends()
        if not starts or not ends:
            return None
        return max(0.0, (max(ends) - min(starts)) * 1000)

    def max_end_ts(self) -> float | None:
        ends = self._ends()
        return max(ends) if ends else None

    def hosts(self) -> list[str]:
        seen: list[str] = []
        for f in self.flows:
            host = FlowTableModel._host_with_port(f)
            if host and host not in seen:
                seen.append(host)
        return seen

    def client_address(self) -> str:
        for f in self.flows:
            if f.client_peername:
                return f.client_peername
        return ""

    def transport_label(self) -> str:
        for f in self.flows:
            if f.client_alpn:
                return f.client_alpn
            if f.client_tls:
                return f.client_tls
        return "TCP"

    def conn_label(self) -> str:
        client = self.client_address() or QCoreApplication.translate(
            "FlowConnTreeModel", "未知客户端"
        )
        hosts = self.hosts()
        if len(hosts) == 1:
            return f"{client} → {hosts[0]}"
        if not hosts:
            return client
        # 显式代理 + keep-alive 下一条物理连接可跨多个 host，不误标单一 host（见方案 D4）。
        return QCoreApplication.translate(
            "FlowConnTreeModel", "{client} → {count} 个目标"
        ).format(client=client, count=len(hosts))


class FlowConnTreeModel(QAbstractItemModel):
    """按客户端物理连接（`client_conn.id`）分组的两级树模型。

    结构自治（沿用平铺模型 `_rows` 自治的教训——不依赖 View 的 SortedList 位置）：
    `_nodes` 顶层顺序（新连接 append），`_by_conn` conn_id→节点，`_by_flow`
    flow.id→节点（update/remove O(1) 反查）。子行渲染委托 `flow_cell`，与平铺共用；
    子行存的是行快照，更新按 id 整体替换。
    """

    HEADERS = FlowTableModel.HEADERS

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._headers = list(self.HEADERS)
        self._source: FlowSource | None = None
        self._nodes: list[_ConnNode] = []
        self._by_conn: dict[str, _ConnNode] = {}
        self._by_flow: dict[str, _ConnNode] = {}
        self._highlight_ids: set[str] = set()

    # ------------------------------------------------------------------
    # QAbstractItemModel 结构
    # ------------------------------------------------------------------
    def index(
        self,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex | None = None,
    ) -> QModelIndex:
        if parent is None:
            parent = QModelIndex()
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            # 顶层连接节点：internalPointer 留空，靠 row 定位 _nodes。
            return self.createIndex(row, column, None)
        # 子行：把父连接节点塞进 internalPointer，parent() 靠它回溯。
        node = self._nodes[parent.row()]
        return self.createIndex(row, column, node)

    def parent(  # ty: ignore[invalid-method-override]
        self, index: QModelIndex | QPersistentModelIndex
    ) -> QModelIndex:
        # QAbstractItemModel.parent 有无参 `-> QObject` 重载，签名与树模型的
        # `parent(index) -> QModelIndex` 天然冲突；Qt 运行期按参数分派，这里的
        # 覆盖是标准写法，忽略静态检查对该重载的 LSP 抱怨。
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if node is None:
            return QModelIndex()  # 顶层节点无父
        try:
            top_row = self._nodes.index(node)
        except ValueError:
            return QModelIndex()
        return self.createIndex(top_row, 0, None)

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        if parent is None or not parent.isValid():
            return len(self._nodes)
        if parent.column() > 0:
            return 0
        if parent.internalPointer() is None:
            top_row = parent.row()
            if 0 <= top_row < len(self._nodes):
                return len(self._nodes[top_row].flows)
            return 0
        return 0  # 子行（flow）没有下一级

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._headers)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return column_display_title(key_of_header(self._headers[section]))
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid():
            return None
        column_name = self._headers[index.column()]
        node = index.internalPointer()
        if node is None:
            top_row = index.row()
            if not (0 <= top_row < len(self._nodes)):
                return None
            return self._conn_data(self._nodes[top_row], column_name, role)

        child_row = index.row()
        if not (0 <= child_row < len(node.flows)):
            return None
        row = node.flows[child_row]
        if role == HIGHLIGHT_ROLE:
            return row.id in self._highlight_ids
        # 子行的 `#` 列 = 组内序号（父节点则是子流计数，见 _conn_data）。
        if column_name == "#":
            if role == Qt.ItemDataRole.DisplayRole:
                return child_row + 1
            if role == SORT_ROLE:
                return child_row + 1
        return flow_cell(row, column_name, role)

    def _conn_data(self, node: _ConnNode, column_name: str, role: int):
        if role == HIGHLIGHT_ROLE:
            return False  # 第一版父节点不做「组内命中」染色
        if role == Qt.ItemDataRole.DisplayRole:
            if column_name == "#":
                return len(node.flows)
            if column_name == "URL":
                return node.conn_label()
            if column_name == "Type":
                # 与子行 `_mime_label` 同一约定：Type 列一律小写。
                return node.transport_label().lower()
            if column_name == "Size":
                return human.pretty_size(node.size_bytes())
            if column_name == "Time":
                return format_duration(node.span_ms())
            return ""  # Mark / Method / Status 父节点留空
        if role == SORT_ROLE:
            if column_name == "#":
                return len(node.flows)
            if column_name == "Size":
                return node.size_bytes()
            if column_name == "Time":
                end = node.max_end_ts()
                return end if end is not None else -1.0
            if column_name == "URL":
                return node.conn_label().lower()
            if column_name == "Type":
                return node.transport_label().lower()
            return ""
        if role == Qt.ItemDataRole.FontRole:
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return None

    # ------------------------------------------------------------------
    # 数据源与增量（由 View 桥接信号驱动，语义对齐平铺模型）
    # ------------------------------------------------------------------
    def set_source(self, source: FlowSource) -> None:
        self.beginResetModel()
        self._source = source
        self._rebuild()
        self.endResetModel()

    def _rebuild(self) -> None:
        self._nodes = []
        self._by_conn = {}
        self._by_flow = {}
        if not self._source:
            return
        for row in self._source:
            self._append_flow(row)

    def _append_flow(self, row: FlowRow) -> _ConnNode:
        """把行快照挂进对应节点（无信号，供全量重建复用）。"""
        cid = _conn_id(row)
        node = self._by_conn.get(cid)
        if node is None:
            node = _ConnNode(cid)
            self._nodes.append(node)
            self._by_conn[cid] = node
        node.flows.append(row)
        self._by_flow[row.id] = node
        return node

    def handle_add(self, row: FlowRow) -> None:
        if not self._source or row.id in self._by_flow:
            return
        cid = _conn_id(row)
        node = self._by_conn.get(cid)
        if node is None:
            # 新连接：先在顶层插入空节点，再插首条子行（两段 begin/end 各自成对）。
            top_row = len(self._nodes)
            self.beginInsertRows(QModelIndex(), top_row, top_row)
            node = _ConnNode(cid)
            self._nodes.append(node)
            self._by_conn[cid] = node
            self.endInsertRows()
        top_row = self._nodes.index(node)
        parent_index = self.index(top_row, 0, QModelIndex())
        child_row = len(node.flows)
        self.beginInsertRows(parent_index, child_row, child_row)
        node.flows.append(row)
        self._by_flow[row.id] = node
        self.endInsertRows()
        if child_row > 0:
            # 已有连接下追加子流：父节点聚合列（#/Size/Time）跟着变。
            self._emit_conn_changed(top_row)

    def handle_update(self, row: FlowRow) -> None:
        node = self._by_flow.get(row.id)
        if node is None:
            return
        try:
            top_row = self._nodes.index(node)
            child_row = next(
                i for i, item in enumerate(node.flows) if item.id == row.id
            )
        except (ValueError, StopIteration):
            return
        parent_index = self.index(top_row, 0, QModelIndex())
        last_col = self.columnCount() - 1
        node.flows[child_row] = row
        self.dataChanged.emit(
            self.index(child_row, 0, parent_index),
            self.index(child_row, last_col, parent_index),
        )
        # 父节点聚合随子流状态变（pending→完成改 Size/Time）；只发 dataChanged，
        # 绝不整树 reset（否则展开状态丢失，见方案风险 1）。
        self._emit_conn_changed(top_row)

    def handle_remove(self, row: FlowRow, index: int) -> None:
        # 签名与桥接对齐（views 传 (row, index)），树按 id 反查、忽略 index。
        node = self._by_flow.pop(row.id, None)
        if node is None:
            return
        try:
            top_row = self._nodes.index(node)
            child_row = next(
                i for i, item in enumerate(node.flows) if item.id == row.id
            )
        except (ValueError, StopIteration):
            return
        parent_index = self.index(top_row, 0, QModelIndex())
        self.beginRemoveRows(parent_index, child_row, child_row)
        node.flows.pop(child_row)
        self.endRemoveRows()
        if not node.flows:
            # 节点空了：连父一起摘。
            self.beginRemoveRows(QModelIndex(), top_row, top_row)
            self._nodes.pop(top_row)
            self._by_conn.pop(node.conn_id, None)
            self.endRemoveRows()
        else:
            self._emit_conn_changed(top_row)

    def handle_refresh(self) -> None:
        # 过滤变化路径：与平铺一致允许整树 reset（展开状态丢失可接受，见风险 1）。
        self.beginResetModel()
        self._rebuild()
        self.endResetModel()

    def _emit_conn_changed(self, top_row: int) -> None:
        if not (0 <= top_row < len(self._nodes)):
            return
        last_col = self.columnCount() - 1
        self.dataChanged.emit(
            self.index(top_row, 0, QModelIndex()),
            self.index(top_row, last_col, QModelIndex()),
        )

    def set_highlight_ids(self, ids: set[str]) -> None:
        """回推命中集，只刷子行背景（父节点第一版不染色）。集合相等则短路。"""
        if ids == self._highlight_ids:
            return
        self._highlight_ids = ids
        last_col = self.columnCount() - 1
        for top_row, node in enumerate(self._nodes):
            if not node.flows:
                continue
            parent_index = self.index(top_row, 0, QModelIndex())
            self.dataChanged.emit(
                self.index(0, 0, parent_index),
                self.index(len(node.flows) - 1, last_col, parent_index),
                [HIGHLIGHT_ROLE],
            )

    # ------------------------------------------------------------------
    # 数据访问（供视图交互 / 统计 / 详情）
    # ------------------------------------------------------------------
    def child_count(self) -> int:
        """可见子流总数（连接节点不计入）——统计 shown 用，非顶层节点数。"""
        return len(self._by_flow)

    def node_at(self, index: QModelIndex | QPersistentModelIndex) -> _ConnNode | None:
        if not index.isValid():
            return None
        if index.internalPointer() is None:
            row = index.row()
            if 0 <= row < len(self._nodes):
                return self._nodes[row]
        return None

    def flow_at(self, index: QModelIndex | QPersistentModelIndex) -> FlowRow | None:
        if not index.isValid():
            return None
        node = index.internalPointer()
        if node is None:
            return None  # 连接节点本身不是 flow
        row = index.row()
        if 0 <= row < len(node.flows):
            return node.flows[row]
        return None

    def flows_under(self, index: QModelIndex | QPersistentModelIndex) -> list[FlowRow]:
        """节点 → 全部子行；子行 → 该单条。删除 / 导出走这条 parent→children 展开。"""
        node = self.node_at(index)
        if node is not None:
            return list(node.flows)
        flow = self.flow_at(index)
        return [flow] if flow is not None else []

    def connection_detail(self, node: _ConnNode) -> dict:
        """连接节点摘要字典（`kind == "connection"`），交详情面板只读渲染。

        读的是行快照里折好的 client_conn 标量握手信息；握手期 alpn/tls 可能
        尚为空串，展示无害。
        """
        first: FlowRow | None = next(iter(node.flows), None)
        span = node.span_ms()
        starts = node._starts()
        return {
            "kind": "connection",
            "conn_id": node.conn_id,
            "client": node.client_address(),
            "targets": node.hosts(),
            "transport": node.transport_label(),
            "tls_version": first.client_tls or None if first else None,
            "alpn": first.client_alpn if first else "",
            "sni": first.client_sni or None if first else None,
            "cipher": first.client_cipher or None if first else None,
            "flow_count": len(node.flows),
            "size": human.pretty_size(node.size_bytes()),
            "duration": format_duration(span) or "—",
            "start": _fmt_ts(min(starts)) if starts else "—",
            "end": _fmt_ts(node.max_end_ts()),
        }

    def clear_data(self) -> None:
        self.beginResetModel()
        self._nodes = []
        self._by_conn = {}
        self._by_flow = {}
        self.endResetModel()
        if self._source:
            self._source.clear()

    def remove_flows(self, flow_ids: list[str]) -> None:
        """批量删除：一次 remove 调用（按 id 寻址），逐行移除走桥接信号回路。"""
        if not self._source or not flow_ids:
            return
        self._source.remove(flow_ids)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return (
        datetime.fromtimestamp(ts, tz=UTC)
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


class FlowConnProxyModel(QSortFilterProxyModel):
    """连接树排序代理：默认锁首见序（顶层不随聚合抖动），用户点列头后才切聚合排。

    平铺代理开着 `setDynamicSortFilter(True)` + `SORT_ROLE`，若顶层直接暴露聚合值，
    每来一条子流都会触发顶层实时重排、连接行乱跳。对策见方案 §3.1：未经用户排序时
    所有兄弟按源行号（=append/到达序）比较——顶层稳定、子行保序；用户点列头置位后
    才回落到 `SORT_ROLE`（组间按聚合、组内按列，正是想要的语义）。
    """

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)
        self._user_sorted = False

    def is_user_sorted(self) -> bool:
        return self._user_sorted

    def mark_user_sorted(self) -> None:
        self._user_sorted = True

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        return True

    def lessThan(
        self,
        left: QModelIndex | QPersistentModelIndex,
        right: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if not self._user_sorted:
            # 兄弟按源行号：顶层=连接 append 序，子行=到达序，全程稳定不抖。
            return left.row() < right.row()
        return super().lessThan(left, right)


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
