"""Block-rule editing dialog."""

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

from ferret.apps.blocklist.models import (
    FIELD_LABELS,
    LOGIC_LABELS,
    STATUS_CHOICES,
    status_label,
)
from ferret.core.mitm import BlockField, BlockLogic, BlockRule

_FIELDS: list[BlockField] = list(BlockField)
_LOGICS: list[BlockLogic] = list(BlockLogic)

_PLACEHOLDERS: dict[BlockField, str] = {
    BlockField.HOST: "example.com",
    BlockField.URL: "http://example.com/ads",
    BlockField.METHOD: "POST",
}


class BlockRuleDialog(MessageBoxBase):
    """下拉选择自动生成 flowfilter 表达式，最终合法性由原生 parse_spec 拍板。"""

    def __init__(
        self,
        title: str,
        rule: BlockRule | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._rule = rule if rule is not None else BlockRule()
        self.__init_widget(title)
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._validate()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.field_combo = ComboBox(self)
        self.field_combo.addItems([FIELD_LABELS.get(f, str(f)) for f in _FIELDS])
        self.field_combo.setCurrentIndex(self._index_of(_FIELDS, self._rule.field))

        self.logic_combo = ComboBox(self)
        self.logic_combo.addItems(
            [LOGIC_LABELS.get(logic, str(logic)) for logic in _LOGICS]
        )
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

        self._sync_placeholder()
        QTimer.singleShot(0, self.value_edit.setFocus)

    def __init_layout(self):
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("匹配对象"), self), self.field_combo)
        form.addRow(BodyLabel(self.tr("条件"), self), self.logic_combo)
        form.addRow(BodyLabel(self.tr("值"), self), self.value_edit)
        form.addRow(BodyLabel(self.tr("响应"), self), self.status_combo)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addLayout(form)
        layout.addWidget(self.preview_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(420)

    def __connect_signal_to_slot(self):
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

    def _sync_placeholder(self):
        field = _FIELDS[max(self.field_combo.currentIndex(), 0)]
        self.value_edit.setPlaceholderText(_PLACEHOLDERS.get(field, ""))

    def _on_field_changed(self):
        self._sync_placeholder()
        self._validate()

    def get_rule(self) -> BlockRule:
        return BlockRule(
            field=_FIELDS[max(self.field_combo.currentIndex(), 0)],
            logic=_LOGICS[max(self.logic_combo.currentIndex(), 0)],
            value=self.value_edit.text().strip(),
            status_code=STATUS_CHOICES[max(self.status_combo.currentIndex(), 0)],
            enabled=self._rule.enabled,
        )

    def _validate(self):
        rule = self.get_rule()
        try:
            # 原生 parse_spec 过不了就不让保存，避免把坏规则写进配置。
            rule.to_spec()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        self.preview_label.setText(self.tr("匹配表达式：{}").format(rule.expression))
        self.yesButton.setEnabled(True)
