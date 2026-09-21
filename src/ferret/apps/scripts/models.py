"""Table model and UI labels for user scripts."""

from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QCoreApplication,
    QMimeData,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from qfluentwidgets import FluentIcon, FluentIconBase

from ferret.core.mitm import (
    SCRIPT_ORIGIN_IMPORT,
    SCRIPT_ORIGIN_NEW,
    ScriptEntry,
    ScriptState,
    ScriptStatus,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 拖拽换序自带的 mime 类型。行序＝执行序（plans/scripts.md §3.2），所以拖动是
# 有语义的操作，不只是排版。
ROW_MIME_TYPE = "application/x-ferret-script-row"

# 文案表只存标记、不求值 —— 模块级求值赶在翻译器安装之前（`core/application.py`
# 顶层就 import 了 MainWindow），译文会永久冻结成中文。求值在下面那几个函数里做
# （见 `ferret.utils.i18n`）。
STATE_LABELS: dict[ScriptState, str] = {
    ScriptState.LOADED: QT_TRANSLATE_NOOP("ScriptState", "已装载"),
    ScriptState.ERROR: QT_TRANSLATE_NOOP("ScriptState", "加载失败"),
    ScriptState.MISSING: QT_TRANSLATE_NOOP("ScriptState", "文件缺失"),
    ScriptState.DISABLED: QT_TRANSLATE_NOOP("ScriptState", "已停用"),
}

STATE_ICONS: dict[ScriptState, FluentIconBase] = {
    ScriptState.LOADED: FluentIcon.ACCEPT,
    ScriptState.ERROR: FluentIcon.CANCEL,
    ScriptState.MISSING: FluentIcon.QUESTION,
    ScriptState.DISABLED: FluentIcon.HIDE,
}

# 内核没跑（或刚换过批还没回报）时条目没有状态：它既不是装载成功也不是失败，
# 单独一档，别拿 MISSING 顶替 —— 那会把「还没开始抓包」说成「文件没了」。
PENDING_LABEL = QT_TRANSLATE_NOOP("ScriptState", "待装载")

ORIGIN_LABELS: dict[str, str] = {
    SCRIPT_ORIGIN_IMPORT: QT_TRANSLATE_NOOP("ScriptOrigin", "导入"),
    SCRIPT_ORIGIN_NEW: QT_TRANSLATE_NOOP("ScriptOrigin", "新建"),
}

ORIGIN_ICONS: dict[str, FluentIconBase] = {
    SCRIPT_ORIGIN_IMPORT: FluentIcon.LINK,
    SCRIPT_ORIGIN_NEW: FluentIcon.DOCUMENT,
}

# 脚本即代码（plans/scripts.md §4）：两个入口 —— 列表页横幅与新建对话框 ——
# 共用这一句，所以同样存标记、用的时候求值。
TRUST_WARNING = QT_TRANSLATE_NOOP("Scripts", "脚本以应用同等权限执行，仅加载可信来源。")


def trust_warning() -> str:
    return QCoreApplication.translate("Scripts", TRUST_WARNING)


def state_label(status: ScriptStatus | None) -> str:
    """状态徽标文案；``None`` ＝ 内核还没回报过这条（待装载）。"""
    if status is None:
        return QCoreApplication.translate("ScriptState", PENDING_LABEL)
    return resolve_marker(STATE_LABELS, status.state, "ScriptState", str(status.state))


def state_icon(status: ScriptStatus | None) -> FluentIconBase | None:
    if status is None:
        return FluentIcon.HISTORY
    return STATE_ICONS.get(status.state)


def origin_label(origin: str) -> str:
    return resolve_marker(ORIGIN_LABELS, origin, "ScriptOrigin", origin)


def origin_icon(origin: str) -> FluentIconBase | None:
    return ORIGIN_ICONS.get(origin)


def script_name(entry: ScriptEntry) -> str:
    """列表里显示的名字：文件名（路径整条在「路径」列里）。"""
    path = entry.path.replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1] or entry.path


def status_summary(entry: ScriptEntry, status: ScriptStatus | None) -> str:
    """行 tooltip：状态 + 错误首行（完整 traceback 在下方面板里看）。"""
    label = state_label(status)
    if status is not None and status.error:
        first = status.error.strip().splitlines()[-1]
        return f"{label} · {first}"
    return f"{label} · {entry.path}"


