"""Table model and UI labels for gateway rules."""

from dataclasses import replace
from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QCoreApplication,
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
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 文案表只存标记、不求值 —— 模块级求值赶在翻译器安装之前（`core/application.py`
# 顶层就 import 了 MainWindow），译文会永久冻结成英文。求值在下面那几个
# `*_label()` 里做（见 `ferret.utils.i18n`）。
LAYER_LABELS: dict[GatewayLayer, str] = {
    GatewayLayer.L4: QT_TRANSLATE_NOOP("GatewayLayer", "Transport layer"),
    GatewayLayer.L7: QT_TRANSLATE_NOOP("GatewayLayer", "Application layer"),
}

POLICY_LABELS: dict[GatewayPolicy, str] = {
    GatewayPolicy.ALLOW_ONLY: QT_TRANSLATE_NOOP("GatewayPolicy", "Allow only"),
    GatewayPolicy.BYPASS: QT_TRANSLATE_NOOP("GatewayPolicy", "Bypass"),
    GatewayPolicy.BLOCK: QT_TRANSLATE_NOOP("GatewayPolicy", "Block"),
    GatewayPolicy.BLOCK_OUT: QT_TRANSLATE_NOOP("GatewayPolicy", "Block (outbound)"),
    GatewayPolicy.BLOCK_IN: QT_TRANSLATE_NOOP("GatewayPolicy", "Block (inbound)"),
    GatewayPolicy.SUSPEND_OUT: QT_TRANSLATE_NOOP("GatewayPolicy", "Suspend (outbound)"),
    GatewayPolicy.SUSPEND_IN: QT_TRANSLATE_NOOP("GatewayPolicy", "Suspend (inbound)"),
}

# 策略说明。措辞对齐 core/mitm/gateway.py 里各策略实际落地的机制，别写成愿望。
POLICY_HINTS: dict[GatewayPolicy, str] = {
    GatewayPolicy.ALLOW_ONLY: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Allow list: only matching traffic is captured, everything else is bypassed (not dropped).",
    ),
    GatewayPolicy.BYPASS: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Matching traffic still reaches the server, but it is not captured and never enters the flow list.",
    ),
    GatewayPolicy.BLOCK: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Matching connections never reach the server and leave no flow record.",
    ),
    GatewayPolicy.BLOCK_OUT: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Hold the request back from the server and answer the client with the response below.",
    ),
    GatewayPolicy.BLOCK_IN: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "The server response already arrived, but it is not forwarded to the client; the connection is closed instead.",
    ),
    GatewayPolicy.SUSPEND_OUT: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Hold the request before it is sent to fake a timeout; the connection stays open until the rule changes or the master switch goes off.",
    ),
    GatewayPolicy.SUSPEND_IN: QT_TRANSLATE_NOOP(
        "GatewayPolicy",
        "Hold the response instead of forwarding it to fake a timeout; the connection stays open until the rule changes or the master switch goes off.",
    ),
}

FIELD_LABELS: dict[GatewayField, str] = {
    GatewayField.HOST: QT_TRANSLATE_NOOP("GatewayField", "Host"),
    GatewayField.METHOD: QT_TRANSLATE_NOOP("GatewayField", "Method"),
}

# 与抓包过滤条的措辞保持一致（apps/capture/services.py 的 _condition_to_expr）。
LOGIC_LABELS: dict[GatewayLogic, str] = {
    GatewayLogic.CONTAINS: QT_TRANSLATE_NOOP("GatewayLogic", "Contains"),
    GatewayLogic.EQUALS: QT_TRANSLATE_NOOP("GatewayLogic", "Equals"),
    GatewayLogic.REGEX: QT_TRANSLATE_NOOP("GatewayLogic", "Regex"),
}

# 前四条是 HTTP 的 reason phrase，本身就是协议原文，不翻；最后一条不是状态码，是
# 「直接断连」的哨兵值，要翻（`{}` 留给哨兵值本身 —— f-string 会让 lupdate 看不见文案）。
STATUS_LABELS: dict[int, str] = {
    403: "403 Forbidden",
    404: "404 Not Found",
    451: "451 Unavailable For Legal Reasons",
    502: "502 Bad Gateway",
    GATEWAY_STATUS_CLOSE: QT_TRANSLATE_NOOP("GatewayStatus", "{} Close the connection"),
}

STATUS_CHOICES: list[int] = [403, 404, 451, 502, GATEWAY_STATUS_CLOSE]

# 只有「屏蔽（出）」会把状态码回给客户端，其余策略这一列没有意义。
_STATUS_POLICIES: frozenset[GatewayPolicy] = frozenset({GatewayPolicy.BLOCK_OUT})

_NO_VALUE = "—"


def layer_label(layer: GatewayLayer) -> str:
    return resolve_marker(LAYER_LABELS, layer, "GatewayLayer", str(layer))


def policy_label(policy: GatewayPolicy) -> str:
    return resolve_marker(POLICY_LABELS, policy, "GatewayPolicy", str(policy))


def policy_hint(policy: GatewayPolicy) -> str:
    return resolve_marker(POLICY_HINTS, policy, "GatewayPolicy")


def field_label(field: GatewayField) -> str:
    return resolve_marker(FIELD_LABELS, field, "GatewayField", str(field))


def logic_label(logic: GatewayLogic) -> str:
    return resolve_marker(LOGIC_LABELS, logic, "GatewayLogic", str(logic))


def status_label(status_code: int) -> str:
    if status_code == GATEWAY_STATUS_CLOSE:
        return resolve_marker(STATUS_LABELS, status_code, "GatewayStatus").format(
            GATEWAY_STATUS_CLOSE
        )
    return STATUS_LABELS.get(status_code, str(status_code))


def uses_status(policy: GatewayPolicy) -> bool:
    """这条策略是否需要用户选一个响应状态码。"""
    return policy in _STATUS_POLICIES


def rule_summary(rule: GatewayRule) -> str:
    """一行描述这条规则实际下发的匹配正则；不可用则返回原因。"""
    try:
        pattern = rule.pattern
    except ValueError as exc:
        return str(exc)
    # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
    matched = QCoreApplication.translate("GatewayRule", "Match pattern: {}").format(
        pattern
    )
    return f"{policy_hint(rule.policy)}\n{matched}"


class GatewayRuleTableModel(QAbstractTableModel):
    """规则列表。顺序是**同优先级内**的裁决顺序（策略优先级更高，见
    core/mitm/gateway.py 的 `_POLICY_PRIORITY`），所以行序有语义，不开排序。"""

    # 同理只做标记：类体也是导入期就求值的。求值在 `headerData()` 里做。
    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Enabled"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Layer"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Policy"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Match on"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Condition"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Value"),
        QT_TRANSLATE_NOOP("GatewayRuleTableModel", "Response"),
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
            return QCoreApplication.translate(
                "GatewayRuleTableModel", self.HEADERS[section]
            )
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
