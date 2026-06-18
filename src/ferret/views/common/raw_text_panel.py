"""原始文本面板 — 带搜索、换行、复制的只读文本显示器"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    SearchLineEdit,
    SimpleCardWidget,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ferret.views.common.info_bar import show_success, show_warning
from ferret.views.common.line_edit import LineNumberTextEdit


class RawTextPanel(SimpleCardWidget):
    """原始文本面板 — 搜索 / 换行 / 复制 + 行号文本显示器

    用于显示 HTTP 原始请求/响应等纯文本内容。
    调用 set_text(str) 填充数据。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBorderRadius(0)
        self._wrap = False
        self._search_positions: list[int] = []

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # —— 初始化 ————————————————————————————————————

    def __init_widget(self):
        # ── 工具栏按钮 ──
        self.search_toggle_btn = TransparentToolButton(FluentIcon.SEARCH, self)
        self.search_toggle_btn.setToolTip(self.tr("查找"))
        self.search_toggle_btn.installEventFilter(
            ToolTipFilter(self.search_toggle_btn, 1000, ToolTipPosition.TOP)
        )

        self.wrap_btn = TransparentToolButton(FluentIcon.RETURN, self)
        self.wrap_btn.setToolTip(self.tr("切换自动换行"))
        self.wrap_btn.installEventFilter(
            ToolTipFilter(self.wrap_btn, 1000, ToolTipPosition.TOP)
        )

        self.copy_btn = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_btn.setToolTip(self.tr("复制"))
        self.copy_btn.installEventFilter(
            ToolTipFilter(self.copy_btn, 1000, ToolTipPosition.TOP)
        )

        # ── 搜索栏（默认隐藏）──
        self.search_bar_widget = QWidget()
        self.search_edit = SearchLineEdit(self.search_bar_widget)
        self.search_edit.setPlaceholderText(self.tr("查找…"))
        self.search_close_btn = TransparentToolButton(
            FluentIcon.CLOSE, self.search_bar_widget
        )
        self.search_bar_widget.hide()

        # ── 文本编辑器（带行号）──
        self.text_edit = LineNumberTextEdit(self)

    def __init_layout(self):
        # 工具栏 — 按钮靠右
        self.toolbar = QHBoxLayout()
        self.toolbar.setContentsMargins(4, 2, 4, 2)
        self.toolbar.addStretch(1)
        self.toolbar.addWidget(self.search_toggle_btn)
        self.toolbar.addWidget(self.wrap_btn)
        self.toolbar.addWidget(self.copy_btn)

        # 搜索栏布局（内含底部分隔线）
        self._search_separator = QFrame()
        self._search_separator.setFrameShape(QFrame.Shape.HLine)
        self._search_separator.setFixedHeight(1)
        self._search_separator.setStyleSheet("background: palette(mid); border: none;")

        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(4)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(self.search_close_btn)

        search_vbox = QVBoxLayout(self.search_bar_widget)
        search_vbox.setContentsMargins(0, 0, 0, 0)
        search_vbox.setSpacing(0)
        search_vbox.addLayout(search_row)
        search_vbox.addWidget(self._search_separator)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(self.toolbar)
        layout.addWidget(self.search_bar_widget)
        layout.addWidget(self.text_edit, 1)

    def __connect_signal_to_slot(self):
        self.search_toggle_btn.clicked.connect(self.__toggle_search_bar)
        self.search_close_btn.clicked.connect(
            lambda: self.__set_search_bar_visible(False)
        )
        self.search_edit.textChanged.connect(self.__on_search)
        self.wrap_btn.clicked.connect(self.__toggle_wrap)
        self.copy_btn.clicked.connect(self.__copy_content)

        # Ctrl+F 快捷键（窗口级，仅在面板可见时生效）
        self._search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        self._search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._search_shortcut.activated.connect(self.__on_ctrl_f)

    # —— 公开接口 ————————————————————————————————————

    def set_text(self, text: str):
        """设置文本内容"""
        self.text_edit.setPlainText(text)

    # —— 搜索 ————————————————————————————————————

    @Slot()
    def __toggle_search_bar(self):
        self.__set_search_bar_visible(not self.search_bar_widget.isVisible())

    @Slot()
    def __on_ctrl_f(self):
        """Ctrl+F 快捷键槽函数 — 仅在面板可见时打开搜索"""
        if self.isVisible():
            self.__set_search_bar_visible(True)

    def __set_search_bar_visible(self, visible: bool):
        self.search_bar_widget.setVisible(visible)
        if visible:
            self.search_edit.setFocus()
        else:
            self.search_edit.clear()
            self.text_edit.setExtraSelections([])

    @Slot(str)
    def __on_search(self, text: str):
        """文本查找 — 高亮所有匹配"""
        self._search_positions.clear()
        self.text_edit.setExtraSelections([])

        if not text:
            return

        full_text = self.text_edit.toPlainText()
        lower_text = full_text.lower()
        lower_search = text.lower()
        selections = []
        highlight_color = QColor(255, 255, 0, 80)

        start = 0
        while True:
            idx = lower_text.find(lower_search, start)
            if idx == -1:
                break
            self._search_positions.append(idx)
            cursor = self.text_edit.textCursor()
            cursor.setPosition(idx)
            cursor.setPosition(idx + len(text), cursor.MoveMode.KeepAnchor)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format.setBackground(highlight_color)
            selections.append(sel)
            start = idx + 1

        self.text_edit.setExtraSelections(selections)

        # 滚动到第一个匹配
        if self._search_positions:
            cursor = self.text_edit.textCursor()
            cursor.setPosition(self._search_positions[0])
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()

    # —— 换行 ————————————————————————————————————

    @Slot()
    def __toggle_wrap(self):
        from qfluentwidgets import PlainTextEdit

        self._wrap = not self._wrap
        if self._wrap:
            self.text_edit.setLineWrapMode(PlainTextEdit.LineWrapMode.WidgetWidth)
        else:
            self.text_edit.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)

    # —— 复制 ————————————————————————————————————

    @Slot()
    def __copy_content(self):
        text = self.text_edit.toPlainText()
        if not text:
            show_warning(
                title=self.tr("提示"),
                content=self.tr("没有可复制的内容"),
                parent=self.window(),
            )
            return
        QApplication.clipboard().setText(text)
        show_success(
            title=self.tr("成功"),
            content=self.tr("已复制到剪贴板"),
            parent=self.window(),
        )


