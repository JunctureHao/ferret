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
    EditableComboBox,
    FluentIcon,
    LineEdit,
    MessageBoxBase,
    RoundMenu,
    SubtitleLabel,
    TransparentToolButton,
)

from ferret.apps.common.edit import ItemDualPanel, Language, ToolPlainTextEdit
from ferret.apps.common.icon import BaseAction
from ferret.apps.rewrite.models import (
    kind_label,
    logic_label,
    replace_summary,
    replacement_display,
    replacement_field_label,
    target_display,
    target_field_label,
)
from ferret.core.mitm import (
    BODY_KINDS,
    HEADER_KINDS,
    MAP_KINDS,
    REPLACE_KINDS,
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
        "把 URL 中出现的这段文本换成重写目标，其余部分保持原样。",
    ),
    RewriteLogic.EQUALS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "整条 URL 完全相同时才生效，重写目标需是带协议和主机名的完整 URL。",
    ),
    RewriteLogic.REGEX: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "对整条 URL 做正则替换，重写目标里可用 \\1、\\g<name> 引用捕获组。",
    ),
}

# 其余类型不改 URL，同一栏只用来**挑流量**：命中即按各自的语义生效。
# 这里的措辞只能讲「命中」，不能讲「替换」。
_MATCH_LOGIC_HINTS: dict[RewriteLogic, str] = {
    RewriteLogic.CONTAINS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "URL 中出现这段文本即命中。"
    ),
    RewriteLogic.EQUALS: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "整条 URL 完全相同时才命中。"
    ),
    RewriteLogic.REGEX: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "按正则匹配整条 URL，命中即生效。"
    ),
}

# 只有重定向两类需要额外解释「命中之后发生什么」；其余类型的说明按下面几条常量给。
_KIND_HINTS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "命中的请求在发出前改写目标地址，Host 请求头随之更新。",
    ),
    RewriteKind.MAP_LOCAL: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog",
        "命中的请求不再发往服务器，直接用本地内容作答。路径**必须当下存在**（原生按 strict 解析），指向目录时按 URL 路径在目录内取同名文件。",
    ),
}


# 头/体两类共用的引擎语义，逐条都踩过坑，必须如实告知。写成函数而不是常量：文案
# 一律等到用的时候才求值（体那条还要把整体匹配正则填进去，f-string 里的文案
# lupdate 看不见）。
def _header_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        "命中时先删掉同名头、再按新值加回去；头值留空 = 只删不加。头值以 @ 开头会被当作**文件路径**读取内容（因此无法下发真的以 @ 开头的头值）。\\n、\\t 等转义会被解码，要字面反斜杠请写 \\\\。",
    )


def _body_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        "体正则留空 = 整体替换（实际下发 {}）；新内容留空 = 清空匹配到的内容。新内容是**字面量**，不支持 \\1 反向引用；以 @ 开头会被当作**文件路径**读取内容，每次请求现读。\\n、\\t 等转义会被解码，要字面反斜杠请写 \\\\。",
    ).format(WHOLE_BODY_PATTERN)


def _replace_request_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        "命中的请求在发出前按所填栏目逐项覆盖：方法、路径、头表、体；留空的栏保持原样，至少要填一项。体以 @ 开头会被当作**文件路径**读取内容，每次请求现读。",
    )


def _replace_response_hint() -> str:
    return QCoreApplication.translate(
        "RewriteRuleDialog",
        "命中的请求不再发往服务器，直接按状态码、头表、体整条作答，至少要填一项。体以 @ 开头会被当作**文件路径**读取内容，每次请求现读。手写二进制响应不支持——请改用「重定向（本地）」。",
    )


