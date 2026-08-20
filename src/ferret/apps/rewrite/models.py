"""Table model and UI labels for rewrite rules."""

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

from ferret.core.mitm import RewriteKind, RewriteLogic, RewriteRule

# 每个 RewriteKind 一条。将来接 map_local / modify_headers / modify_body 时，
# 只要在这里补上对应文案，表格和对话框的下拉都会自动多出一项。
KIND_LABELS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: "重定向",
}

# 与屏蔽页、抓包过滤条的措辞保持一致。
LOGIC_LABELS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: "包含",
    RewriteLogic.EQUALS: "等于",
    RewriteLogic.REGEX: "正则表达式",
}


def kind_label(kind: RewriteKind) -> str:
    return KIND_LABELS.get(kind, str(kind))


def logic_label(logic: RewriteLogic) -> str:
    return LOGIC_LABELS.get(logic, str(logic))


def rule_summary(rule: RewriteRule) -> str:
    """一行描述这条规则实际交给 `re.sub` 的两个参数；不可用则返回原因。"""
    try:
        return f"{rule.subject}  →  {rule.template}"
    except ValueError as exc:
        return str(exc)


class RewriteRuleTableModel(QAbstractTableModel):
    """规则列表。顺序即优先级：原生 MapRemote 会按 spec 顺序**逐条**改写同一个
    URL（不是命中即停），所以行序是有语义的，不开排序。"""

    HEADERS: ClassVar[list[str]] = ["启用", "类型", "匹配方式", "原始 URL", "重写为"]

    enabled_toggled = Signal(int, bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._rules: list[RewriteRule] = []

    def set_rules(self, rules: list[RewriteRule]) -> None:
        self.beginResetModel()
        self._rules = list(rules)
        self.endResetModel()

    def rule_at(self, row: int) -> RewriteRule | None:
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
                return kind_label(rule.kind)
            if col == 2:
                return logic_label(rule.logic)
            if col == 3:
                return rule.value
            if col == 4:
                return rule.replacement
            return None

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            return Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.ToolTipRole:
            return rule_summary(rule)

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


class RewriteRuleFilterProxyModel(QSortFilterProxyModel):
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
        if not isinstance(model, RewriteRuleTableModel):
            return True
        rule = model.rule_at(source_row)
        if rule is None:
            return True
        haystack = " ".join(
            (
                rule.value,
                rule.replacement,
                kind_label(rule.kind),
                logic_label(rule.logic),
            )
        ).lower()
        return self._filter_text in haystack
