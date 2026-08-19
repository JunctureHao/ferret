"""Table model and UI labels for block rules."""

from dataclasses import replace
from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)

from ferret.core.mitm import BLOCK_STATUS_CLOSE, BlockField, BlockLogic, BlockRule

FIELD_LABELS: dict[BlockField, str] = {
    BlockField.HOST: "主机",
    BlockField.URL: "URL",
    BlockField.METHOD: "方法",
}

# 与抓包过滤条的措辞保持一致（apps/capture/services.py 的 _condition_to_expr）。
LOGIC_LABELS: dict[BlockLogic, str] = {
    BlockLogic.CONTAINS: "包含",
    BlockLogic.EQUALS: "等于",
    BlockLogic.REGEX: "正则表达式",
}

STATUS_LABELS: dict[int, str] = {
    403: "403 Forbidden",
    404: "404 Not Found",
    451: "451 Unavailable For Legal Reasons",
    502: "502 Bad Gateway",
    BLOCK_STATUS_CLOSE: f"{BLOCK_STATUS_CLOSE} 直接断开连接",
}

STATUS_CHOICES: list[int] = [403, 404, 451, 502, BLOCK_STATUS_CLOSE]


def status_label(status_code: int) -> str:
    return STATUS_LABELS.get(status_code, str(status_code))


class BlockRuleTableModel(QAbstractTableModel):
    """规则列表。顺序即优先级，原生 addon 取第一条命中的 spec，故不开排序。"""

    HEADERS: ClassVar[list[str]] = ["启用", "匹配对象", "条件", "值", "响应"]

    enabled_toggled = Signal(int, bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._rules: list[BlockRule] = []

    def set_rules(self, rules: list[BlockRule]) -> None:
        self.beginResetModel()
        self._rules = list(rules)
        self.endResetModel()

    def rule_at(self, row: int) -> BlockRule | None:
        if 0 <= row < len(self._rules):
            return self._rules[row]
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
            return self.HEADERS[section]
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._rules)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self.HEADERS)

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
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
        if not (0 <= row < len(self._rules)):
            return None

        rule = self._rules[row]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 1:
                return FIELD_LABELS.get(rule.field, str(rule.field))
            if col == 2:
                return LOGIC_LABELS.get(rule.logic, str(rule.logic))
            if col == 3:
                return rule.value
            if col == 4:
                return status_label(rule.status_code)
            return None

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.ToolTipRole:
            try:
                return rule.expression
            except ValueError as exc:
                return str(exc)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == 0:
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.UserRole:
            return rule

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
        if not (0 <= row < len(self._rules)):
            return False
        enabled = Qt.CheckState(value) == Qt.CheckState.Checked
        rule = self._rules[row]
        if rule.enabled == enabled:
            return False
        # 先本地生效给即时反馈，控制器的落盘/下发放到下一个事件循环，
        # 避免它回头 reset 本模型时落在 setData 里造成重入。
        self._rules[row] = replace(rule, enabled=enabled)
        self.dataChanged.emit(index, index, [role])
        QTimer.singleShot(0, lambda: self.enabled_toggled.emit(row, enabled))
        return True


class BlockRuleFilterProxyModel(QSortFilterProxyModel):
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
        if not self._filter_text:
            return True
        model = self.sourceModel()
        if not isinstance(model, BlockRuleTableModel):
            return True
        rule = model.rule_at(source_row)
        if rule is None:
            return True
        haystack = " ".join(
            (
                rule.value,
                FIELD_LABELS.get(rule.field, str(rule.field)),
                LOGIC_LABELS.get(rule.logic, str(rule.logic)),
                status_label(rule.status_code),
            )
        ).lower()
        return self._filter_text in haystack