_TARGET_PLACEHOLDERS: dict[RewriteKind, str] = {
    RewriteKind.MODIFY_REQUEST_HEADER: "User-Agent",
    RewriteKind.MODIFY_RESPONSE_HEADER: "Cache-Control",
    RewriteKind.MODIFY_REQUEST_BODY: QT_TRANSLATE_NOOP(
        "RewriteRuleDialog", "留空 = 整体替换"
    ),
    RewriteKind.MODIFY_RESPONSE_BODY: r'"code":\s*\d+',
}

_TARGET_ROW = 3
_REPLACEMENT_ROW = 4
_METHOD_ROW = 5
_PATH_ROW = 6
_STATUS_ROW = 7
_HEADERS_ROW = 8
_BODY_ROW = 9

# 替换响应的常用状态码（可手输，§6：常见码下拉）。
_STATUS_CODES: list[str] = [
    "200",
    "201",
    "204",
    "301",
    "302",
    "304",
    "400",
    "401",
    "403",
    "404",
    "429",
    "500",
    "502",
    "503",
    "504",
]


def _sniff_language(text: str) -> Language:
    """按内容挑高亮：JSON / HTML（XML 词法器）/ 纯文本（§6）。"""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        return Language.JSON
    if stripped.startswith("<"):
        return Language.XML
    return Language.TEXT


