"""Table models and UI labels for breakpoints: the rule list and the held queue."""

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
    HTTPFlow,
    InterceptField,
    InterceptLogic,
    InterceptPhase,
    InterceptRule,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 只剩两个，而且只用来标注「队列里这条停在哪」—— 规则不再选阶段，见 `InterceptPhase`。
PHASE_LABELS: dict[InterceptPhase, str] = {
    InterceptPhase.REQUEST: QT_TRANSLATE_NOOP("InterceptPhase", "Request phase"),
    InterceptPhase.RESPONSE: QT_TRANSLATE_NOOP("InterceptPhase", "Response phase"),
}


# 所有规则的行为都一样，所以是一句固定说明而不是一张按阶段分的表。措辞对齐实际下发的
# 表达式（`InterceptRule.expression` 不带 ~q / ~s），别写成愿望。
def both_phases_hint() -> str:
    return QCoreApplication.translate(
        "InterceptRule",
        "Matching traffic is held twice: once before the request goes out (you can edit the request, or answer the client with a faked response instead of sending it to the server), and once again after it is released and the response comes back (only the response can be edited then).",
    )


FIELD_LABELS: dict[InterceptField, str] = {
    InterceptField.URL: QT_TRANSLATE_NOOP("InterceptField", "URL"),
    InterceptField.HOST: QT_TRANSLATE_NOOP("InterceptField", "Host"),
    InterceptField.METHOD: QT_TRANSLATE_NOOP("InterceptField", "Method"),
}

# 与网关页、重写页的措辞逐字一致（三页各有自己的 context，译文必须填同一个词）。
# 只存标记：模块级求值赶在翻译器安装之前，求值推到 `logic_label()`。
LOGIC_LABELS: dict[InterceptLogic, str] = {
    InterceptLogic.CONTAINS: QT_TRANSLATE_NOOP("InterceptLogic", "Contains"),
    InterceptLogic.EQUALS: QT_TRANSLATE_NOOP("InterceptLogic", "Equals"),
    InterceptLogic.REGEX: QT_TRANSLATE_NOOP("InterceptLogic", "Regex"),
}

# 主机与方法走原生 ~d / ~m，两个都不带端口；URL 走 ~u，含协议、端口和查询串。
FIELD_HINTS: dict[InterceptField, str] = {
    InterceptField.URL: QT_TRANSLATE_NOOP(
        "InterceptField",
        "Matches the whole URL, including scheme, port and query string.",
    ),
    InterceptField.HOST: QT_TRANSLATE_NOOP(
        "InterceptField", "Matches the host name without the port; case-insensitive."
    ),
    InterceptField.METHOD: QT_TRANSLATE_NOOP(
        "InterceptField", "Matches the request method; case-insensitive."
    ),
}

_WAITING = QT_TRANSLATE_NOOP("HeldFlowTableModel", "Waiting for response")
_EDITED = QT_TRANSLATE_NOOP("HeldFlowTableModel", "Edited")


def phase_label(phase: InterceptPhase) -> str:
    return resolve_marker(PHASE_LABELS, phase, "InterceptPhase", str(phase))


def field_label(field: InterceptField) -> str:
    return resolve_marker(FIELD_LABELS, field, "InterceptField", str(field))


def field_hint(field: InterceptField) -> str:
    return resolve_marker(FIELD_HINTS, field, "InterceptField")


def logic_label(logic: InterceptLogic) -> str:
    return resolve_marker(LOGIC_LABELS, logic, "InterceptLogic", str(logic))


def rule_summary(rule: InterceptRule) -> str:
    """一行描述这条规则实际下发的表达式；填不全或写坏了则返回原因。

    走 `validate()` 而不是只读 `expression`：拼表达式这一步不碰正则引擎，
    ``~u "bad("`` 能一路拼出来，只有原生 `parse_filter` 才认得出它是坏的 ——
    而下发时炸掉的正是那一步（还会把整批规则连坐回滚）。
    """
    try:
        rule.validate()
    except ValueError as exc:
        return str(exc)
    # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
    expression = QCoreApplication.translate(
        "InterceptRule", "Match expression: {}"
    ).format(rule.expression)
    return f"{both_phases_hint()}\n{expression}"


def held_phase_label(flow: HTTPFlow) -> str:
    """这条流量停在哪个阶段。判据和原生 ~q / ~s 完全一致：有没有响应。"""
    return phase_label(
        InterceptPhase.RESPONSE if flow.response is not None else InterceptPhase.REQUEST
    )


