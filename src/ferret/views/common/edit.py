import sys

from PySide6.QtCore import QRect, QSize, Qt, Slot
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    SimpleCardWidget,
    TransparentToolButton,
    isDarkTheme,
    setCustomStyleSheet,
)

from ferret.views.common.icon import BaseIcon


class LineNumberArea(SimpleCardWidget):
    """行号区域 — 只负责绘制"""

    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, e):
        self.editor.line_number_area_paint_event(e)


class CodePlainTextEdit(PlainTextEdit):
    """纯文本编辑器 — 基础设置 + 当前行高亮绘制"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__connect_signal_to_slot()

    def __init_widget(self):

        self.setFrameShape(PlainTextEdit.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_wrap(False)
        self.layer.hide()
        _qss = (
            "PlainTextEdit { background: transparent; border: none; padding: 0; }"
            "PlainTextEdit:hover { background: transparent; border: none; }"
            "PlainTextEdit:focus { background: transparent; border: none; }"
            "PlainTextEdit:disabled { background: transparent; border: none; }"
            "PlainTextEdit:read-only { background: transparent; border: none; }"
        )
        setCustomStyleSheet(self, _qss, _qss)

    def __connect_signal_to_slot(self):
        self.cursorPositionChanged.connect(self.viewport().update)

    def contextMenuEvent(self, e):
        e.accept()

    def set_wrap(self, wrap: bool = True):
        """设置是否换行"""
        self.setLineWrapMode(
            self.LineWrapMode.WidgetWidth if wrap else self.LineWrapMode.NoWrap
        )

    def toggle_wrap(self):
        self.set_wrap(self.lineWrapMode() == self.LineWrapMode.NoWrap)

    def current_highlight_block(self) -> int:
        """返回当前应高亮的行号（有选区时取选区起始行）"""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return self.document().findBlock(cursor.selectionStart()).blockNumber()
        return cursor.blockNumber()

    def paintEvent(self, event):
        super().paintEvent(event)

        current_block = self.current_highlight_block()
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.contentOffset()

        painter = QPainter(self.viewport())
        color = QColor(255, 255, 255, 15) if isDarkTheme() else QColor(0, 0, 0, 8)

        while block.isValid():
            top = round(self.blockBoundingGeometry(block).translated(offset).top())
            bottom = top + round(self.blockBoundingRect(block).height())

            if top > event.rect().bottom():
                break

            if (
                block_number == current_block
                and block.isVisible()
                and bottom >= event.rect().top()
            ):
                painter.fillRect(0, top, self.viewport().width(), bottom - top, color)
                break

            block = block.next()
            block_number += 1

        painter.end()


class CodeEditor(SimpleCardWidget):
    """带行号的编辑器 — 覆盖方案"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text_edit = CodePlainTextEdit(self)
        self.line_number_area = LineNumberArea(self)

        self.__connect_signal_to_slot()
        self.__update_line_number_area_width(0)

    def __connect_signal_to_slot(self):
        self.text_edit.blockCountChanged.connect(self.__update_line_number_area_width)
        self.text_edit.updateRequest.connect(self.__update_line_number_area)
        self.text_edit.cursorPositionChanged.connect(self.line_number_area.update)

    # —— 行号区域 ——

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.text_edit.blockCount())))
        return 10 + self.text_edit.fontMetrics().horizontalAdvance("9") * (digits + 1)

    @Slot(int)
    def __update_line_number_area_width(self, _):
        self.text_edit.setViewportMargins(0, 0, 0, 0)
        self.resize(self.size())

    @Slot(QRect, int)
    def __update_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height()
            )
        if rect.contains(self.text_edit.viewport().rect()):
            self.__update_line_number_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        line_width = self.line_number_area_width()
        self.line_number_area.setGeometry(QRect(0, 0, line_width + 1, self.height()))
        self.text_edit.setGeometry(
            QRect(line_width, 0, self.width() - line_width, self.height())
        )

    # —— 绘制行号 ——

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)

        palette = self.text_edit.palette()
        painter.fillRect(event.rect(), Qt.GlobalColor.transparent)

        # 绘制右侧分隔线（与 SimpleCardWidget 边框风格一致）
        sep_color = QColor(0, 0, 0, 48) if isDarkTheme() else QColor(0, 0, 0, 12)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(sep_color, 1))
        right = self.line_number_area.width() - 1
        painter.drawLine(right, 0, right, self.line_number_area.height())

        current_block = self.text_edit.current_highlight_block()
        block = self.text_edit.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.text_edit.contentOffset()

        while block.isValid():
            top = round(
                self.text_edit.blockBoundingGeometry(block).translated(offset).top()
            )
            bottom = top + round(self.text_edit.blockBoundingRect(block).height())

            if top > event.rect().bottom():
                break

            if block.isVisible() and bottom >= event.rect().top():
                if block_number == current_block:
                    highlight = (
                        QColor(255, 255, 255, 15)
                        if isDarkTheme()
                        else QColor(0, 0, 0, 8)
                    )
                    painter.fillRect(
                        0,
                        top,
                        self.line_number_area.width() + 5,
                        bottom - top,
                        highlight,
                    )
                    painter.setPen(palette.color(QPalette.ColorRole.Text))
                else:
                    inactive = palette.color(QPalette.ColorRole.Text)
                    inactive.setAlpha(100)
                    painter.setPen(inactive)

                painter.drawText(
                    0,
                    top,
                    self.line_number_area.width() - 15,
                    bottom - top,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                    str(block_number + 1),
                )

            block = block.next()
            block_number += 1

        painter.end()

    # —— 代理方法 ——

    def set_text(self, text: str):
        self.text_edit.setPlainText(text)

    def to_text(self) -> str:
        return self.text_edit.toPlainText()


