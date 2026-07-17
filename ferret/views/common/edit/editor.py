from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QResizeEvent,
    QTextFormat,
)
from PySide6.QtWidgets import QTextEdit, QWidget
from qfluentwidgets import PlainTextEdit, isDarkTheme, qconfig, setCustomStyleSheet

from ferret.views.common.font import FontManager

from .highlighter import (
    HeadersHighlighter,
    HTTPHighlighter,
    JSONHighlighter,
)


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
        self.highlighter = HTTPHighlighter(self.document())
        self._highlighter_class = HTTPHighlighter

        self.ln_left_padding = 25
        self.ln_right_padding = 25
        self._fold_regions = []  # 折叠区域数据（待实现折叠 UI）
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
        is_dark = isDarkTheme()

        # 删除了 base_bg_color，不再需要强行擦除背景
        h_bg_color = self.get_highlight_line_color()
        active_num_color = (
            QColor(255, 255, 255, 255) if is_dark else QColor(0, 0, 0, 255)
        )
        normal_num_color = (
            QColor(255, 255, 255, 120) if is_dark else QColor(0, 0, 0, 120)
        )
        divider_color = QColor(255, 255, 255, 40) if is_dark else QColor(0, 0, 0, 30)

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
        return QColor(255, 255, 255, 15) if isDarkTheme() else QColor(0, 0, 0, 10)

    def set_word_wrap(self, wrap: bool):
        if wrap:
            self.setLineWrapMode(self.LineWrapMode.WidgetWidth)
        else:
            self.setLineWrapMode(self.LineWrapMode.NoWrap)

    def set_language(self, lang: str):
        """切换编辑器的高亮语言。

        支持: "http"(完整报文) / "headers"(纯 Key: Value) / "json" / "xml"。
        不同语言对应不同 highlighter，避免把纯 header 文本误判为 Token.Error 全红。
        """
        mapping = {
            "http": HTTPHighlighter,
            "headers": HeadersHighlighter,
            "json": JSONHighlighter,
            "xml": HTTPHighlighter,  # 含 html/xml 的报文仍走 http lexer
        }
        cls = mapping.get(lang, HTTPHighlighter)
        if cls is self._highlighter_class:
            return
        # 断开旧 highlighter 的内容变更监听，替换为新的
        try:
            self.highlighter.document().contentsChanged.disconnect(
                self.highlighter._on_contents_changed
            )
        except Exception:
            pass
        self.highlighter.deleteLater()
        self.highlighter = cls(self.document())
        self._highlighter_class = cls

    def enable_fold(self, regions: list):
        """接收折叠区域数据（来自 Packet 预解析），供后续折叠 UI 使用。

        当前仅保存数据，完整折叠交互在下一步实现。
        """
        self._fold_regions = regions or []


__all__ = [LineNumberArea, CodeEditor]
