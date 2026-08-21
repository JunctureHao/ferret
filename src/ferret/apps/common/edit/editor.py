"""带行号的代码编辑器。

高亮不再由本模块挑选实现类：``TokenHighlighter`` 一个实例贯穿始终，换语言只是换
它内部的词法器（见 ``highlighter.py``）。颜色一律问 ``EditorPalette``，本模块不再
自己算 ``QColor``。
"""

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QResizeEvent,
    QTextFormat,
)
from PySide6.QtWidgets import QTextEdit, QWidget
from qfluentwidgets import PlainTextEdit, qconfig, setCustomStyleSheet

from ferret.apps.common.font import FontManager

from .highlighter import TokenHighlighter
from .syntax import Language
from .theme import EditorPalette


class LineNumberArea(QWidget):
    def __init__(self, parent: "CodeEditor"):
        super().__init__(parent)
        self.code_editor = parent

    def sizeHint(self) -> QSize:
        return QSize(self.code_editor.get_line_number_area_width(), 0)

    def paintEvent(self, event):
        self.code_editor.line_number_area_paint_event(event)


class CodeEditor(PlainTextEdit):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.line_number_area = LineNumberArea(self)
        FontManager.register()
        self.editor_font = FontManager.code_font(10)
        self.setFont(self.editor_font)
        self.line_number_area.setFont(self.editor_font)
        # 单个 highlighter 贯穿生命周期：换语言走 set_language，不再重建实例。
        self.highlighter = TokenHighlighter(self.document())

        self.ln_left_padding = 25
        self.ln_right_padding = 25
        self._search_active = False  # 查找模式下挂起当前行高亮，避免覆盖查找高亮

        self.__init_widget()
        self.__connect_signal_to_slot()
        self.set_line_number_area_width(0)
        self.set_highlight_current_line()
        self.set_word_wrap(False)

    def __init_widget(self):
        self.layer.hide()
        self.document().setDocumentMargin(0)
        self.setContentsMargins(0, 0, 0, 0)

        # 背景设为 transparent，继承外层 ToolPlainTextEdit(SimpleCardWidget) 的底色；
        # 去掉独立 border / border-radius，避免与外层卡片形成双层圆角/色差。
        # 覆盖 :hover/:focus 伪状态，确保鼠标进入时不变亮、始终与底图统一。
        # 关闭选中文本块高亮（selection 透明、文字色随默认）。
        _qss = (
            "PlainTextEdit { background: transparent; border: none; padding: 0; }"
            "PlainTextEdit:hover { background: transparent; border: none; }"
            "PlainTextEdit:focus { background: transparent; border: none; }"
        )
        setCustomStyleSheet(self, _qss, _qss)

    def __connect_signal_to_slot(self):
        self.blockCountChanged.connect(self.set_line_number_area_width)
        self.updateRequest.connect(self.set_line_number_area)
        self.cursorPositionChanged.connect(self.set_highlight_current_line)

        qconfig.themeChanged.connect(lambda _: self.line_number_area.update())
        qconfig.themeChanged.connect(self.set_highlight_current_line)
        qconfig.themeChanged.connect(lambda _: self.highlighter.refresh_style())

    def get_line_number_area_width(self) -> int:
        digits = 1
        max_val = max(1, self.blockCount())
        while max_val >= 10:
            max_val /= 10
            digits += 1
        return (
            self.ln_left_padding
            + self.fontMetrics().horizontalAdvance("9") * digits
            + self.ln_right_padding
        )

    def set_line_number_visible(self, visible: bool):
        """控制行号区显隐。隐藏时同时回收左侧 viewport 边距，避免空白错位。"""
        self.line_number_area.setVisible(visible)
        if visible:
            self.set_line_number_area_width(0)
        else:
            self.setViewportMargins(0, 0, 0, 0)

    def set_line_number_area_width(self, _):
        """设置行号区域宽度"""
        width = self.get_line_number_area_width()
        self.setViewportMargins(width, 0, 0, 0)

        # 【终极美化 2】：使用 contentsRect()，确保行号区域在边框内部
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), width, cr.height())
        )

    def resizeEvent(self, e: QResizeEvent, /) -> None:
        super().resizeEvent(e)
        width = self.get_line_number_area_width()
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), width, cr.height())
        )
        hsb = self.scrollDelegate.hScrollBar
        hsb.move(cr.left() + width, hsb.y())
        hsb.resize(cr.width() - width - 2, hsb.height())

    def set_line_number_area(self, rect, dy):
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height()
            )
        self.line_number_area.update()

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        cursor_block = self.textCursor().blockNumber()

        # 每帧取一次色即可（``EditorPalette`` 只读当前主题），不要放进 while 循环。
        h_bg_color = self.get_highlight_line_color()
        active_num_color = EditorPalette.line_number_active()
        normal_num_color = EditorPalette.line_number_normal()
        divider_color = EditorPalette.gutter_divider()

        block = self.firstVisibleBlock()
        blockNumber = block.blockNumber()
        content_offset = self.contentOffset()

        # ================= 图层 1：先画高亮背景和数字 =================
        while block.isValid():
            geom = self.blockBoundingGeometry(block).translated(content_offset)
            top_val = geom.top()
            bottom_val = geom.bottom()

            y_top = int(top_val + 0.5) if top_val >= 0 else int(top_val - 0.5)
            y_bottom = (
                int(bottom_val + 0.5) if bottom_val >= 0 else int(bottom_val - 0.5)
            )
            height = y_bottom - y_top

            if y_top > event.rect().bottom():
                break

            if block.isVisible() and y_bottom >= event.rect().top():
                rect = QRect(0, y_top, self.line_number_area.width(), height)
                is_active = blockNumber == cursor_block

                if is_active:
                    # 仅仅铺上透明高亮色即可，让底部的样式透过来
                    painter.fillRect(rect, h_bg_color)
                    painter.setPen(active_num_color)
                else:
                    painter.setPen(normal_num_color)

                number = str(blockNumber + 1)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, number)

            block = block.next()
            blockNumber += 1

        # ================= 图层 2：最后画分割线（压在最顶层）=================
        # 把画线的代码移到最后，这样分割线就会画在高亮区块的【上面】，永远不会被遮挡！
        x = self.line_number_area.width() - 1
        painter.setPen(QPen(divider_color, 1))
        painter.drawLine(x, event.rect().top(), x, event.rect().bottom())

    def set_highlight_current_line(self):
        # 查找模式下不打断查找高亮（由 set_search_selections 接管 ExtraSelection）
        if self._search_active:
            self.line_number_area.update()
            return
        extra_selections = []
        selection = QTextEdit.ExtraSelection()
        line_color = self.get_highlight_line_color()
        selection.format.setBackground(line_color)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        extra_selections.append(selection)
        self.setExtraSelections(extra_selections)
        self.line_number_area.update()

    def set_search_active(self, active: bool):
        """切换查找模式。激活时挂起当前行高亮，避免覆盖查找高亮。"""
        self._search_active = active
        if not active:
            self.set_highlight_current_line()

    def get_highlight_line_color(self) -> QColor:
        return EditorPalette.current_line()

    def set_word_wrap(self, wrap: bool):
        if wrap:
            self.setLineWrapMode(self.LineWrapMode.WidgetWidth)
        else:
            self.setLineWrapMode(self.LineWrapMode.NoWrap)

    @property
    def language(self) -> Language:
        return self.highlighter.language

    def set_language(self, lang: Language | str) -> None:
        """切换编辑器的高亮语言。

        原实现按语言 new 一个 highlighter 子类、``deleteLater()`` 旧的，还要手动
        ``disconnect`` 对方的私有 slot；现在只是把词法器换一个函数。``str`` 入参
        经 ``Language.coerce`` 收敛（``flow/views.py::_body_lang`` 等旧调用点仍传
        字符串），无法识别时退回 HTTP，与历史 fallback 行为一致。
        """
        self.highlighter.set_language(Language.coerce(lang))


__all__ = ["CodeEditor", "LineNumberArea"]
