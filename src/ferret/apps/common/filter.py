from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    CheckBox,
    FluentIcon,
    Flyout,
    FlyoutAnimationType,
    FlyoutView,
    LineEdit,
    RoundMenu,
    TransparentDropDownPushButton,
    TransparentPushButton,
    TransparentToolButton,
)


class MultiFilterManager(QWidget):
    """抓包页过滤面板：单一 flowfilter 表达式编辑器 + Fluent 引导控件。

    模型只有一条原生表达式（`.plans/0-filter-redesign.expression-first.md`）：编辑器是
    唯一事实源，「添加筛选」下拉只在光标处**单向插入** token，不做反向解析。
    """

    MAX_ROWS = 5  # 历史遗留常量，外部或有引用，保留占位（当前模型不再有多行）。

    # 名称保留兼容既有接线（views.py）。语义 = 「表达式变了，请重算过滤」。
    conditionsChanged = Signal()
    panelCloseRequested = Signal()  # 收起面板

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.setVisible(False)

        # ── 表达式编辑器（唯一事实源，自带清除 ×） ──
        self.expression_input = LineEdit(self)
        self.expression_input.setMinimumWidth(360)
        self.expression_input.setClearButtonEnabled(True)
        self.expression_input.setPlaceholderText(self.tr('~u "api/.*" & !~m GET'))
        self.expression_input.setAccessibleName(self.tr("原生过滤表达式"))
        # 200ms 复位式 debounce：每个键入都重启，避免每按一键就编译一次表达式。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)

        # 错误详情行：parse 原文（任意英文，直显不译）。
        self.error_label = CaptionLabel(self)
        self.error_label.setStyleSheet("QLabel { color: #c42b1c; }")
        self.error_label.setVisible(False)

        # ── 「＋ 添加筛选」下拉 + 语法帮助 ──
        self.add_filter_btn = TransparentDropDownPushButton(
            FluentIcon.ADD, self.tr("添加筛选"), self
        )
        self._build_add_menu()
        self.help_btn = TransparentToolButton(FluentIcon.HELP, self)
        self.help_btn.setToolTip(self.tr("flowfilter 语法帮助"))
        self.help_btn.setAccessibleName(self.tr("flowfilter 语法帮助"))

        # ── 「仅高亮不过滤」开关 ──
        # 勾选后同一条表达式从「过滤（隐藏不匹配行）」切成「高亮（命中行整行染色，
        # 不隐藏任何行）」。两模式互斥，切换立即生效（不走 200ms debounce）。
        self.highlight_check = CheckBox(self.tr("仅高亮不过滤"), self)
        self.highlight_check.setToolTip(
            self.tr("勾选后不再隐藏不匹配的流量，改为高亮命中的行")
        )

        # ── 收起 ──
        self.close_btn = TransparentPushButton(FluentIcon.UP, self.tr("收起"), self)
        self.close_btn.setToolTip(self.tr("收起筛选面板"))
        self.close_btn.setAccessibleName(self.tr("收起筛选面板"))

    def _build_add_menu(self) -> None:
        """构建「添加筛选」下拉：每项在光标处插入一段 flowfilter token 骨架。"""
        menu = RoundMenu(parent=self)

        # 带值的选择器：插 `~x ""` 并把光标停在引号中间。
        value_fields = (
            ("~u", self.tr("URL")),
            ("~d", self.tr("域名")),
            ("~h", self.tr("请求/响应头")),
            ("~b", self.tr("正文")),
            ("~t", self.tr("内容类型")),
        )
        for op, label in value_fields:
            menu.addAction(
                Action(
                    f"{op}  {label}",
                    triggered=lambda _=False, o=op: self._insert_token(f'{o} ""', -1),
                )
            )
        # `~m` 值不带引号（方法名），`~c` 只吃精确整数状态码，都停在末尾等用户续打。
        menu.addAction(
            Action(
                self.tr("~m  方法"),
                triggered=lambda: self._insert_token("~m "),
            )
        )
        menu.addAction(
            Action(
                self.tr("~c  状态码"),
                triggered=lambda: self._insert_token("~c "),
            )
        )

        menu.addSeparator()
        for op, label in (
            ("~q", self.tr("~q  请求期")),
            ("~s", self.tr("~s  响应期")),
            ("~websocket", self.tr("~websocket  WebSocket 流量")),
            ("~marked", self.tr("~marked  已标记")),
        ):
            menu.addAction(
                Action(label, triggered=lambda _=False, o=op: self._insert_token(o))
            )

        menu.addSeparator()
        for token, label in (
            ("&", self.tr("&  与")),
            ("|", self.tr("|  或")),
            ("!", self.tr("!  非")),
        ):
            menu.addAction(
                Action(
                    label,
                    triggered=lambda _=False, t=token: self._insert_operator(t),
                )
            )
        menu.addAction(
            Action(
                self.tr("( )  分组"),
                triggered=lambda: self._insert_token("()", -1),
            )
        )

        self._add_menu = menu
        self.add_filter_btn.setMenu(menu)

    def __init_layout(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 8)
        root_layout.setSpacing(8)

        # ── 表达式编辑器 + 错误行 ──
        root_layout.addWidget(self.expression_input)
        root_layout.addWidget(self.error_label)

        # ── 添加筛选 + 帮助（左） · 收起（右） ──
        control_row = QHBoxLayout()
        control_row.setContentsMargins(0, 0, 0, 0)
        control_row.setSpacing(8)
        control_row.addWidget(self.highlight_check)
        control_row.addWidget(self.add_filter_btn)
        control_row.addWidget(self.help_btn)
        control_row.addStretch(1)
        control_row.addWidget(self.close_btn)
        root_layout.addLayout(control_row)

    def __connect_signal_to_slot(self):
        self.close_btn.clicked.connect(self.panelCloseRequested.emit)
        self.help_btn.clicked.connect(self._show_syntax_help)
        self.expression_input.textChanged.connect(self.__on_text_changed)
        self._debounce.timeout.connect(self._on_condition_changed)
        # 模式切换立即重算（过滤↔高亮 是两条不同下发路径，不该等 debounce）。
        self.highlight_check.stateChanged.connect(self._on_condition_changed)

    # ── token 插入器 ──

    def _insert_token(self, token: str, cursor_offset: int = 0) -> None:
        """在光标处插入 token；非空表达式自动补 ` & ` 前缀。

        ``cursor_offset`` 为插入后光标相对 token 末尾的偏移（-1 = 停在最后一个字符前，
        用于把光标塞进 `""` / `()` 中间）。
        """
        editor = self.expression_input
        text = editor.text()
        pos = editor.cursorPosition()
        prefix = ""
        # 光标前已有非空白内容 → 补连接符，省得用户手打 ` & `。
        if text[:pos].strip() and not text[:pos].rstrip().endswith(("&", "|", "!", "(")):
            prefix = " & "
        fragment = prefix + token
        editor.insert(fragment)
        if cursor_offset:
            editor.setCursorPosition(editor.cursorPosition() + cursor_offset)
        editor.setFocus()

    def _insert_operator(self, operator: str) -> None:
        """插入布尔运算符：`&`/`|` 两侧留空格，`!` 只前置一个空格。"""
        editor = self.expression_input
        pos = editor.cursorPosition()
        before = editor.text()[:pos]
        if operator == "!":
            fragment = "!" if before.endswith(" ") or not before else " !"
        else:
            lead = "" if before.endswith(" ") or not before else " "
            fragment = f"{lead}{operator} "
        editor.insert(fragment)
        editor.setFocus()

    def _show_syntax_help(self) -> None:
        """弹出 flowfilter 操作符速查表。"""
        view = FlyoutView(
            title=self.tr("flowfilter 语法"),
            content=self.tr(
                "~u <正则>     URL（含 scheme/端口/查询串）\n"
                "~d <正则>     域名（不含端口）\n"
                "~m <正则>     请求方法，如 ~m GET\n"
                "~c <整数>     状态码，只认精确码，如 ~c 200 / ~c 404\n"
                "~h <正则>     请求或响应头\n"
                "~b <正则>     正文\n"
                "~t <正则>     内容类型\n"
                "~q / ~s        请求期 / 响应期\n"
                "~websocket    WebSocket 流量\n"
                "~marked        已标记流量\n"
                "\n"
                "组合：a & b（与） a | b（或） !a（非） ( )（分组）\n"
                "带空格或括号的值要加引号：~u \"api/.*\""
            ),
            isClosable=True,
        )
        Flyout.make(view, self.help_btn, self, aniType=FlyoutAnimationType.PULL_UP)

    # ── 状态与数据 ──

    @Slot()
    def __on_text_changed(self) -> None:
        self._debounce.start()

    def get_raw_expression(self) -> str:
        """用户手写的整条原生 flowfilter 表达式（未校验，校验在 controller）。"""
        return self.expression_input.text().strip()

    def is_highlight_mode(self) -> bool:
        """勾了「仅高亮不过滤」→ 表达式当高亮用（不隐藏行）；否则当过滤用。"""
        return self.highlight_check.isChecked()

    # 历史名：View 侧仍以 conditions 语义接线。当前模型下条件即整条表达式，
    # 保留空列表返回，避免任何遗留调用方炸掉（新代码不该再调）。
    def get_conditions(self) -> list[dict]:
        return []

    def has_active_filter(self) -> bool:
        return bool(self.get_raw_expression())

    def active_condition_count(self) -> int:
        """command bar 只拿它当「过滤是否生效」的指示（views.py），非条数。"""
        return 1 if self.has_active_filter() else 0

    def set_raw_error(self, message: str) -> None:
        """置/清表达式错误态。错误消息是 parse 的原文（任意英文），直显不译。"""
        self.expression_input.setProperty("filterError", bool(message))
        self.expression_input.setToolTip(message)
        self.expression_input.setStyleSheet(
            "LineEdit { border: 1px solid #c42b1c; }" if message else ""
        )
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def showEvent(self, event: QShowEvent) -> None:
        """面板展开时自动聚焦表达式编辑器。"""
        super().showEvent(event)
        self.focus_first_input()

    def focus_first_input(self):
        self.expression_input.setFocus()

    @Slot()
    def clear_conditions(self):
        """清空表达式。"""
        self.expression_input.clear()
        self.set_raw_error("")
        self.conditionsChanged.emit()

    @Slot()
    def _on_condition_changed(self) -> None:
        self.conditionsChanged.emit()
