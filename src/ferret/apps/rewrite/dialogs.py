"""Rewrite-rule editing dialog."""

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

from ferret.apps.rewrite.models import kind_label, logic_label
from ferret.core.mitm import RewriteKind, RewriteLogic, RewriteRule

_KINDS: list[RewriteKind] = list(RewriteKind)
_LOGICS: list[RewriteLogic] = list(RewriteLogic)

_VALUE_PLACEHOLDERS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: "api.example.com",
    RewriteLogic.EQUALS: "https://api.example.com/v1/user",
    RewriteLogic.REGEX: r"^https://api\.example\.com/(.*)",
}

_REPLACEMENT_PLACEHOLDERS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: "127.0.0.1:8000",
    RewriteLogic.EQUALS: "http://127.0.0.1:8000/v1/user",
    RewriteLogic.REGEX: r"http://127.0.0.1:8000/\1",
}

# 三种匹配方式落到原生 addon 都是同一句 `re.sub(subject, replacement, pretty_url)`，
# 差别只在 subject 怎么生成，所以「包含」是**局部替换**、「等于」才是整条替换。
_LOGIC_HINTS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: "把 URL 中出现的这段文本换成重写目标，其余部分保持原样。",
    RewriteLogic.EQUALS: "整条 URL 完全相同时才生效，重写目标需是带协议和主机名的完整 URL。",
    RewriteLogic.REGEX: (
        r"对整条 URL 做正则替换，重写目标里可用 \1、\g<name> 引用捕获组。"
    ),
}


class RewriteRuleDialog(MessageBoxBase):
    """下拉选择自动生成 url-regex，最终合法性由原生 parse_map_remote_spec 拍板。"""

    def __init__(
        self,
        title: str,
        rule: RewriteRule | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._rule = rule if rule is not None else RewriteRule()
        self.__init_widget(title)
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._validate()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems([kind_label(kind) for kind in _KINDS])
        self.kind_combo.setCurrentIndex(self._index_of(_KINDS, self._rule.kind))
        # 目前 RewriteKind 只有 map_remote 一个成员，单选项下拉没有交互意义；
        # 将来补上 map_local / modify_headers 等成员，这里会自动变回可选。
        self.kind_combo.setEnabled(len(_KINDS) > 1)

        self.logic_combo = ComboBox(self)
        self.logic_combo.addItems([logic_label(logic) for logic in _LOGICS])
        self.logic_combo.setCurrentIndex(self._index_of(_LOGICS, self._rule.logic))

        self.value_edit = LineEdit(self)
        self.value_edit.setText(self._rule.value)
        self.value_edit.setClearButtonEnabled(True)

        self.replacement_edit = LineEdit(self)
        self.replacement_edit.setText(self._rule.replacement)
        self.replacement_edit.setClearButtonEnabled(True)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setWordWrap(True)

        self.preview_label = CaptionLabel(self)
        self.preview_label.setWordWrap(True)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        self._sync_logic_texts()
        QTimer.singleShot(0, self.value_edit.setFocus)

    def __init_layout(self):
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("类型"), self), self.kind_combo)
        form.addRow(BodyLabel(self.tr("匹配方式"), self), self.logic_combo)
        form.addRow(BodyLabel(self.tr("原始 URL"), self), self.value_edit)
        form.addRow(BodyLabel(self.tr("重写为"), self), self.replacement_edit)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addLayout(form)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.preview_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(480)

    def __connect_signal_to_slot(self):
        self.kind_combo.currentIndexChanged.connect(self._validate)
        self.logic_combo.currentIndexChanged.connect(self._on_logic_changed)
        self.value_edit.textChanged.connect(self._validate)
        self.replacement_edit.textChanged.connect(self._validate)

    @staticmethod
    def _index_of(items: list, value) -> int:
        for i, item in enumerate(items):
            if item == value:
                return i
        return 0

    def _current_logic(self) -> RewriteLogic:
        return _LOGICS[max(self.logic_combo.currentIndex(), 0)]

    def _sync_logic_texts(self):
        logic = self._current_logic()
        self.value_edit.setPlaceholderText(_VALUE_PLACEHOLDERS.get(logic, ""))
        self.replacement_edit.setPlaceholderText(
            _REPLACEMENT_PLACEHOLDERS.get(logic, "")
        )
        self.hint_label.setText(_LOGIC_HINTS.get(logic, ""))

    def _on_logic_changed(self):
        self._sync_logic_texts()
        self._validate()

    def get_rule(self) -> RewriteRule:
        return RewriteRule(
            kind=_KINDS[max(self.kind_combo.currentIndex(), 0)],
            logic=self._current_logic(),
            value=self.value_edit.text().strip(),
            replacement=self.replacement_edit.text().strip(),
            enabled=self._rule.enabled,
        )

    def _validate(self):
        rule = self.get_rule()
        try:
            # 原生 parse_map_remote_spec 过不了就不让保存，避免把坏规则写进配置
            # —— options.update 是原子的，一条坏 spec 会让整批规则回滚。
            rule.to_spec()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        self.preview_label.setText(
            self.tr("匹配正则：{}\n替换为：{}").format(rule.subject, rule.template)
        )
        self.yesButton.setEnabled(True)
