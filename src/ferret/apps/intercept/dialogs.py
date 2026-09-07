"""Breakpoint dialogs: the rule form, and the confirm shown when flows are still held."""

from enum import StrEnum

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
)

from ferret.apps.intercept.models import (
    field_hint,
    field_label,
    logic_label,
    phase_hint,
    phase_label,
)
from ferret.core.mitm import (
    InterceptField,
    InterceptLogic,
    InterceptPhase,
    InterceptRule,
)

_FIELDS: list[InterceptField] = list(InterceptField)
_LOGICS: list[InterceptLogic] = list(InterceptLogic)
_PHASES: list[InterceptPhase] = list(InterceptPhase)

_PLACEHOLDERS: dict[tuple[InterceptField, InterceptLogic], str] = {
    (InterceptField.URL, InterceptLogic.CONTAINS): "/v1/user",
    (InterceptField.URL, InterceptLogic.EQUALS): "https://api.example.com/v1/user",
    (InterceptField.URL, InterceptLogic.REGEX): r"^https://api\.example\.com/v1/.*",
    (InterceptField.HOST, InterceptLogic.CONTAINS): "example.com",
    (InterceptField.HOST, InterceptLogic.EQUALS): "api.example.com",
    (InterceptField.HOST, InterceptLogic.REGEX): r".*\.example\.com$",
    (InterceptField.METHOD, InterceptLogic.CONTAINS): "POST",
    (InterceptField.METHOD, InterceptLogic.EQUALS): "POST",
    (InterceptField.METHOD, InterceptLogic.REGEX): "POST|PUT|PATCH",
}


class InterceptRuleDialog(MessageBoxBase):
    """断点规则表单：匹配对象 + 条件 + 值 + 阶段。

    「阶段」决定命中的流量停几次：请求（只停请求期）/ 响应（只停响应期）/
    请求和响应（默认，两期各停一次），经 `intercept_expression` 编译成段外的
    ``~q`` / ``~s`` 选择器。

    合法性一律交给原生 flowfilter 解析器拍板（`InterceptRule.validate` 内部跑
    `parse_filter`），过不了就不让保存 —— `options.update` 是原子的，一条坏表达式
    会让整批规则回滚。
    """

    def __init__(
        self,
        title: str,
        rule: InterceptRule | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._rule = rule if rule is not None else InterceptRule()
        self.__init_widget(title)
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._sync_texts()
        self._validate()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.field_combo = ComboBox(self)
        self.field_combo.addItems([field_label(field) for field in _FIELDS])
        self.field_combo.setCurrentIndex(self._index_of(_FIELDS, self._rule.field))

        self.logic_combo = ComboBox(self)
        self.logic_combo.addItems([logic_label(logic) for logic in _LOGICS])
        self.logic_combo.setCurrentIndex(self._index_of(_LOGICS, self._rule.logic))

        self.phase_combo = ComboBox(self)
        self.phase_combo.addItems([phase_label(phase) for phase in _PHASES])
        self.phase_combo.setCurrentIndex(self._index_of(_PHASES, self._rule.phase))

        self.value_edit = LineEdit(self)
        self.value_edit.setText(self._rule.value)
        self.value_edit.setClearButtonEnabled(True)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setWordWrap(True)

        self.preview_label = CaptionLabel(self)
        self.preview_label.setWordWrap(True)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        QTimer.singleShot(0, self.value_edit.setFocus)

    def __init_layout(self):
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("匹配对象"), self), self.field_combo)
        form.addRow(BodyLabel(self.tr("条件"), self), self.logic_combo)
        form.addRow(BodyLabel(self.tr("匹配值"), self), self.value_edit)
        form.addRow(BodyLabel(self.tr("阶段"), self), self.phase_combo)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addLayout(form)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.preview_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(520)

    def __connect_signal_to_slot(self):
        self.field_combo.currentIndexChanged.connect(self._on_choice_changed)
        self.logic_combo.currentIndexChanged.connect(self._on_choice_changed)
        self.phase_combo.currentIndexChanged.connect(self._on_choice_changed)
        self.value_edit.textChanged.connect(self._validate)

    @staticmethod
    def _index_of(items: list, value) -> int:
        for i, item in enumerate(items):
            if item == value:
                return i
        return 0

    def _current_field(self) -> InterceptField:
        return _FIELDS[max(self.field_combo.currentIndex(), 0)]

    def _current_logic(self) -> InterceptLogic:
        return _LOGICS[max(self.logic_combo.currentIndex(), 0)]

    def _current_phase(self) -> InterceptPhase:
        return _PHASES[max(self.phase_combo.currentIndex(), 0)]

    def _sync_texts(self) -> None:
        field, logic = self._current_field(), self._current_logic()
        self.value_edit.setPlaceholderText(_PLACEHOLDERS.get((field, logic), ""))
        hints = [phase_hint(self._current_phase()), field_hint(field)]
        self.hint_label.setText("\n".join(hint for hint in hints if hint))

    def _on_choice_changed(self):
        self._sync_texts()
        self._validate()

    def get_rule(self) -> InterceptRule:
        return InterceptRule(
            field=self._current_field(),
            logic=self._current_logic(),
            value=self.value_edit.text().strip(),
            enabled=self._rule.enabled,
            phase=self._current_phase(),
        )

    def _validate(self):
        rule = self.get_rule()
        try:
            rule.validate()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        # 预览给的是真正要下发的那截表达式，多条规则会被 `|` 连起来。
        self.preview_label.setText(self.tr("匹配表达式：{}").format(rule.expression))
        self.yesButton.setEnabled(True)


class HeldFlowsChoice(StrEnum):
    """关掉断点窗口时，那些还攥在手里的流量怎么办。"""

    KEEP_HELD = "keep_held"
    CANCEL = "cancel"


class HeldFlowsCloseDialog(MessageBoxBase):
    """队列非空时关窗的二选一确认。

    不能默默关掉：窗口一藏，那几条流量还钉在 `wait_for_resume()` 上，客户端就那么
    转圈，而界面上再也看不到它们（`handle_hook` 在等待期间 `disarm()` 了看门狗，
    `tcp_timeout` 不会兜底）。所以「保持挂起」也是一个**明示**的选择，不是默认行为。

    结果读 :attr:`choice` 而不是 `exec()` 的真假：「保持挂起」也算关得掉。
    """

    def __init__(self, flow_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.choice = HeldFlowsChoice.CANCEL

        self.title_label = SubtitleLabel(
            self.tr("还有 {} 条流量挂着").format(flow_count), self
        )
        self.desc_label = BodyLabel(
            self.tr(
                "挂起中的流量不会自己超时放行，客户端会一直等。保持挂起的话，可以从断点页的「拦截队列」再打开这个窗口。"
            ),
            self,
        )
        self.desc_label.setWordWrap(True)

        self.yesButton.setText(self.tr("保持挂起并隐藏"))
        self.cancelButton.setText(self.tr("取消"))

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(420)

    def validate(self) -> bool:
        """「保持挂起」这一路的记账点。

        刻意用这个钩子而不是再给 `yesButton.clicked` 挂一个槽：基类在
        `__onYesButtonClicked` 里是先 `validate()` 再 `accept()`，记账稳稳排在关窗
        之前；反过来挂槽就要赌两个槽的调用顺序压过 `accept()`。
        """
        self.choice = HeldFlowsChoice.KEEP_HELD
        return True
