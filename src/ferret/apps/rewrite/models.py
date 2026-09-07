"""Table model and UI labels for rewrite rules."""

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
    BODY_KINDS,
    FILE_REPLACEMENT_PREFIX,
    HEADER_KINDS,
    MAP_KINDS,
    WHOLE_BODY_PATTERN,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 每个 RewriteKind 一条，表格的类型列与对话框的类型下拉都由它生成 —— 再加一种
# 重写类型时，`RewriteKind` 补成员、这里补文案，界面自动多出一项。
# 文案表只存标记、不求值 —— 模块级求值赶在翻译器安装之前（`core/application.py`
# 顶层就 import 了 MainWindow），译文会永久冻结成英文。求值在下面那几个函数里做
# （见 `ferret.utils.i18n`）。
KIND_LABELS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: QT_TRANSLATE_NOOP("RewriteKind", "重定向（远程）"),
    RewriteKind.MAP_LOCAL: QT_TRANSLATE_NOOP("RewriteKind", "重定向（本地）"),
    RewriteKind.MODIFY_REQUEST_HEADER: QT_TRANSLATE_NOOP("RewriteKind", "请求头"),
    RewriteKind.MODIFY_RESPONSE_HEADER: QT_TRANSLATE_NOOP("RewriteKind", "响应头"),
    RewriteKind.MODIFY_REQUEST_BODY: QT_TRANSLATE_NOOP("RewriteKind", "请求体"),
    RewriteKind.MODIFY_RESPONSE_BODY: QT_TRANSLATE_NOOP("RewriteKind", "响应体"),
}

# 「目标」一栏在六种类型里指三样不同的东西，列头只能给个中性名字，具体含义靠这里
# 的行内文案和对话框的动态标签讲清楚。
TARGET_LABELS: dict[RewriteKind, str] = {
    RewriteKind.MODIFY_REQUEST_HEADER: QT_TRANSLATE_NOOP("RewriteFields", "头名称"),
    RewriteKind.MODIFY_RESPONSE_HEADER: QT_TRANSLATE_NOOP("RewriteFields", "头名称"),
    RewriteKind.MODIFY_REQUEST_BODY: QT_TRANSLATE_NOOP("RewriteFields", "体正则"),
    RewriteKind.MODIFY_RESPONSE_BODY: QT_TRANSLATE_NOOP("RewriteFields", "体正则"),
}

REPLACEMENT_LABELS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: QT_TRANSLATE_NOOP("RewriteFields", "重写为"),
    RewriteKind.MAP_LOCAL: QT_TRANSLATE_NOOP("RewriteFields", "本地文件或目录"),
    RewriteKind.MODIFY_REQUEST_HEADER: QT_TRANSLATE_NOOP("RewriteFields", "头值"),
    RewriteKind.MODIFY_RESPONSE_HEADER: QT_TRANSLATE_NOOP("RewriteFields", "头值"),
    RewriteKind.MODIFY_REQUEST_BODY: QT_TRANSLATE_NOOP("RewriteFields", "新内容"),
    RewriteKind.MODIFY_RESPONSE_BODY: QT_TRANSLATE_NOOP("RewriteFields", "新内容"),
}

# 与屏蔽页、抓包过滤条的措辞保持一致。
LOGIC_LABELS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: QT_TRANSLATE_NOOP("RewriteLogic", "包含"),
    RewriteLogic.EQUALS: QT_TRANSLATE_NOOP("RewriteLogic", "等于"),
    RewriteLogic.REGEX: QT_TRANSLATE_NOOP("RewriteLogic", "正则表达式"),
}


def kind_label(kind: RewriteKind) -> str:
    return resolve_marker(KIND_LABELS, kind, "RewriteKind", str(kind))


def logic_label(logic: RewriteLogic) -> str:
    return resolve_marker(LOGIC_LABELS, logic, "RewriteLogic", str(logic))


def target_field_label(kind: RewriteKind) -> str:
    """「目标」栏的标签。重定向两类用不上这一栏，落到中性名字上。"""
    return resolve_marker(
        TARGET_LABELS,
        kind,
        "RewriteFields",
        QCoreApplication.translate("RewriteFields", "目标"),
    )


