"""Gateway-rule editing dialog."""

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFormLayout,
    QGridLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    LineEdit,
    MessageBoxBase,
    RadioButton,
    SegmentedWidget,
    SubtitleLabel,
)

from ferret.apps.gateway.models import (
    STATUS_CHOICES,
    field_label,
    layer_label,
    logic_label,
    policy_hint,
    policy_label,
    status_label,
    uses_status,
)
from ferret.core.mitm import (
    LAYER_POLICIES,
    GatewayField,
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
)

_LAYERS: list[GatewayLayer] = list(GatewayLayer)
_LOGICS: list[GatewayLogic] = list(GatewayLogic)

# 每层能按什么匹配。L4 只有主机：连接还没有 HTTP 语义，拿不到方法
# （`GatewayRule.validate` 会直接拒掉 L4 + 方法）。
_LAYER_FIELDS: dict[GatewayLayer, list[GatewayField]] = {
    GatewayLayer.L4: [GatewayField.HOST],
    GatewayLayer.L7: [GatewayField.HOST, GatewayField.METHOD],
}

_PLACEHOLDERS: dict[GatewayField, str] = {
    GatewayField.HOST: "example.com",
    GatewayField.METHOD: "POST",
}

_LAYER_HINTS: dict[GatewayLayer, str] = {
    GatewayLayer.L4: "作用在连接上，命中的流量压根不会成为一条记录。仅对 HTTPS/CONNECT 完整生效。",
    GatewayLayer.L7: "作用在每条 HTTP 流量上，可以按方法匹配，屏蔽/挂起时列表里仍会留下记录。",
}


