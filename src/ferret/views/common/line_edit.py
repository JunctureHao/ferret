"""可复用的文本编辑组件 — 带行号、主题适配、可选 JSON 高亮"""

from PySide6.QtCore import QRect, QRegularExpression, Qt, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QShortcut,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextFormat,
)
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
    PlainTextEdit,
    SearchLineEdit,
    SimpleCardWidget,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    isDarkTheme,
)
from qfluentwidgets.common.style_sheet import setCustomStyleSheet

# —— JSON 语法高亮 ——————————————————————————————————


class JsonHighlighter(QSyntaxHighlighter):
    """JSON 语法高亮器 — 适配深色/浅色主题"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        dark = isDarkTheme()

        colors = {
            "key": "#CF8E6D" if dark else "#0033B3",
            "value": "#6AAB73" if dark else "#067D17",
            "number": "#2EB5C1" if dark else "#1750EB",
            "bool": "#CC7832" if dark else "#0033B3",
        }

        def add_rule(pattern: str, color: str):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self._rules.append((QRegularExpression(pattern), fmt))

        add_rule(r'"[^"]*"\s*(?=:)', colors["key"])
        add_rule(r"(?<=:)\s*\"[^\"]*\"", colors["value"])
        add_rule(r"\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b", colors["number"])
        add_rule(r"\b(true|false|null)\b", colors["bool"])

    def highlightBlock(self, text: str):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


# —— 行号区域 ——————————————————————————————————————


class LineNumberArea(QWidget):
    """行号绘制区域 — 与 LineNumberTextEdit 配合使用"""

    def __init__(self, editor: "LineNumberTextEdit"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        from PySide6.QtCore import QSize

        return QSize(self._editor.lineNumberAreaWidth(), 0)

    def paintEvent(self, event):
        self._editor.lineNumberAreaPaintEvent(event)


# —— 文本编辑器 ————————————————————————————————————


class LineNumberTextEdit(PlainTextEdit):
    """文本显示器 — 带左侧行号，适配 qfluentwidgets 主题

    特性：
    - 左侧行号区域（动态宽度）
    - 等宽字体、透明背景
    - 自动隐藏 qfluentwidgets 聚焦横条和 hover/focus 样式
    - 当前行高亮（可选）
    - JSON 语法高亮（可选）
    - 双击选中完整逻辑行（兼容换行模式）
    """

    def __init__(
        self,
        parent=None,
        *,
        read_only: bool = True,
        highlight_current_line: bool = False,
        json_highlight: bool = False,
    ):
        super().__init__(parent)
        self._read_only = read_only
        self._highlight_current_line = highlight_current_line
        self._json_highlight = json_highlight

        self.__init_widget()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        # 行号区域
        self.lineNumberArea = LineNumberArea(self)

        # 基础设置
        self.setReadOnly(self._read_only)
        self.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
        self.setFrameShape(PlainTextEdit.Shape.NoFrame)
        self.document().setDocumentMargin(0)

        # 允许选择文本并显示光标
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
            | Qt.TextInteractionFlag.TextEditable
        )
        self.setCursorWidth(2)

        # 等宽字体
        font = QFont("Consolas", 10)
        font.setFixedPitch(True)
        self.setFont(font)

        # viewport 透明
        self.viewport().setAutoFillBackground(False)
        self.viewport().setStyleSheet("background: transparent;")

        # 隐藏 EditLayer overlay
        self.layer.hide()

        # 覆盖内置 QSS 所有状态的 border / background
        _custom_qss = (
            "PlainTextEdit { background: transparent; border: none; }"
            "PlainTextEdit:hover { background: transparent; border: none; }"
            "PlainTextEdit:focus { background: transparent; border: none; }"
            "PlainTextEdit:disabled { background: transparent; border: none; }"
        )
        setCustomStyleSheet(self, _custom_qss, _custom_qss)

        # JSON 高亮
        if self._json_highlight:
            self.highlighter = JsonHighlighter(self.document())

        # 初始行号宽度
        self.__updateLineNumberAreaWidth(0)

    def __connect_signal_to_slot(self):
        self.blockCountChanged.connect(self.__updateLineNumberAreaWidth)
        self.updateRequest.connect(self.__updateLineNumberArea)
        if self._highlight_current_line:
            self.cursorPositionChanged.connect(self.__highlightCurrentLine)
            self.__highlightCurrentLine()

    # —— 行号区域 ——

    def lineNumberAreaWidth(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 30 + self.fontMetrics().horizontalAdvance("9") * digits

    @Slot(int)
    def __updateLineNumberAreaWidth(self, _):
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)

    @Slot(object, int)
    def __updateLineNumberArea(self, rect, dy):
        if dy:
            self.lineNumberArea.scroll(0, dy)
        else:
            self.lineNumberArea.update(
                0, rect.y(), self.lineNumberArea.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self.__updateLineNumberAreaWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.lineNumberArea.setGeometry(
            QRect(cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height())
        )

    def lineNumberAreaPaintEvent(self, event):
        painter = QPainter(self.lineNumberArea)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        current_block = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.contentOffset()

        while block.isValid():
            top = round(self.blockBoundingGeometry(block).translated(offset).top())
            bottom = top + round(self.blockBoundingRect(block).height())

            if top > event.rect().bottom():
                break

            if block.isVisible() and bottom >= event.rect().top():
                # 当前行高亮背景
                if self._highlight_current_line and block_number == current_block:
                    color = (
                        QColor(255, 255, 255, 15)
                        if isDarkTheme()
                        else QColor(0, 0, 0, 8)
                    )
                    painter.fillRect(
                        0, top, self.lineNumberArea.width() + 5, bottom - top, color
                    )

                # 行号文字颜色
                if block_number == current_block:
                    painter.setPen(
                        QColor(200, 200, 200) if isDarkTheme() else QColor(30, 30, 30)
                    )
                else:
                    painter.setPen(QColor(128, 128, 128, 120))

                painter.drawText(
                    0,
                    top,
                    self.lineNumberArea.width() - 15,
                    bottom - top,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    str(block_number + 1),
                )

            block = block.next()
            block_number += 1

        painter.end()

    # —— 当前行高亮 ——

    @Slot()
    def __highlightCurrentLine(self):
        selections = []
        selection = QTextEdit.ExtraSelection()
        color = QColor(255, 255, 255, 15) if isDarkTheme() else QColor(0, 0, 0, 8)
        selection.format.setBackground(color)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        selections.append(selection)
        self.setExtraSelections(selections)
        self.lineNumberArea.update()

    # —— 双击选中整行 ——

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        """双击选中完整逻辑行（非视觉行，兼容换行模式）"""
        cursor = self.textCursor()
        cursor.movePosition(cursor.MoveOperation.StartOfBlock)
        cursor.movePosition(cursor.MoveOperation.EndOfBlock, cursor.MoveMode.KeepAnchor)
        self.setTextCursor(cursor)
        event.accept()


# —— CodeCard 卡片组件 ——————————————————————————————


class CodeCard(SimpleCardWidget):
    """代码卡片 — LineNumberTextEdit + 工具栏（换行/复制/查找）

    特性：
    - 可选 JSON 语法高亮
    - 可选当前行高亮
    - 工具栏：换行、复制、查找（Ctrl+F）
    - 双击选中完整逻辑行
    """

    def __init__(
        self,
        parent=None,
        *,
        json_highlight: bool = False,
        highlight_current_line: bool = True,
        show_search: bool = True,
    ):
        super().__init__(parent)
        self.setBorderRadius(0)
        self._wrap = False
        self._search_positions: list[int] = []
        self._show_search = show_search

        self.__init_widget(json_highlight, highlight_current_line)
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self, json_highlight: bool, highlight_current_line: bool):
        # ── 文本编辑器 ──
        self.text_edit = LineNumberTextEdit(
            self,
            read_only=True,
            highlight_current_line=highlight_current_line,
            json_highlight=json_highlight,
        )

        # ── 工具栏按钮 ──
        self.button_bar = QWidget()
        self.button_bar.setFixedHeight(32)

        if self._show_search:
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
        if self._show_search:
            self.search_bar_widget = QWidget()
            self.search_edit = SearchLineEdit(self.search_bar_widget)
            self.search_edit.setPlaceholderText(self.tr("查找…"))
            self.search_close_btn = TransparentToolButton(
                FluentIcon.CLOSE, self.search_bar_widget
            )
            self.search_bar_widget.hide()

    def __init_layout(self):
        # 工具栏布局
        button_layout = QHBoxLayout(self.button_bar)
        button_layout.setContentsMargins(4, 2, 4, 2)
        button_layout.setSpacing(2)
        button_layout.addStretch(1)

        if self._show_search:
            button_layout.addWidget(self.search_toggle_btn)
        button_layout.addWidget(self.wrap_btn)
        button_layout.addWidget(self.copy_btn)

        # 搜索栏布局（含底部分隔线）
        if self._show_search:
            self._search_separator = QFrame()
            self._search_separator.setFrameShape(QFrame.Shape.HLine)
            self._search_separator.setFixedHeight(1)
            self._search_separator.setStyleSheet(
                "background: palette(mid); border: none;"
            )

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

        # 主布局
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.button_bar)
        if self._show_search:
            layout.addWidget(self.search_bar_widget)
        layout.addWidget(self.text_edit, 1)

    def __connect_signal_to_slot(self):
        self.wrap_btn.clicked.connect(self.__toggle_wrap)
        self.copy_btn.clicked.connect(self.__copy_content)

        if self._show_search:
            self.search_toggle_btn.clicked.connect(self.__toggle_search_bar)
            self.search_close_btn.clicked.connect(
                lambda: self.__set_search_bar_visible(False)
            )
            self.search_edit.textChanged.connect(self.__on_search)

            # Ctrl+F 快捷键
            self._search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
            self._search_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            self._search_shortcut.activated.connect(self.__on_ctrl_f)

    # —— 公开接口 ————————————————————————————————————

    def set_text(self, text: str):
        """设置文本内容"""
        self.text_edit.setPlainText(text)

    # —— 换行 ——————————————————————————————————————

    @Slot()
    def __toggle_wrap(self):
        self._wrap = not self._wrap
        if self._wrap:
            self.text_edit.setLineWrapMode(PlainTextEdit.LineWrapMode.WidgetWidth)
            self.wrap_btn.setToolTip(self.tr("关闭换行"))
        else:
            self.text_edit.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)
            self.wrap_btn.setToolTip(self.tr("切换自动换行"))

    # —— 复制 ——————————————————————————————————————

    @Slot()
    def __copy_content(self):
        from ferret.views.common.info_bar import show_success, show_warning

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

    # —— 搜索 ——————————————————————————————————————

    @Slot()
    def __toggle_search_bar(self):
        self.__set_search_bar_visible(not self.search_bar_widget.isVisible())

    @Slot()
    def __on_ctrl_f(self):
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


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    editor = CodeCard()
    editor.resize(600, 400)
    editor.show()
    sys.exit(app.exec())