def held_edited(flow: HTTPFlow) -> bool:
    """这条流量是否有可撤销的改动。

    原生 `Flow.modified()` 一旦 `backup()` 过就恒为 True（它拿 `_backup` 和
    `get_state()` 比，而后者必然多带一个内容不同的 backup 键），所以它的实际语义是
    「有备份、撤销得回去」—— 而这恰好就是界面要标的东西：`apply_request_edit` /
    `apply_response_edit` / `fake_response` 写回前都会 `backup()`，`revert()` 成功后
    又把备份清掉。`flow.copy()` 会把备份一起带过来，所以拿 Qt 线程上的快照判也准。
    """
    return bool(flow.modified())


def held_status(flow: HTTPFlow) -> str:
    """「状态」列：响应期给状态码，请求期还没有响应可给。"""
    text = (
        QCoreApplication.translate("HeldFlowTableModel", _WAITING)
        if flow.response is None
        else str(flow.response.status_code)
    )
    if not held_edited(flow):
        return text
    edited = QCoreApplication.translate("HeldFlowTableModel", _EDITED)
    return f"{text} · {edited}"


class InterceptRuleTableModel(QAbstractTableModel):
    """断点规则列表。

    行序没有语义，所以不给上移/下移：所有启用的规则会被 `|` 连成一条 flowfilter
    表达式（见 `intercept_expression`），命中任意一条就拦，先后无别。这与网关页/
    重写页刻意不同 —— 那两页的行序分别决定裁决顺序和逐条改写顺序。
    """

    # 同理只做标记：类体也是导入期就求值的。求值在 `headerData()` 里做。
    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("InterceptRuleTableModel", "Enabled"),
        QT_TRANSLATE_NOOP("InterceptRuleTableModel", "Match on"),
        QT_TRANSLATE_NOOP("InterceptRuleTableModel", "Condition"),
        QT_TRANSLATE_NOOP("InterceptRuleTableModel", "Value"),
    ]

    enabled_toggled = Signal(int, bool)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._rules: list[InterceptRule] = []

    def set_rules(self, rules: list[InterceptRule]) -> None:
        self.beginResetModel()
        self._rules = list(rules)
        self.endResetModel()

    def rule_at(self, row: int) -> InterceptRule | None:
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
                "InterceptRuleTableModel", self.HEADERS[section]
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
                return field_label(rule.field)
            if col == 2:
                return logic_label(rule.logic)
            if col == 3:
                return rule.value
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


class InterceptRuleFilterProxyModel(QSortFilterProxyModel):
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
        if not isinstance(model, InterceptRuleTableModel):
            return True
        rule = model.rule_at(source_row)
        if rule is None:
            return True
        haystack = " ".join(
            (
                rule.value,
                field_label(rule.field),
                logic_label(rule.logic),
            )
        ).lower()
        return self._filter_text in haystack


class HeldFlowTableModel(QAbstractTableModel):
    """拦截队列。装的是 `MitmFacade.intercepted_flows()` 给的 `flow.copy()` 快照。

    整表重置而不做增量：队列本来就短（`INTERCEPT_LIMIT` 是 128，实际同时拦下的是
    个位数），而每次刷新拿到的都是一批新的快照对象，增量比对反而要按 id 手工对齐。
    """

    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("HeldFlowTableModel", "Phase"),
        QT_TRANSLATE_NOOP("HeldFlowTableModel", "Method"),
        QT_TRANSLATE_NOOP("HeldFlowTableModel", "URL"),
        QT_TRANSLATE_NOOP("HeldFlowTableModel", "Status"),
    ]

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._flows: list[HTTPFlow] = []

    def set_flows(self, flows: list[HTTPFlow]) -> None:
        self.beginResetModel()
        self._flows = list(flows)
        self.endResetModel()

    def flow_at(self, row: int) -> HTTPFlow | None:
        if 0 <= row < len(self._flows):
            return self._flows[row]
        return None

    def row_of(self, flow_id: str) -> int:
        """按 id 找行；刷新后快照换了对象，只有 id 是稳定的。"""
        for row, flow in enumerate(self._flows):
            if flow.id == flow_id:
                return row
        return -1

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
                "HeldFlowTableModel", self.HEADERS[section]
            )
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._flows)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self.HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._flows)):
            return None

        flow = self._flows[row]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return held_phase_label(flow)
            if col == 1:
                return flow.request.method
            if col == 2:
                return flow.request.pretty_url
            if col == 3:
                return held_status(flow)
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            return flow.request.pretty_url

        if role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.UserRole:
            return flow

        return None