class _PolicyPanel(QWidget):
    """One layer's policy radios laid out two per row."""

    policy_changed = Signal(object)

    def __init__(self, layer: GatewayLayer, parent: QWidget | None = None):
        super().__init__(parent)
        self.policies = list(LAYER_POLICIES[layer])
        self.group = QButtonGroup(self)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        for i, policy in enumerate(self.policies):
            button = RadioButton(policy_label(policy), self)
            button.setToolTip(policy_hint(policy))
            self.group.addButton(button, i)
            grid.addWidget(button, i // 2, i % 2)
        self.group.idToggled.connect(self._on_toggled)
        self.group.button(0).setChecked(True)

    def policy(self) -> GatewayPolicy:
        index = self.group.checkedId()
        return self.policies[max(index, 0)]

    def set_policy(self, policy: GatewayPolicy) -> bool:
        """Select ``policy``；本层不支持就返回 False，调用方决定退回哪一条。"""
        if policy not in self.policies:
            return False
        button = self.group.button(self.policies.index(policy))
        if button is None:
            return False
        button.setChecked(True)
        return True

    def _on_toggled(self, index: int, checked: bool) -> None:
        # idToggled 一次切换发两下（旧的取消、新的选中），只认选中那一下。
        if checked and 0 <= index < len(self.policies):
            self.policy_changed.emit(self.policies[index])


class GatewayRuleDialog(MessageBoxBase):
    """分层选层 + 单选选策略；最终合法性由 `GatewayRule.validate` 拍板。"""

    def __init__(
        self,
        title: str,
        rule: GatewayRule | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._rule = rule if rule is not None else GatewayRule()
        self.__init_widget(title)
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._sync_layer_texts()
        self._sync_policy_texts()
        self._validate()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.layer_pivot = SegmentedWidget(self)
        self.policy_stack = QStackedWidget(self)
        self._panels: dict[GatewayLayer, _PolicyPanel] = {}
        for layer in _LAYERS:
            panel = _PolicyPanel(layer, self)
            self._panels[layer] = panel
            self.policy_stack.addWidget(panel)
            self.layer_pivot.addItem(routeKey=str(layer), text=layer_label(layer))
        self.layer_pivot.setCurrentItem(str(self._rule.layer))
        self.policy_stack.setCurrentWidget(self._panels[self._rule.layer])
        self._panels[self._rule.layer].set_policy(self._rule.policy)

        self.layer_hint_label = CaptionLabel(self)
        self.layer_hint_label.setWordWrap(True)

        self.policy_hint_label = CaptionLabel(self)
        self.policy_hint_label.setWordWrap(True)

        self.field_combo = ComboBox(self)
        self.logic_combo = ComboBox(self)
        self.logic_combo.addItems([logic_label(logic) for logic in _LOGICS])
        self.logic_combo.setCurrentIndex(self._index_of(_LOGICS, self._rule.logic))

        self.value_edit = LineEdit(self)
        self.value_edit.setText(self._rule.value)
        self.value_edit.setClearButtonEnabled(True)

        self.status_combo = ComboBox(self)
        self.status_combo.addItems([status_label(s) for s in STATUS_CHOICES])
        if self._rule.status_code in STATUS_CHOICES:
            self.status_combo.setCurrentIndex(
                STATUS_CHOICES.index(self._rule.status_code)
            )

        self.preview_label = CaptionLabel(self)
        self.preview_label.setWordWrap(True)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        self._sync_fields()
        QTimer.singleShot(0, self.value_edit.setFocus)

    def __init_layout(self):
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("匹配对象"), self), self.field_combo)
        form.addRow(BodyLabel(self.tr("条件"), self), self.logic_combo)
        form.addRow(BodyLabel(self.tr("值"), self), self.value_edit)
        self.status_row_label = BodyLabel(self.tr("响应"), self)
        form.addRow(self.status_row_label, self.status_combo)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.layer_pivot)
        layout.addWidget(self.layer_hint_label)
        layout.addWidget(self.policy_stack)
        layout.addWidget(self.policy_hint_label)
        layout.addLayout(form)
        layout.addWidget(self.preview_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(480)

    def __connect_signal_to_slot(self):
        # 用 currentItemChanged 而不是 addItem 的 onClick：它在 setCurrentItem 里发，
        # 同一项重复点击不会重发（`pivot.py:208` 先比 routeKey），也覆盖程序化切换。
        self.layer_pivot.currentItemChanged.connect(self._on_layer_changed)
        for panel in self._panels.values():
            panel.policy_changed.connect(self._on_policy_changed)
        self.field_combo.currentIndexChanged.connect(self._on_field_changed)
        self.logic_combo.currentIndexChanged.connect(self._validate)
        self.status_combo.currentIndexChanged.connect(self._validate)
        self.value_edit.textChanged.connect(self._validate)

    @staticmethod
    def _index_of(items: list, value) -> int:
        for i, item in enumerate(items):
            if item == value:
                return i
        return 0

    # --- current selection ---

    def _current_layer(self) -> GatewayLayer:
        route = self.layer_pivot.currentRouteKey()
        try:
            return GatewayLayer(route)
        except ValueError:
            return _LAYERS[0]

    def _current_panel(self) -> _PolicyPanel:
        return self._panels[self._current_layer()]

    def _current_policy(self) -> GatewayPolicy:
        return self._current_panel().policy()

    def _current_fields(self) -> list[GatewayField]:
        return _LAYER_FIELDS[self._current_layer()]

    def _current_field(self) -> GatewayField:
        fields = self._current_fields()
        return fields[min(max(self.field_combo.currentIndex(), 0), len(fields) - 1)]

    # --- syncing ---

    def _sync_layer_texts(self):
        self.layer_hint_label.setText(_LAYER_HINTS.get(self._current_layer(), ""))

    def _sync_policy_texts(self):
        policy = self._current_policy()
        self.policy_hint_label.setText(policy_hint(policy))
        # 只有屏蔽（出）会把状态码回给客户端，其余策略这一项没有意义。
        needed = uses_status(policy)
        self.status_combo.setEnabled(needed)
        self.status_row_label.setEnabled(needed)

    def _sync_fields(self):
        """Rebuild the field combo for the current layer, keeping the choice."""
        wanted = self._current_field()
        fields = self._current_fields()
        self.field_combo.blockSignals(True)
        self.field_combo.clear()
        self.field_combo.addItems([field_label(f) for f in fields])
        self.field_combo.setCurrentIndex(self._index_of(fields, wanted))
        self.field_combo.setEnabled(len(fields) > 1)
        self.field_combo.blockSignals(False)
        self._sync_placeholder()

    def _sync_placeholder(self):
        self.value_edit.setPlaceholderText(_PLACEHOLDERS.get(self._current_field(), ""))

    # --- slots ---

    def _on_layer_changed(self, route: str):
        try:
            layer = GatewayLayer(route)
        except ValueError:
            return
        # 每层各记自己的策略：换回来还是上次选的那条。两层的策略集只有仅允许/绕行
        # 重合，跨层搬运没什么可搬的，不如让选择留在原地。
        self.policy_stack.setCurrentWidget(self._panels[layer])
        self._sync_layer_texts()
        self._sync_policy_texts()
        self._sync_fields()
        self._validate()

    def _on_policy_changed(self, _policy: GatewayPolicy):
        # 两个面板共用这个槽，但界面一律从当前面板重读，谁发的都不影响结果。
        self._sync_policy_texts()
        self._validate()

    def _on_field_changed(self):
        self._sync_placeholder()
        self._validate()

    def get_rule(self) -> GatewayRule:
        return GatewayRule(
            layer=self._current_layer(),
            policy=self._current_policy(),
            field=self._current_field(),
            logic=_LOGICS[max(self.logic_combo.currentIndex(), 0)],
            value=self.value_edit.text().strip(),
            status_code=STATUS_CHOICES[max(self.status_combo.currentIndex(), 0)],
            enabled=self._rule.enabled,
        )

    def _validate(self):
        rule = self.get_rule()
        try:
            # validate() 会走一遍 re.compile：坏正则必须在这里拦下，绝不能写进配置
            # —— 下发时 options.update 是原子的，一条坏 pattern 会让整批规则回滚。
            rule.validate()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        self.preview_label.setText(self.tr("匹配正则：{}").format(rule.pattern))
        self.yesButton.setEnabled(True)