if __name__ == "__main__":
    import sys

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from qfluentwidgets import Theme, isDarkTheme, setTheme, toggleTheme, PushButton

    app = QApplication(sys.argv)
    setTheme(Theme.LIGHT)

    sample_raw = (
        "GET /api/v1/users HTTP/1.1\n"
        "Host: example.com\n"
        "User-Agent: Ferret/1.0\n"
        "Accept: application/json\n"
        "Accept-Encoding: gzip, deflate, br\n"
        "Connection: keep-alive\n"
        "Cookie: session=abc123; token=xyz789\n"
        "\n"
    )

    window = QWidget()
    window.setWindowTitle("RawTextPanel Demo")
    window.resize(700, 400)

    panel = RawTextPanel(window)
    panel.set_text(sample_raw)

    theme_btn = PushButton("切换主题", window)
    theme_btn.setFixedWidth(120)

    def _on_toggle():
        toggleTheme()
        bg = "#202020" if isDarkTheme() else "#f5f5f5"
        window.setStyleSheet(f"background: {bg};")

    theme_btn.clicked.connect(_on_toggle)
    window.setStyleSheet("background: #f5f5f5;")

    layout = QVBoxLayout(window)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(12)
    layout.addWidget(theme_btn, 0, Qt.AlignmentFlag.AlignRight)
    layout.addWidget(panel, 1)

    window.show()
    sys.exit(app.exec())
