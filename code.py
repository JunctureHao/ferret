# test JUN test


import os
import sys
import urllib

from PySide6.QtCore import QRect, QRegularExpression, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextFormat,
)
from PySide6.QtWidgets import QApplication, QTextEdit, QVBoxLayout, QWidget
from qfluentwidgets import (
    ComboBox,
    PlainTextEdit,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    isDarkTheme,
)


# --- 1. 语法高亮 (保持不变) ---
class PythonHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rules = []
        dark = isDarkTheme()
        colors = {
            "keyword": "#CF8E6D" if dark else "#0033B3",
            "string": "#6AAB73" if dark else "#067D17",
            "comment": "#7A7E85" if dark else "#8C8C8C",
            "function": "#56A8F5" if dark else "#127DA5",
        }

        def add(p, c):
            f = QTextCharFormat()
            f.setForeground(QColor(c))
            self.rules.append((QRegularExpression(p), f))

        add(
            r"\b(class|def|from|import|if|else|return|as|for|while|try|except|with|None|True|False)\b",
            colors["keyword"],
        )
        add(r"'[^']*'|\"[^\"]*\"", colors["string"])
        add(r"#[^\n]*", colors["comment"])
        add(r"\b[A-Za-z0-9_]+(?=\\()", colors["function"])

    def highlightBlock(self, text):
        for p, f in self.rules:
            it = p.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


# --- 2. 深度定制的编辑器 ---
class CodeEditor(PlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.lineNumberArea = LineNumberArea(self)

        # 彻底去掉所有内边距
        self.document().setDocumentMargin(0)
        self.setLineWrapMode(PlainTextEdit.LineWrapMode.NoWrap)

        self.blockCountChanged.connect(self.updateLineNumberAreaWidth)
        self.updateRequest.connect(self.updateLineNumberArea)
        self.cursorPositionChanged.connect(self.highlightCurrentLine)

        self.updateLineNumberAreaWidth(0)

        # 字体
        font = QFont("Consolas", 11)
        font.setFixedPitch(True)
        self.setFont(font)
        self.highlighter = PythonHighlighter(self.document())

        # 彻底移除边框和背景，由外部容器控制
        self.setFrameShape(PlainTextEdit.NoFrame)
        self.viewport().setStyleSheet("background: transparent;")
        self.setStyleSheet("background: transparent; border: none;")

        self.highlightCurrentLine()

    def lineNumberAreaWidth(self):
        digits = 1
        max_val = max(1, self.blockCount())
        while max_val >= 10:
            max_val /= 10
            digits += 1
        # 20px 是左侧的安全留白，让行号不贴边
        return 20 + self.fontMetrics().horizontalAdvance("9") * digits + 10

    def updateLineNumberAreaWidth(self, _):
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)

    def updateLineNumberArea(self, rect, dy):
        if dy:
            self.lineNumberArea.scroll(0, dy)
        else:
            self.lineNumberArea.update(
                0, rect.y(), self.lineNumberArea.width(), rect.height()
            )
        if rect.contains(self.viewport().rect()):
            self.updateLineNumberAreaWidth(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        # 确保行号区填满左侧区域
        self.lineNumberArea.setGeometry(
            QRect(cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height())
        )

    def highlightCurrentLine(self):
        extraSelections = []
        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            # 极淡的高亮条颜色
            color = QColor(255, 255, 255, 15) if isDarkTheme() else QColor(0, 0, 0, 8)
            selection.format.setBackground(color)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extraSelections.append(selection)
        self.setExtraSelections(extraSelections)
        self.lineNumberArea.update()

    def lineNumberAreaPaintEvent(self, event):
        painter = QPainter(self.lineNumberArea)
        # 不要在这里填充背景，让底层的 Card 背景透上来

        current_block_num = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        blockNumber = block.blockNumber()
        offset = self.contentOffset()

        while block.isValid():
            top = round(self.blockBoundingGeometry(block).translated(offset).top())
            bottom = top + round(self.blockBoundingRect(block).height())

            if top > event.rect().bottom():
                break

            if block.isVisible() and bottom >= event.rect().top():
                # 绘制行号区的高亮部分
                if blockNumber == current_block_num:
                    color = (
                        QColor(255, 255, 255, 15)
                        if isDarkTheme()
                        else QColor(0, 0, 0, 8)
                    )
                    # 矩形稍微向右宽一点，确保覆盖掉交界线
                    painter.fillRect(
                        0, top, self.lineNumberArea.width() + 5, bottom - top, color
                    )

                number = str(blockNumber + 1)
                # 行号文字颜色
                if blockNumber == current_block_num:
                    painter.setPen(
                        QColor(200, 200, 200) if isDarkTheme() else QColor(30, 30, 30)
                    )
                else:
                    painter.setPen(QColor(128, 128, 128, 120))

                # 绘制数字，留出右侧 15px 间距
                painter.drawText(
                    0,
                    top,
                    self.lineNumberArea.width() - 15,
                    bottom - top,
                    Qt.AlignRight | Qt.AlignVCenter,
                    number,
                )

            block = block.next()
            blockNumber += 1


class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.codeEditor = editor

    def paintEvent(self, event):
        self.codeEditor.lineNumberAreaPaintEvent(event)


# --- 3. 仿官方 Card 布局容器 ---
class CodeCard(SimpleCardWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # 设置卡片内部背景色
        self.set_bg()

        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(0, 10, 0, 10)  # 左右 0，上下 10
        self.mainLayout.setSpacing(0)

        # 头部区域
        self.header = QWidget()
        self.headerLayout = QVBoxLayout(self.header)
        self.headerLayout.setContentsMargins(20, 5, 20, 15)
        self.langCombo = ComboBox()
        self.langCombo.addItems(["Python"])
        self.langCombo.setFixedWidth(120)
        self.headerLayout.addWidget(StrongBodyLabel("选择语言："))
        self.headerLayout.addWidget(self.langCombo)

        # 编辑器
        self.editor = CodeEditor(self)

        self.mainLayout.addWidget(self.header)
        self.mainLayout.addWidget(self.editor)

    def set_bg(self):
        # 强制同步卡片和编辑器的颜色
        bg = QColor(32, 32, 32) if isDarkTheme() else QColor(255, 255, 255)
        self.setBackgroundColor(bg)


class Demo(QWidget):
    def __init__(self):
        super().__init__()
        # 窗口大背景色
        self.setStyleSheet(
            f"background-color: {'#202020' if isDarkTheme() else '#f3f3f3'};"
        )
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(40, 40, 40, 40)

        self.title = SubtitleLabel("源代码")
        self.card = CodeCard(self)

        self.layout.addWidget(self.title)
        self.layout.addSpacing(15)
        self.layout.addWidget(self.card)
        self.resize(1000, 700)

        self.card.editor.setPlainText("""# coding: utf-8
from PySide6.QtCore import Qt
from qfluentwidgets import FluentWindow

class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        # 1. 行号区现在有了左侧边距
        # 2. 高亮条从最左侧开始贯穿
        # 3. 颜色完全统一，没有白色/灰色杂条
        self.initWindow()
        self.isChartVisible = False
""")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Demo()
    w.show()
    app.exec()
