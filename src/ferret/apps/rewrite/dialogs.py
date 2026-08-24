"""Rewrite-rule editing dialog."""

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    LineEdit,
    MessageBoxBase,
    RoundMenu,
    SubtitleLabel,
    TransparentToolButton,
)

from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.icon import BaseAction
from ferret.apps.rewrite.models import (
    kind_label,
    logic_label,
    replacement_display,
    replacement_field_label,
    target_display,
    target_field_label,
)
from ferret.core.mitm import (
    BODY_KINDS,
    HEADER_KINDS,
    MAP_KINDS,
    WHOLE_BODY_PATTERN,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
)
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

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

# 三种匹配方式落到 MapRemote 都是同一句 `re.sub(subject, replacement, pretty_url)`，
# 差别只在 subject 怎么生成，所以「包含」是**局部替换**、「等于」才是整条替换。
# 这几张表只存标记，求值推到 `_sync_kind_texts()` —— 模块级求值赶在翻译器安装之前。
_REWRITE_LOGIC_HINTS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "Replaces this text wherever it appears in the URL and leaves the rest untouched.",
    ),
    RewriteLogic.EQUALS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "Applies only when the whole URL matches exactly; the rewrite target must be a full URL with scheme and host.",
    ),
    RewriteLogic.REGEX: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        r"Runs a regex substitution over the whole URL; the rewrite target can reference capture groups with \1 or \g<name>.",
    ),
}

# 其余五种类型不改 URL，同一栏只用来**挑流量**：`map_local` 走原生
# `re.search(spec.regex, pretty_url)`，头/体两类落在 spec 的 flow-filter 段
# （`~u`）上。所以这里的措辞只能讲「命中」，不能讲「替换」。
_MATCH_LOGIC_HINTS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "Matches when this text appears anywhere in the URL."
    ),
    RewriteLogic.EQUALS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "Matches only when the whole URL is exactly the same."
    ),
    RewriteLogic.REGEX: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "Matches the whole URL by regex."
    ),
}

# 只有重定向两类需要额外解释「命中之后发生什么」；头/体两类的说明按下面两条常量给。
_KIND_HINTS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "Matching requests get a new target address before they go out, and the Host header follows.",
    ),
    RewriteKind.MAP_LOCAL: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "Matching requests never reach the server; local content answers them instead. The path **must exist right now** (mitmproxy parses it strictly), and a folder is searched by the URL path for a file of the same name.",
    ),
}


# 头/体两类共用的原生语义，逐条都踩过坑，必须如实告知。写成函数而不是常量：文案
# 一律等到用的时候才求值（体那条还要把整体匹配正则填进去，f-string 里的文案
# lupdate 看不见）。
def _header_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        r"On a match the header is removed first and then added back with the new value; an empty header value means remove only. A header value starting with @ is read as a **file path** (mitmproxy's own semantics, so a header value that really starts with @ cannot be sent). Escapes such as \n and \t are decoded; write \\ for a literal backslash.",
    )


def _body_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        r"An empty body pattern replaces the whole body (it sends {} instead); empty new content clears whatever matched. New content is **literal**, so \1 back-references do not work (mitmproxy substitutes with `lambda _: replacement`), and content starting with @ is read as a **file path**. Escapes such as \n and \t are decoded; write \\ for a literal backslash.",
    ).format(WHOLE_BODY_PATTERN)


_TARGET_PLACEHOLDERS: dict[RewriteKind, str] = {
    RewriteKind.MODIFY_REQUEST_HEADER: "User-Agent",
    RewriteKind.MODIFY_RESPONSE_HEADER: "Cache-Control",
    RewriteKind.MODIFY_REQUEST_BODY: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "Empty = replace the whole body"
    ),
    RewriteKind.MODIFY_RESPONSE_BODY: r'"code":\s*\d+',
}

_TARGET_ROW = 3