class ScriptTableModel(QAbstractTableModel):
    """脚本清单。顺序即执行序：多个脚本按列表序依次拿到同一条流量
    （`FerretScriptAddon.addons` 按这个序返回 ns），所以行序有语义，不开排序。"""

    # 同理只做标记：类体也是导入期就求值的。求值在 `headerData()` 里做。
    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("ScriptTableModel", "启用"),
        QT_TRANSLATE_NOOP("ScriptTableModel", "名称"),
        QT_TRANSLATE_NOOP("ScriptTableModel", "来源"),
        QT_TRANSLATE_NOOP("ScriptTableModel", "状态"),
        QT_TRANSLATE_NOOP("ScriptTableModel", "路径"),
    ]

    enabled_toggled = Signal(int, bool)
    #: 拖拽换序：源行 → 目标行（落点已按「从上往下拖要减一格」折算过）。
    rows_moved = Signal(int, int)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._entries: list[ScriptEntry] = []
        self._statuses: dict[str, ScriptStatus] = {}

    def set_scripts(self, entries: list[ScriptEntry]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def set_statuses(self, statuses: dict[str, ScriptStatus]) -> None:
        """只有状态列要重画，不用整表 reset（reset 会吃掉用户的选中行）。"""
        self._statuses = dict(statuses)
        if not self._entries:
            return
        self.dataChanged.emit(
            self.index(0, 3),
            self.index(len(self._entries) - 1, 3),
            [
                Qt.ItemDataRole.DisplayRole,
                Qt.ItemDataRole.DecorationRole,
                Qt.ItemDataRole.ToolTipRole,
            ],
        )

    def entry_at(self, row: int) -> ScriptEntry | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return QCoreApplication.translate("ScriptTableModel", self.HEADERS[section])
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._entries)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self.HEADERS)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            # 空白处也要收拖拽：拖到最后一行下面就是「移到末尾」。
            return super().flags(index) | Qt.ItemFlag.ItemIsDropEnabled
        flags = super().flags(index) | Qt.ItemFlag.ItemIsDragEnabled
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._entries)):
            return None

        entry = self._entries[row]
        status = self._statuses.get(entry.path)
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1:
                return script_name(entry)
            if col == 2:
                return origin_label(entry.origin)
            if col == 3:
                return state_label(status)
            if col == 4:
                return entry.path
            return None

        if role == Qt.ItemDataRole.DecorationRole:
            icon = None
            if col == 2:
                icon = origin_icon(entry.origin)
            elif col == 3:
                icon = state_icon(status)
            return icon.icon() if icon is not None else None

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if entry.enabled else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.ToolTipRole:
            return status_summary(entry, status)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == 0:
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.UserRole:
            return entry

        return None

    def setData(
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        row = index.row()
        if not (0 <= row < len(self._entries)):
            return False
        enabled = Qt.CheckState(value) == Qt.CheckState.Checked
        entry = self._entries[row]
        if entry.enabled == enabled:
            return False
        # 勾选框的即时反馈交给控制器那一轮 `set_scripts`（它会回头重填本模型）。
        # 放到下一个事件循环再发，避免控制器 reset 本模型时落在 setData 里造成重入。
        QTimer.singleShot(0, lambda: self.enabled_toggled.emit(row, enabled))
        return True

    # --- 拖拽换序 ---

    def supportedDropActions(self) -> Qt.DropAction:
        return Qt.DropAction.MoveAction

    def mimeTypes(self) -> list[str]:
        return [ROW_MIME_TYPE]

    def mimeData(self, indexes: Any) -> QMimeData:
        rows = sorted({index.row() for index in indexes if index.isValid()})
        data = QMimeData()
        # 单行拖动就够用（表格是整行选中，一次拖一条最不容易拖错）。
        data.setData(ROW_MIME_TYPE, str(rows[0] if rows else -1).encode("ascii"))
        return data

    def dropMimeData(
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        """接住行内拖拽，把换序意图发给控制器 —— 本模型自己不搬数据。

        **刻意返回 False**：数据的唯一权威副本在控制器手上，它换完序会整批下发并
        回头重填本模型。返回 True 的话 `QAbstractItemView.startDrag` 会按
        `InternalMove` 的约定再把拖走的那行删一遍（原行删除由发起方做），于是一次
        拖动丢一条脚本。信号照发，换序照做，只是不让 view 插手行的增删。
        """
        if action != Qt.DropAction.MoveAction or not data.hasFormat(ROW_MIME_TYPE):
            return False
        try:
            payload = bytes(data.data(ROW_MIME_TYPE).data())
            source = int(payload.decode("ascii"))
        except ValueError:
            return False
        if not (0 <= source < len(self._entries)):
            return False
        # 落在两行之间时 row 是插入位；落在某一行上时 row 为 -1，取 parent 那一行。
        target = row if row >= 0 else parent.row()
        if target < 0:
            target = len(self._entries)
        if target > source:
            # 插入位是「搬走之前」的下标，往下拖时要减掉自己占的那一格。
            target -= 1
        target = max(0, min(target, len(self._entries) - 1))
        if target != source:
            QTimer.singleShot(0, lambda: self.rows_moved.emit(source, target))
        return False


class ScriptFilterProxyModel(QSortFilterProxyModel):
    """只有文字搜索一个条件（名称 / 路径 / 来源 / 状态都算）。"""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter_text: str = ""

    def set_filter_text(self, text: str) -> None:
        self.beginFilterChange()
        self._filter_text = (text or "").strip().lower()
        self.endFilterChange()

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        model = self.sourceModel()
        if not isinstance(model, ScriptTableModel):
            return True
        entry = model.entry_at(source_row)
        if entry is None or not self._filter_text:
            return True
        haystack = " ".join(
            (entry.path, script_name(entry), origin_label(entry.origin))
        ).lower()
        return self._filter_text in haystack
