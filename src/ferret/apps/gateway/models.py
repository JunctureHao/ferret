"""Table model and UI labels for gateway rules."""

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

from ferret.core.mitm import (
    GATEWAY_STATUS_CLOSE,
    GatewayField,
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
)

LAYER_LABELS: dict[GatewayLayer, str] = {
    GatewayLayer.L4: "传输层",
    GatewayLayer.L7: "应用层",
}

POLICY_LABELS: dict[GatewayPolicy, str] = {
    GatewayPolicy.ALLOW_ONLY: "仅允许",
    GatewayPolicy.BYPASS: "绕行",
    GatewayPolicy.BLOCK: "屏蔽",
    GatewayPolicy.BLOCK_OUT: "屏蔽（出）",
    GatewayPolicy.BLOCK_IN: "屏蔽（入）",
    GatewayPolicy.SUSPEND_OUT: "挂起（出）",
    GatewayPolicy.SUSPEND_IN: "挂起（入）",
}

# 策略说明。措辞对齐 core/mitm/gateway.py 里各策略实际落地的机制，别写成愿望。
POLICY_HINTS: dict[GatewayPolicy, str] = {
    GatewayPolicy.ALLOW_ONLY: "白名单：只抓取命中的流量，其余一律绕行（不是丢弃）。",
    GatewayPolicy.BYPASS: "命中的流量照常发往服务器，但不抓包、不进流量列表。",
    GatewayPolicy.BLOCK: "命中的连接到不了服务器，也不会产生流量记录。",
    GatewayPolicy.BLOCK_OUT: "拦住发往服务器的请求，直接按下面的响应回给客户端。",
    GatewayPolicy.BLOCK_IN: "服务器的响应已经回来了，但不转发给客户端，直接断开。",
    GatewayPolicy.SUSPEND_OUT: "请求发出前挂住不放，模拟超时；不断连，直到规则变更或关掉总开关。",
    GatewayPolicy.SUSPEND_IN: "响应回来后挂住不转发，模拟超时；不断连，直到规则变更或关掉总开关。",
}

FIELD_LABELS: dict[GatewayField, str] = {
    GatewayField.HOST: "主机",
    GatewayField.METHOD: "方法",
}

# 与抓包过滤条的措辞保持一致（apps/capture/services.py 的 _condition_to_expr）。
LOGIC_LABELS: dict[GatewayLogic, str] = {
    GatewayLogic.CONTAINS: "包含",
    GatewayLogic.EQUALS: "等于",
    GatewayLogic.REGEX: "正则表达式",
}

STATUS_LABELS: dict[int, str] = {
    403: "403 Forbidden",
    404: "404 Not Found",
    451: "451 Unavailable For Legal Reasons",
    502: "502 Bad Gateway",
    GATEWAY_STATUS_CLOSE: f"{GATEWAY_STATUS_CLOSE} 直接断开连接",
}

STATUS_CHOICES: list[int] = [403, 404, 451, 502, GATEWAY_STATUS_CLOSE]

# 只有「屏蔽（出）」会把状态码回给客户端，其余策略这一列没有意义。
_STATUS_POLICIES: frozenset[GatewayPolicy] = frozenset({GatewayPolicy.BLOCK_OUT})

_NO_VALUE = "—"


def layer_label(layer: GatewayLayer) -> str:
    return LAYER_LABELS.get(layer, str(layer))


def policy_label(policy: GatewayPolicy) -> str:
    return POLICY_LABELS.get(policy, str(policy))


def policy_hint(policy: GatewayPolicy) -> str:
    return POLICY_HINTS.get(policy, "")


def field_label(field: GatewayField) -> str:
    return FIELD_LABELS.get(field, str(field))


def logic_label(logic: GatewayLogic) -> str:
    return LOGIC_LABELS.get(logic, str(logic))


def status_label(status_code: int) -> str:
    return STATUS_LABELS.get(status_code, str(status_code))


def uses_status(policy: GatewayPolicy) -> bool:
    """这条策略是否需要用户选一个响应状态码。"""
    return policy in _STATUS_POLICIES


def rule_summary(rule: GatewayRule) -> str:
    """一行描述这条规则实际下发的匹配正则；不可用则返回原因。"""
    try:
        return f"{policy_hint(rule.policy)}\n匹配正则：{rule.pattern}"
    except ValueError as exc:
        return str(exc)


class GatewayRuleTableModel(QAbstractTableModel):
    """规则列表。顺序是**同优先级内**的裁决顺序（策略优先级更高，见
    core/mitm/gateway.py 的 `_POLICY_PRIORITY`），所以行序有语义，不开排序。"""

    HEADERS: ClassVar[list[str]] = [
        "启用",
        "层",
        "策略",
        "匹配对象",
        "条件",
        "值",
        "响应",
    ]

    enabled_toggled = Signal(int, bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._rules: list[GatewayRule] = []

    def set_rules(self, rules: list[GatewayRule]) -> None:
        self.beginResetModel()
        self._rules = list(rules)
        self.endResetModel()

    def rule_at(self, row: int) -> GatewayRule | None:
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
                return layer_label(rule.layer)
            if col == 2:
                return policy_label(rule.policy)
            if col == 3:
                return field_label(rule.field)
            if col == 4:
                return logic_label(rule.logic)
            if col == 5:
                return rule.value
            if col == 6:
                if not uses_status(rule.policy):
                    return _NO_VALUE
                return status_label(rule.status_code)
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


class GatewayRuleFilterProxyModel(QSortFilterProxyModel):
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
        if not isinstance(model, GatewayRuleTableModel):
            return True
        rule = model.rule_at(source_row)
        if rule is None:
            return True
        haystack = " ".join(
            (
                rule.value,
                layer_label(rule.layer),
                policy_label(rule.policy),
                field_label(rule.field),
                logic_label(rule.logic),
            )
        ).lower()
        return self._filter_text in haystack