class SearchBar(QWidget):
    """搜索栏 — 文本查找与导航"""

    def __init__(self, text_edit: CodePlainTextEdit, parent=None):
        super().__init__(parent)
        self.__text_edit = text_edit
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.hide()

    def __init_widget(self):
        # self.setFixedHeight(40)
        self.input = LineEdit(self)
        self.input.setPlaceholderText("查找...")
        self.input.setFixedWidth(200)

        self.label_count = BodyLabel("0/0", self)
        # self.label_count.setFixedWidth(50)
        # self.label_count.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_prev = TransparentToolButton(FluentIcon.UP, self)
        self.btn_next = TransparentToolButton(FluentIcon.DOWN, self)
        self.btn_close = TransparentToolButton(FluentIcon.CLOSE, self)

    def __init_layout(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch()
        layout.addWidget(self.input)
        layout.addWidget(self.label_count)
        layout.addWidget(self.btn_prev)
        layout.addWidget(self.btn_next)
        layout.addWidget(self.btn_close)

    def __connect_signal_to_slot(self):
        self.input.textChanged.connect(self.__search)
        self.btn_prev.clicked.connect(self.__find_prev)
        self.btn_next.clicked.connect(self.__find_next)
        self.btn_close.clicked.connect(self.close_bar)

    def open_bar(self):
        self.show()
        self.input.setFocus()
        self.input.selectAll()

    def close_bar(self):
        self.hide()
        self.__text_edit.setFocus()

    @Slot(str)
    def __search(self, text: str):
        if not text:
            self.label_count.setText("0/0")
            return
        cursor = self.__text_edit.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.__text_edit.setTextCursor(cursor)
        self.__find_next()

    @Slot()
    def __find_next(self):
        text = self.input.text()
        if not text:
            return
        cursor = self.__text_edit.document().find(text, self.__text_edit.textCursor())
        if cursor.isNull():
            cursor = self.__text_edit.document().find(text)
        self.__text_edit.setTextCursor(cursor)
        self.__update_count()

    @Slot()
    def __find_prev(self):
        text = self.input.text()
        if not text:
            return
        cursor = self.__text_edit.document().find(
            text,
            self.__text_edit.textCursor(),
            self.__text_edit.document().FindFlag.FindBackward,
        )
        if cursor.isNull():
            cursor = self.__text_edit.document().find(
                text,
                self.__text_edit.document().lastBlock().position(),
                self.__text_edit.document().FindFlag.FindBackward,
            )
        self.__text_edit.setTextCursor(cursor)
        self.__update_count()

    def __update_count(self):
        text = self.input.text()
        if not text:
            self.label_count.setText("0/0")
            return
        doc = self.__text_edit.document()
        cursor = doc.find(text)
        total = 0
        positions = []
        while not cursor.isNull():
            total += 1
            positions.append(cursor.selectionStart())
            cursor = doc.find(text, cursor)
        current_pos = self.__text_edit.textCursor().selectionStart()
        current = 0
        for i, pos in enumerate(positions):
            if pos <= current_pos:
                current = i + 1
        self.label_count.setText(f"{current}/{total}")


class CodeEditorPanel(SimpleCardWidget):
    """代码编辑器面板 — 工具栏 + 搜索栏 + 编辑器的完整组件

    Signals:
        copyClicked: 复制按钮点击
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        # —— 工具栏 ——
        self._toolbar = QWidget(self)
        self._btn_search = TransparentToolButton(BaseIcon.DOCUMENT_SEARCH, self)
        self._btn_search.setToolTip("查找")
        self._btn_copy = TransparentToolButton(FluentIcon.COPY, self)
        self._btn_copy.setToolTip("复制全部")
        self._btn_wrap = TransparentToolButton(FluentIcon.ALIGNMENT, self)
        self._btn_wrap.setToolTip("切换自动换行")

        # —— 编辑器 ——
        self._code_editor = CodeEditor(self)

        # —— 搜索栏 ——
        self._search_bar = SearchBar(self._code_editor.text_edit, self)
        self._search_bar.hide()

    def __init_layout(self):
        self.toolbar_layout = QHBoxLayout(self.toolbar)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_layout.setSpacing(0)
        self.toolbar_layout.addStretch()
        self.toolbar_layout.addWidget(self._btn_search)
        self.toolbar_layout.addWidget(self._btn_copy)
        self.toolbar_layout.addWidget(self._btn_wrap)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._search_bar)
        layout.addWidget(self._code_editor, stretch=1)

    def __connect_signal_to_slot(self):
        self._btn_search.clicked.connect(self.__toggle_search)
        self._btn_copy.clicked.connect(self.__on_btn_copy_clicked)
        self._btn_wrap.clicked.connect(self._code_editor.text_edit.toggle_wrap)

    # ========== 私有方法 ==========

    @Slot()
    def __on_btn_copy_clicked(self):
        QApplication.clipboard().setText(self.to_text())

    @Slot()
    def __toggle_search(self):
        if self._search_bar.isVisible():
            self._search_bar.close_bar()
        else:
            self._search_bar.open_bar()

    # ========== 公共接口 ==========

    @property
    def toolbar(self) -> QWidget:
        """获取工具栏（用于添加自定义按钮）"""
        return self._toolbar

    def add_toolbar_widget(self, widget: QWidget) -> None:
        """在工具栏 stretch 前添加组件"""
        self.toolbar_layout.insertWidget(self.toolbar_layout.count() - 1, widget)

    def add_toolbar_trailing_widget(self, widget: QWidget) -> None:
        """在工具栏 stretch 后添加组件（右侧）"""
        self.toolbar_layout.addWidget(widget)

    def set_text(self, text: str) -> None:
        """设置编辑器文本"""
        self._code_editor.set_text(text)

    def to_text(self) -> str:
        """获取编辑器文本"""
        return self._code_editor.to_text()

    def set_read_only(self, read_only: bool = True) -> None:
        """设置编辑器只读模式"""
        self._code_editor.text_edit.setReadOnly(read_only)


class CodeViewPanel(CodeEditorPanel):
    """代码查看面板 — 只读的 CodeEditorPanel"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.set_read_only(True)


class CodeEditorContainer(QWidget):
    """编辑器容器 — 双面板切换"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        # —— 编辑器面板 ——
        self.code_editor_panel = CodeEditorPanel(self)
        self.btn_switch_to_plan = TransparentToolButton(FluentIcon.VIEW, self)
        self.code_editor_panel.add_toolbar_trailing_widget(self.btn_switch_to_plan)

        # —— 工具栏 2：计划面板 ——
        self.toolbar_plan = QWidget(self)
        self.btn_plan_add = TransparentToolButton(FluentIcon.ADD, self)
        self.btn_plan_remove = TransparentToolButton(FluentIcon.REMOVE, self)
        self.btn_plan_save = TransparentToolButton(FluentIcon.SAVE, self)
        self.btn_plan_refresh = TransparentToolButton(FluentIcon.SYNC, self)
        self.btn_switch_to_editor = TransparentToolButton(FluentIcon.EDIT, self)
        self.toolbar_plan.hide()

        # —— 面板 ——
        self.stack = QStackedWidget(self)
        self.plan_panel = QWidget()
        self.stack.addWidget(self.code_editor_panel)
        self.stack.addWidget(self.plan_panel)

    def __init_layout(self):
        plan_layout = QHBoxLayout(self.toolbar_plan)
        plan_layout.setContentsMargins(0, 0, 0, 0)
        plan_layout.setSpacing(0)
        plan_layout.addWidget(self.btn_plan_add)
        plan_layout.addWidget(self.btn_plan_remove)
        plan_layout.addWidget(self.btn_plan_save)
        plan_layout.addWidget(self.btn_plan_refresh)
        plan_layout.addStretch()
        plan_layout.addWidget(self.btn_switch_to_editor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar_plan)
        layout.addWidget(self.stack)

    def __connect_signal_to_slot(self):
        self.btn_switch_to_plan.clicked.connect(lambda: self.__switch_panel(1))
        self.btn_switch_to_editor.clicked.connect(lambda: self.__switch_panel(0))

    @Slot(int)
    def __switch_panel(self, index: int):
        self.stack.setCurrentIndex(index)
        self.code_editor_panel.setVisible(index == 0)
        self.toolbar_plan.setVisible(index == 1)

    # —— 代理方法 ——

    def set_text(self, text: str):
        self.code_editor_panel.set_text(text)

    def to_text(self) -> str:
        return self.code_editor_panel.to_text()


if __name__ == "__main__":
    from qfluentwidgets import Theme, TransparentToolButton, setTheme
    from qfluentwidgets.window.fluent_window import FluentWidget

    from ferret.config import resources_rc  # noqa: F401

    app = QApplication(sys.argv)
    setTheme(Theme.DARK)

    window = FluentWidget()
    window.setWindowTitle("CodeViewPanel Demo")
    window.resize(700, 500)

    # 在窗口控制按钮左侧插入主题切换按钮
    btn_theme = TransparentToolButton(FluentIcon.CONSTRACT)
    btn_theme.setToolTip("切换主题")
    title_layout = window.titleBar.hBoxLayout
    title_layout.insertWidget(
        title_layout.count() - 1, btn_theme, 0, Qt.AlignmentFlag.AlignVCenter
    )

    @btn_theme.clicked.connect
    def _():
        setTheme(Theme.LIGHT if isDarkTheme() else Theme.DARK)

    # 内容区域，顶部预留标题栏高度
    layout = QVBoxLayout(window)
    title_height = window.titleBar.height()
    layout.setContentsMargins(12, title_height + 4, 12, 12)
    layout.setSpacing(8)

    editor = CodeViewPanel()
    editor.set_text(
        "\n".join(
            [
                f"Line {i}: Hello World1111111111111111111111111111111111111111111111111111111111111111111111111111111"
                for i in range(1, 101)
            ]
        )
    )
    layout.addWidget(editor, stretch=1)

    window.show()
    sys.exit(app.exec())