def replacement_field_label(kind: RewriteKind) -> str:
    """「重写为」栏的标签。"""
    return resolve_marker(
        REPLACEMENT_LABELS,
        kind,
        "RewriteFields",
        QCoreApplication.translate("RewriteFields", "重写为"),
    )


def target_display(rule: RewriteRule) -> str:
    """「目标」列的显示文本：头名 / 体正则；重定向两类用不上这一栏。

    体正则整栏留空时显示原生会真的下发的那条整体匹配正则，而不是一片空白 ——
    否则「整体替换」和「还没填」在表格里长得一模一样。
    """
    if rule.kind in HEADER_KINDS:
        return rule.target.strip()
    if rule.kind in BODY_KINDS:
        return rule.target if rule.target.strip() else WHOLE_BODY_PATTERN
    return ""


def replacement_display(rule: RewriteRule) -> str:
    """「重写为」列的显示文本，空值按各类型的实际语义写成人话。

    头/体两类的空替换串是**合法且有意义**的：原生 `ModifyHeaders.run` 先 pop 同名头、
    只在替换串非空时才 add 回去（空 = 删掉这个头）；`ModifyBody.run` 的
    `re.sub` 把匹配段换成空串（空 = 清掉这段内容）。
    """
    if rule.replacement:
        reads_file = rule.kind not in MAP_KINDS and rule.replacement.startswith(
            FILE_REPLACEMENT_PREFIX
        )
        if reads_file:
            # 原生 `ModifySpec.read_replacement` 会把 `@` 之后的部分当文件路径读取；
            # 重定向两类没有这层语义，`@` 在它们那儿就是普通字符。
            # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
            return QCoreApplication.translate("RewriteRule", "读取文件 {}").format(
                rule.replacement[1:]
            )
        return rule.replacement
    if rule.kind in HEADER_KINDS:
        return QCoreApplication.translate("RewriteRule", "（删除该头）")
    if rule.kind in BODY_KINDS:
        return QCoreApplication.translate("RewriteRule", "（清空）")
    return ""


def rule_summary(rule: RewriteRule) -> str:
    """一行描述这条规则实际会做什么；填不全或写坏了则返回原因。

    重定向两类走 `rule.template`（`re.sub` 的替换串），头/体两类刻意**不**走 ——
    `template` 把空替换串一律判成「重写目标不能为空」，而那两类的空替换串是合法的
    删除/清空语义（见 `replacement_display`）。
    """
    try:
        subject = rule.subject
        if rule.kind == RewriteKind.MAP_REMOTE:
            return f"{subject}  →  {rule.template}"
        if rule.kind == RewriteKind.MAP_LOCAL:
            path = rule.replacement.strip()
            if not path:
                raise ValueError(
                    QCoreApplication.translate("RewriteRule", "本地文件或目录不能为空")
                )
            return f"{subject}  →  {path}"
        if rule.kind in HEADER_KINDS and not rule.target.strip():
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求头/响应头名称不能为空")
            )
    except ValueError as exc:
        return str(exc)
    return f"{subject}  →  {target_display(rule)} = {replacement_display(rule)}"


class RewriteRuleTableModel(QAbstractTableModel):
    """规则列表。顺序即优先级：四个原生 addon 都按 spec 顺序**逐条**作用于同一条
    流量（不是命中即停），所以行序是有语义的，不开排序。"""

    # 同理只做标记：类体也是导入期就求值的。求值在 `headerData()` 里做。
    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "启用"),
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "类型"),
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "匹配方式"),
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "匹配 URL"),
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "目标"),
        QT_TRANSLATE_NOOP("RewriteRuleTableModel", "重写为"),
    ]

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
            return QCoreApplication.translate(
                "RewriteRuleTableModel", self.HEADERS[section]
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
                return kind_label(rule.kind)
            if col == 2:
                return logic_label(rule.logic)
            if col == 3:
                return rule.value
            if col == 4:
                return target_display(rule)
            if col == 5:
                return replacement_display(rule)
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
                rule.target,
                rule.replacement,
                kind_label(rule.kind),
                logic_label(rule.logic),
            )
        ).lower()
        return self._filter_text in haystack