class RewriteRuleDialog(MessageBoxBase):
    """六种重写类型共用一张表单：类型一换，栏位标签/提示/编辑器随之切换。

    最终合法性一律交给原生解析器拍板（`RewriteRule.to_spec` 内部会跑
    `parse_map_remote_spec` / `parse_map_local_spec` / `parse_modify_spec`），
    过不了就不让保存 —— `options.update` 是原子的，一条坏 spec 会让整批规则回滚。
    """

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
        self._sync_kind_texts()
        self._validate()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.kind_combo = ComboBox(self)
        self.kind_combo.addItems([kind_label(kind) for kind in _KINDS])
        self.kind_combo.setCurrentIndex(self._index_of(_KINDS, self._rule.kind))
        # 上一次选中的类型，只给 `_on_kind_changed` 搬内容用（见 `_replacement_value`）。
        self._last_kind = self._current_kind()

        self.logic_combo = ComboBox(self)
        self.logic_combo.addItems([logic_label(logic) for logic in _LOGICS])
        self.logic_combo.setCurrentIndex(self._index_of(_LOGICS, self._rule.logic))

        self.value_edit = LineEdit(self)
        self.value_edit.setText(self._rule.value)
        self.value_edit.setClearButtonEnabled(True)

        self.target_edit = LineEdit(self)
        self.target_edit.setText(self._rule.target)
        self.target_edit.setClearButtonEnabled(True)

        self.replacement_edit = LineEdit(self)
        self.replacement_edit.setClearButtonEnabled(True)

        # 浏览按钮只给 map_local：原生 parse_map_local_spec 要求路径当下就存在，
        # 让用户选而不是手打，能挡掉绝大多数「路径不存在」的回滚。
        self.browse_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.browse_btn.setFixedSize(32, 32)
        self.browse_btn.setToolTip(self.tr("Pick a local file or folder"))

        # 体内容常是整段 JSON，单行输入框放不下 —— 复用 apps/common/edit 的编辑器
        # （自带复制/换行/查找与高亮），和抓包详情页是同一套控件。
        self.replacement_text = ToolPlainTextEdit(self)
        self.replacement_text.setMinimumHeight(180)

        self.replacement_stack = QStackedWidget(self)
        self.replacement_stack.addWidget(self.__build_single_line_row())
        self.replacement_stack.addWidget(self.replacement_text)

        self.target_label = BodyLabel(self)
        self.replacement_label = BodyLabel(self)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setWordWrap(True)

        self.preview_label = CaptionLabel(self)
        self.preview_label.setWordWrap(True)

        self.yesButton.setText(self.tr("Save"))
        self.cancelButton.setText(self.tr("Cancel"))

        self._set_replacement_text(self._rule.replacement)
        QTimer.singleShot(0, self.value_edit.setFocus)

    def __build_single_line_row(self) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.replacement_edit, 1)
        layout.addWidget(self.browse_btn)
        return row

    def __init_layout(self):
        self.form = QFormLayout()
        self.form.setSpacing(8)
        self.form.addRow(BodyLabel(self.tr("Type"), self), self.kind_combo)
        self.form.addRow(BodyLabel(self.tr("Condition"), self), self.logic_combo)
        self.form.addRow(BodyLabel(self.tr("Match URL"), self), self.value_edit)
        self.form.addRow(self.target_label, self.target_edit)
        self.form.addRow(self.replacement_label, self.replacement_stack)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addLayout(self.form)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.preview_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(560)

    def __connect_signal_to_slot(self):
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        self.logic_combo.currentIndexChanged.connect(self._on_logic_changed)
        self.value_edit.textChanged.connect(self._validate)
        self.target_edit.textChanged.connect(self._validate)
        self.replacement_edit.textChanged.connect(self._validate)
        self.replacement_text.changed.connect(self._validate)
        self.browse_btn.clicked.connect(self._on_browse)

    @staticmethod
    def _index_of(items: list, value) -> int:
        for i, item in enumerate(items):
            if item == value:
                return i
        return 0

    def _current_kind(self) -> RewriteKind:
        return _KINDS[max(self.kind_combo.currentIndex(), 0)]

    def _current_logic(self) -> RewriteLogic:
        return _LOGICS[max(self.logic_combo.currentIndex(), 0)]

    # —— 栏位随类型切换 ——

    def _multiline(self, kind: RewriteKind) -> bool:
        """体内容用多行编辑器，其余（URL / 路径 / 头值）用单行输入框。"""
        return kind in BODY_KINDS

    def _replacement_value(self, kind: RewriteKind | None = None) -> str:
        """读「重写为」栏。`kind` 默认取当前选中的类型。

        切换类型时必须显式传**切换前**的类型：``currentIndexChanged`` 触发时下拉
        已经是新值了，照当前类型去读会读到那个还空着的新栏位，用户刚打的内容就没了。
        """
        if self._multiline(self._current_kind() if kind is None else kind):
            return self.replacement_text.text()
        return self.replacement_edit.text()

    def _set_replacement_text(self, text: str) -> None:
        """两个编辑器都写一遍，切换类型时内容不会凭空消失。

        `ToolPlainTextEdit.set_text` 是程序化换文本、不发 ``changed``，所以这里
        不会顺带触发一轮校验（切换类型的那轮由 `_on_kind_changed` 自己收尾）。
        """
        self.replacement_edit.setText(text)
        self.replacement_text.set_text(text, Language.JSON)

    def _sync_kind_texts(self) -> None:
        kind = self._current_kind()
        logic = self._current_logic()
        is_map = kind in MAP_KINDS

        self.form.setRowVisible(_TARGET_ROW, not is_map)
        # 标签由 models 统一给：`tr(变量)` lupdate 提取不到，得走标记 + translate。
        self.target_label.setText(target_field_label(kind))
        self.target_edit.setPlaceholderText(
            resolve_marker(_TARGET_PLACEHOLDERS, kind, "RewriteRuleDialog")
        )
        self.replacement_label.setText(replacement_field_label(kind))

        self.value_edit.setPlaceholderText(_VALUE_PLACEHOLDERS.get(logic, ""))
        self.replacement_stack.setCurrentIndex(int(self._multiline(kind)))
        self.browse_btn.setVisible(kind == RewriteKind.MAP_LOCAL)
        self.replacement_edit.setPlaceholderText(
            _REPLACEMENT_PLACEHOLDERS.get(logic, "")
            if kind == RewriteKind.MAP_REMOTE
            else ""
        )

        logic_hints = (
            _REWRITE_LOGIC_HINTS
            if kind == RewriteKind.MAP_REMOTE
            else _MATCH_LOGIC_HINTS
        )
        hints = [
            resolve_marker(logic_hints, logic, "RewriteRuleDialog"),
            resolve_marker(_KIND_HINTS, kind, "RewriteRuleDialog"),
        ]
        if kind in HEADER_KINDS:
            hints.append(_header_hint())
        elif kind in BODY_KINDS:
            hints.append(_body_hint())
        self.hint_label.setText("\n".join(hint for hint in hints if hint))

    def _on_kind_changed(self):
        # 单行 ↔ 多行换栏时把内容带过去，用户改错类型不必重打一遍。
        carried = self._replacement_value(self._last_kind)
        self._last_kind = self._current_kind()
        self._set_replacement_text(carried)
        self._sync_kind_texts()
        self._validate()

    def _on_logic_changed(self):
        self._sync_kind_texts()
        self._validate()

    def _on_browse(self):
        """map_local 既能指向单个文件、也能指向整个目录，所以给两个入口。"""
        menu = RoundMenu(parent=self)
        file_action = BaseAction(
            icon=FluentIcon.DOCUMENT, text=self.tr("Pick a file"), parent=menu
        )
        file_action.triggered.connect(self._pick_file)
        menu.addAction(file_action)
        dir_action = BaseAction(
            icon=FluentIcon.FOLDER, text=self.tr("Pick a folder"), parent=menu
        )
        dir_action.triggered.connect(self._pick_directory)
        menu.addAction(dir_action)
        menu.exec(self.browse_btn.mapToGlobal(self.browse_btn.rect().bottomLeft()))

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Pick a local file"))
        if path:
            self.replacement_edit.setText(path)

    def _pick_directory(self):
        path = QFileDialog.getExistingDirectory(self, self.tr("Pick a local folder"))
        if path:
            self.replacement_edit.setText(path)

    # —— 取值与校验 ——

    def get_rule(self) -> RewriteRule:
        """按类型决定哪几栏能安全 strip。

        - URL 匹配值：恒 strip，前后空白在 URL 里没有意义。
        - 目标：头名 strip；**体正则不 strip** —— 正则里的空白是有意义的，
          整栏留空才当「整体替换」（见 `RewriteRule._body_spec`）。
        - 替换串：URL / 本地路径 strip；**头值与体内容不 strip** —— 尾随换行之类
          原样下发才是用户要的（原生 `_modify_replacement` 也不 strip）。
        """
        kind = self._current_kind()
        target = self.target_edit.text()
        replacement = self._replacement_value()
        return RewriteRule(
            kind=kind,
            logic=self._current_logic(),
            value=self.value_edit.text().strip(),
            target=target.strip() if kind in HEADER_KINDS else target,
            replacement=replacement.strip() if kind in MAP_KINDS else replacement,
            enabled=self._rule.enabled,
        )

    def _validate(self):
        rule = self.get_rule()
        try:
            rule.to_spec()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        self.preview_label.setText("\n".join(self._preview_lines(rule)))
        self.yesButton.setEnabled(True)

    def _preview_lines(self, rule: RewriteRule) -> list[str]:
        """`to_spec` 已经过了，所以这里取哪个属性都不会再抛。"""
        lines = [self.tr("Match pattern: {}").format(rule.subject)]
        if rule.kind == RewriteKind.MAP_REMOTE:
            lines.append(self.tr("Rewrite to: {}").format(rule.template))
        elif rule.kind == RewriteKind.MAP_LOCAL:
            lines.append(self.tr("Local path: {}").format(rule.replacement.strip()))
        else:
            # 这两行的措辞由 models 统一给，和表格里那两列逐字一致。
            lines.append(
                self.tr("{}: {}").format(self.target_label.text(), target_display(rule))
            )
            lines.append(
                self.tr("{}: {}").format(
                    self.replacement_label.text(), replacement_display(rule)
                )
            )
        return lines