class RewriteRuleDialog(MessageBoxBase):
    """八种重写类型共用一张表单（定则三）：类型一换，形态随之切换。

    - **形态 A（字段型，既有六类）**：匹配区 + 目标/替换区；
    - **形态 B（消息型，替换请求/替换响应）**：匹配区 + 方法/路径/状态码 +
      头表 `ItemDualPanel` + 体编辑器。

    最终合法性一律交给 `RewriteRule.validate()`，过不了就不让保存 —— 下发是
    整批编译的，一条坏规则会让整批回滚。
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

        # 浏览按钮只给 map_local：引擎要求路径当下就存在，让用户选而不是手打，
        # 能挡掉绝大多数「路径不存在」的回滚。
        self.browse_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.browse_btn.setFixedSize(32, 32)
        self.browse_btn.setToolTip(self.tr("选择本地文件或目录"))

        # 体内容常是整段 JSON，单行输入框放不下 —— 复用 apps/common/edit 的编辑器
        # （自带复制/换行/查找与高亮），和抓包详情页是同一套控件。
        self.replacement_text = ToolPlainTextEdit(self)
        self.replacement_text.setMinimumHeight(180)

        self.replacement_stack = QStackedWidget(self)
        self.replacement_stack.addWidget(self.__build_single_line_row())
        self.replacement_stack.addWidget(self.replacement_text)

        # —— 形态 B（消息型）：替换请求 / 替换响应 ——
        self.method_edit = LineEdit(self)
        self.method_edit.setPlaceholderText("GET")
        self.method_edit.setClearButtonEnabled(True)

        self.path_edit = LineEdit(self)
        self.path_edit.setPlaceholderText("/v1/user?id=1")
        self.path_edit.setClearButtonEnabled(True)

        self.status_combo = EditableComboBox(self)
        self.status_combo.addItems(_STATUS_CODES)
        if self._rule.status_code is not None:
            self.status_combo.setText(str(self._rule.status_code))

        self.headers_panel = ItemDualPanel(editable=True, parent=self)
        self.headers_panel.set_items(list(self._rule.headers))

        self.message_body = ToolPlainTextEdit(self)
        self.message_body.setMinimumHeight(140)
        self._set_message_body(self._rule.replacement)

        self.target_label = BodyLabel(self)
        self.replacement_label = BodyLabel(self)
        self.method_label = BodyLabel(self.tr("方法"), self)
        self.path_label = BodyLabel(self.tr("路径"), self)
        self.status_label = BodyLabel(self.tr("状态码"), self)
        self.headers_label = BodyLabel(self.tr("头表"), self)
        self.body_label = BodyLabel(self.tr("体"), self)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setWordWrap(True)

        self.preview_label = CaptionLabel(self)
        self.preview_label.setWordWrap(True)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        self.method_edit.setText(self._rule.method)
        self.path_edit.setText(self._rule.path)
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
        self.form.addRow(BodyLabel(self.tr("类型"), self), self.kind_combo)
        self.form.addRow(BodyLabel(self.tr("匹配方式"), self), self.logic_combo)
        self.form.addRow(BodyLabel(self.tr("匹配 URL"), self), self.value_edit)
        # 目标（头名/体正则）与替换（单行/多行）各占一行 —— 两个标签都必须进
        # 布局：BodyLabel(self) 只挂了 parent，不进 QFormLayout 就会浮在 (0,0)。
        self.form.addRow(self.target_label, self.target_edit)
        self.form.addRow(self.replacement_label, self.replacement_stack)
        self.form.addRow(self.method_label, self.method_edit)
        self.form.addRow(self.path_label, self.path_edit)
        self.form.addRow(self.status_label, self.status_combo)
        self.form.addRow(self.headers_label, self.headers_panel)
        self.form.addRow(self.body_label, self.message_body)

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
        self.method_edit.textChanged.connect(self._validate)
        self.path_edit.textChanged.connect(self._validate)
        self.status_combo.currentTextChanged.connect(self._validate)
        self.headers_panel.changed.connect(self._validate)
        self.message_body.changed.connect(self._validate)

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
        """读「重写为/体」栏。`kind` 默认取当前选中的类型。

        切换类型时必须显式传**切换前**的类型：``currentIndexChanged`` 触发时下拉
        已经是新值了，照当前类型去读会读到那个还空着的新栏位，用户刚打的内容就没了。
        """
        kind = self._current_kind() if kind is None else kind
        if kind in REPLACE_KINDS:
            return self.message_body.text()
        if self._multiline(kind):
            return self.replacement_text.text()
        return self.replacement_edit.text()

    def _set_replacement_text(self, text: str) -> None:
        """所有正文编辑器都写一遍，切换类型时内容不会凭空消失。

        `ToolPlainTextEdit.set_text` 是程序化换文本、不发 ``changed``，所以这里
        不会顺带触发一轮校验（切换类型的那轮由 `_on_kind_changed` 自己收尾）。
        """
        self.replacement_edit.setText(text)
        self.replacement_text.set_text(text, Language.JSON)
        self._set_message_body(text)

    def _set_message_body(self, text: str) -> None:
        self.message_body.set_text(text, _sniff_language(text))

    def _sync_kind_texts(self) -> None:
        kind = self._current_kind()
        logic = self._current_logic()
        is_map = kind in MAP_KINDS
        is_replace = kind in REPLACE_KINDS

        # 目标行：重定向两类没有「目标」栏；替换两类走消息型形态，同样退场。
        self.form.setRowVisible(_TARGET_ROW, not is_map and not is_replace)
        # 替换行：字段型六类都用（重定向两类填重写目标，头/体填值/内容）。
        self.form.setRowVisible(_REPLACEMENT_ROW, not is_replace)
        self.form.setRowVisible(_METHOD_ROW, kind == RewriteKind.REPLACE_REQUEST)
        self.form.setRowVisible(_PATH_ROW, kind == RewriteKind.REPLACE_REQUEST)
        self.form.setRowVisible(_STATUS_ROW, kind == RewriteKind.REPLACE_RESPONSE)
        self.form.setRowVisible(_HEADERS_ROW, is_replace)
        self.form.setRowVisible(_BODY_ROW, is_replace)
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
        elif kind == RewriteKind.REPLACE_REQUEST:
            hints.append(_replace_request_hint())
        elif kind == RewriteKind.REPLACE_RESPONSE:
            hints.append(_replace_response_hint())
        self.hint_label.setText("\n".join(hint for hint in hints if hint))

    def _on_kind_changed(self):
        # 单行 ↔ 多行 ↔ 消息体换栏时把内容带过去，用户改错类型不必重打一遍。
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
            icon=FluentIcon.DOCUMENT, text=self.tr("选择文件"), parent=menu
        )
        file_action.triggered.connect(self._pick_file)
        menu.addAction(file_action)
        dir_action = BaseAction(
            icon=FluentIcon.FOLDER, text=self.tr("选择目录"), parent=menu
        )
        dir_action.triggered.connect(self._pick_directory)
        menu.addAction(dir_action)
        menu.exec(self.browse_btn.mapToGlobal(self.browse_btn.rect().bottomLeft()))

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("选择本地文件"))
        if path:
            self.replacement_edit.setText(path)

    def _pick_directory(self):
        path = QFileDialog.getExistingDirectory(self, self.tr("选择本地目录"))
        if path:
            self.replacement_edit.setText(path)

    # —— 取值与校验 ——

    def get_rule(self) -> RewriteRule:
        """按类型决定哪几栏能安全 strip。

        - URL 匹配值 / 方法 / 路径：恒 strip，前后空白没有意义。
        - 目标：头名 strip；**体正则不 strip** —— 正则里的空白是有意义的，
          整栏留空才当「整体替换」。
        - 替换串 / 体内容：URL / 本地路径 strip；**头值与体内容不 strip** ——
          尾随换行之类原样下发才是用户要的。
        - 状态码：留空 = 执行期取 200；非数字在这里就报错，让预览区指着那一栏说。
        """
        kind = self._current_kind()
        logic = self._current_logic()
        value = self.value_edit.text().strip()
        enabled = self._rule.enabled
        if kind == RewriteKind.REPLACE_REQUEST:
            return RewriteRule(
                kind=kind,
                logic=logic,
                value=value,
                enabled=enabled,
                method=self.method_edit.text().strip(),
                path=self.path_edit.text().strip(),
                headers=tuple(self.headers_panel.items()),
                replacement=self.message_body.text(),
            )
        if kind == RewriteKind.REPLACE_RESPONSE:
            return RewriteRule(
                kind=kind,
                logic=logic,
                value=value,
                enabled=enabled,
                status_code=self._status_value(),
                headers=tuple(self.headers_panel.items()),
                replacement=self.message_body.text(),
            )
        target = self.target_edit.text()
        replacement = self._replacement_value()
        return RewriteRule(
            kind=kind,
            logic=logic,
            value=value,
            enabled=enabled,
            target=target.strip() if kind in HEADER_KINDS else target,
            replacement=replacement.strip() if kind in MAP_KINDS else replacement,
        )

    def _status_value(self) -> int | None:
        text = self.status_combo.text().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError as exc:
            # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "状态码必须是整数：{}").format(
                    text
                )
            ) from exc

    def _validate(self):
        try:
            rule = self.get_rule()
            rule.validate()
        except ValueError as exc:
            self.preview_label.setText(str(exc))
            self.yesButton.setEnabled(False)
            return
        self.preview_label.setText("\n".join(self._preview_lines(rule)))
        self.yesButton.setEnabled(True)

    def _preview_lines(self, rule: RewriteRule) -> list[str]:
        """`validate` 已经过了，所以这里取哪个属性都不会再抛。"""
        lines = [self.tr("匹配正则：{}").format(rule.subject)]
        if rule.kind == RewriteKind.MAP_REMOTE:
            lines.append(self.tr("替换为：{}").format(rule.template))
        elif rule.kind == RewriteKind.MAP_LOCAL:
            lines.append(self.tr("本地路径：{}").format(rule.replacement.strip()))
        elif rule.kind in REPLACE_KINDS:
            lines.append(replace_summary(rule))
        else:
            # 这两行的措辞由 models 统一给，和表格里那两列逐字一致。
            lines.append(
                self.tr("{}：{}").format(self.target_label.text(), target_display(rule))
            )
            lines.append(
                self.tr("{}：{}").format(
                    self.replacement_label.text(), replacement_display(rule)
                )
            )
        return lines
