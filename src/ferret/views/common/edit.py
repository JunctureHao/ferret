import json
import sys
from enum import Enum, auto

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QPainter, QPalette, QPen, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PlainTextEdit,
    SimpleCardWidget,
    TableWidget,
    TransparentToolButton,
    isDarkTheme,
    setCustomStyleSheet,
)

from ferret.views.common.button import TransparentTooltipButton
from ferret.views.common.icon import BaseIcon
from ferret.views.common.info_bar import show_success, show_warning


class LineNumberArea(QWidget):
    """行号区域 — 只负责绘制，作为 text_edit 的子控件覆盖在左侧 margin 上"""

    def __init__(self, editor: "LineNumberTextEdit"):
        super().__init__(editor.text_edit)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, e):
        self.editor.line_number_area_paint_event(e)


class PlainTextEditWithSearch(PlainTextEdit):
    """纯文本编辑器 — 基础设置 + 当前行高亮绘制

    复用 qfluentwidgets SmoothScrollDelegate 自带的横向滚动条，
    覆盖其 _adjustPos 定位逻辑，让滚动条避开左侧行号区域。
    """

    def __init__(self, parent=None):
        self._left_margin = 0
        super().__init__(parent)
        self.__init_widget()
        self.__connect_signal_to_slot()

    def __init_widget(self):

        self.setFrameShape(PlainTextEdit.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_wrap(False)
        self.layer.hide()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        _qss = (
            "PlainTextEdit { background: transparent; border: none; padding: 0; }"
            "PlainTextEdit:hover { background: transparent; border: none; }"
            "PlainTextEdit:focus { background: transparent; border: none; }"
            "PlainTextEdit:disabled { background: transparent; border: none; }"
            "PlainTextEdit:read-only { background: transparent; border: none; }"
        )
        setCustomStyleSheet(self, _qss, _qss)

        # 复用 delegate 提供的 fluent 风格横向滚动条，仅覆盖其定位逻辑
        # （不要调用 setHorizontalScrollBarPolicy，那会触发 delegate 的 forceHidden）
        self._hbar = self.scrollDelegate.hScrollBar
        self._hbar._adjustPos = self._reposition_hbar
        # 确保滚动条容器本身透明，只有 groove/handle 自绘内容可见
        self._hbar.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._hbar.setAutoFillBackground(False)
        self._hbar.setStyleSheet("background: transparent;")

    def __connect_signal_to_slot(self):
        self.cursorPositionChanged.connect(self.viewport().update)
        # updateRequest 在内容/视口变化时触发，是更新 margin 的最可靠时机
        self.updateRequest.connect(self.__on_update_request)

    def __on_update_request(self, rect, dy):
        """视口更新时检查并同步 bottom margin"""
        self.__sync_bottom_margin()

    def contextMenuEvent(self, e):
        e.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.__sync_bottom_margin()
        self._reposition_hbar()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.__sync_bottom_margin)
        self._reposition_hbar()

    def setPlainText(self, text: str):
        super().setPlainText(text)
        QTimer.singleShot(0, self.__sync_bottom_margin)

    def set_left_margin(self, width: int):
        """设置左侧 viewport margin（行号区域宽度）"""
        self._left_margin = width
        self.__sync_bottom_margin()
        self._reposition_hbar()

    def __sync_bottom_margin(self):
        """根据文档内容是否超宽，动态设置 viewport bottom margin

        不依赖滚动条的 range 信号（存在竞态），直接用文档理想宽度判断。
        """
        vp = self.viewport()
        if vp is None or vp.width() <= 0:
            return
        need_hbar = self.document().idealWidth() > vp.width()
        need_bottom = 13 if need_hbar else 0
        current = self.viewportMargins()
        if current.bottom() == need_bottom and current.left() == self._left_margin:
            return  # 没变化，避免触发 layoutChildren 导致回环
        self.setViewportMargins(self._left_margin, 0, 0, need_bottom)

    def _reposition_hbar(self, size=None):
        """覆盖 ScrollBar._adjustPos — 横向滚动条只在文本区域下方，避开行号区域"""
        if size is None:
            size = self.size()
        vbar = self.scrollDelegate.vScrollBar
        vbar_w = 13 if vbar.maximum() > 0 else 0
        self._hbar.resize(size.width() - self._left_margin - vbar_w - 2, 12)
        self._hbar.move(self._left_margin + 1, size.height() - 13)

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
        color = QColor(255, 255, 255, 18) if isDarkTheme() else QColor(0, 0, 0, 10)

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


class LineNumberTextEdit(QWidget):
    """带行号的编辑器 — 覆盖方案"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text_edit = PlainTextEditWithSearch(self)
        self.line_number_area = LineNumberArea(self)

        # 内置搜索栏
        self.search_bar = SearchBar(self.text_edit, self)
        self.search_bar.hide()

        self.__init_layout()
        self.__connect_signal_to_slot()
        self.__update_line_number_area_width(0)

    def __init_layout(self):
        # 主布局：搜索栏 + 编辑器（line_number_area 作为 text_edit 子控件，不放进布局）
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # 搜索栏
        main_layout.addWidget(self.search_bar)

        # 文本编辑器（行号区域覆盖在其左侧 viewport margin 上）
        main_layout.addWidget(self.text_edit)

    def __connect_signal_to_slot(self):
        self.text_edit.blockCountChanged.connect(self.__update_line_number_area_width)
        self.text_edit.updateRequest.connect(self.__update_line_number_area)
        self.text_edit.cursorPositionChanged.connect(self.line_number_area.update)

    def set_read_only(self, read_only: bool):
        self.text_edit.setReadOnly(read_only)

    def toggle_search(self):
        """显示/隐藏搜索栏"""
        if self.search_bar.isVisible():
            self.search_bar.close_bar()
            self.search_bar.hide()
            self.resize(self.width(), self.height() - self.search_bar.height())
            self.text_edit.updateGeometry()
            self.text_edit.viewport().update()
        else:
            self.search_bar.show()
            self.search_bar.open_bar()
            self.resize(self.width(), self.height() + self.search_bar.height())
            self.text_edit.updateGeometry()
            self.text_edit.viewport().update()

    # —— 行号区域 ——

    def line_number_area_width(self) -> int:
        block_count = self.text_edit.blockCount()
        max_digits = max(1, len(str(block_count)))
        max_width = self.text_edit.fontMetrics().horizontalAdvance("9" * max_digits)
        # 左 padding 12 + 右 padding 10 + 分隔线 1 + 安全余量 2
        padding = 25
        min_width = 48  # 即使单行号也保持舒适宽度
        return max(min_width, max_width + padding)

    @Slot(int)
    def __update_line_number_area_width(self, _):
        line_width = self.line_number_area_width()
        self.text_edit.set_left_margin(line_width)

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
        # line_number_area 定位到 text_edit 内容区域的左侧
        cr = self.text_edit.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(), line_width, cr.height())
        )
        self.text_edit.updateGeometry()

    # —— 绘制行号 ——

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        palette = self.text_edit.palette()

        # 绘制右侧分隔线（淡化，不抢视觉）
        sep_color = QColor(0, 0, 0, 30) if isDarkTheme() else QColor(0, 0, 0, 10)
        painter.setPen(QPen(sep_color, 1))
        right = self.line_number_area.width() - 1
        painter.drawLine(right, 0, right, self.line_number_area.height())

        # 行号字体：比正文小一号
        ln_font = self.text_edit.font()
        ps = ln_font.pointSize()
        if ps > 0:
            ln_font.setPointSize(max(1, ps - 1))
        else:
            px = ln_font.pixelSize()
            if px > 0:
                ln_font.setPixelSize(max(1, px - 1))
        painter.setFont(ln_font)

        # 行号颜色
        text_color = palette.color(QPalette.ColorRole.Text)
        active_color = QColor(text_color)
        active_color.setAlpha(220)
        inactive_color = QColor(text_color)
        inactive_color.setAlpha(90)

        current_block = self.text_edit.current_highlight_block()
        block = self.text_edit.firstVisibleBlock()
        block_number = block.blockNumber()
        offset = self.text_edit.contentOffset()

        # 当前行高亮背景（行号区部分）
        highlight = QColor(255, 255, 255, 18) if isDarkTheme() else QColor(0, 0, 0, 10)

        # 左右内边距
        left_pad = 12
        right_pad = 10
        text_rect_w = self.line_number_area.width() - left_pad - right_pad

        while block.isValid():
            top = round(
                self.text_edit.blockBoundingGeometry(block).translated(offset).top()
            )
            bottom = top + round(self.text_edit.blockBoundingRect(block).height())

            if top > event.rect().bottom():
                break

            if block.isVisible() and bottom >= event.rect().top():
                # 当前行高亮背景
                if block_number == current_block:
                    painter.fillRect(
                        0, top, self.line_number_area.width(), bottom - top, highlight
                    )
                    painter.setPen(active_color)
                else:
                    painter.setPen(inactive_color)

                # 右对齐 + 垂直居中
                painter.drawText(
                    left_pad,
                    top,
                    text_rect_w,
                    bottom - top,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
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

    def __init__(self, text_edit: PlainTextEdit, parent=None):
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


class ToolWidget(SimpleCardWidget):
    """工具栏  初始空 layout形式"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)

        self.__init_layout()

    def __init_layout(self):
        self._main_layout = QHBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._left_layout = QHBoxLayout()
        self._left_layout.setContentsMargins(0, 0, 0, 0)
        self._right_layout = QHBoxLayout()
        self._right_layout.setContentsMargins(0, 0, 0, 0)
        self._right_layout.setSpacing(0)

        self._main_layout.addLayout(self._left_layout)
        self._main_layout.addStretch()
        self._main_layout.addLayout(self._right_layout)

    @property
    def left_layout(self) -> QHBoxLayout:
        return self._left_layout

    @property
    def right_layout(self) -> QHBoxLayout:
        return self._right_layout


class KVTableWidget(TableWidget):
    """键值对表格"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_table()

    def _init_table(self):
        self.setColumnCount(3)
        self.setWordWrap(False)
        self.horizontalHeader().setVisible(False)
        self.verticalHeader().setVisible(False)
        self.setColumnHidden(0, True)

        self.setAlternatingRowColors(False)

        # 设置列宽策略
        header = self.horizontalHeader()

        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        # 让第三列(操作)固定宽度
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

    def set_items(self, items: dict):
        """设置键值对数据 - 优化大数据量处理"""
        data_size = len(items)
        self.setRowCount(data_size)
        # 批量设置，减少界面更新次数
        self.setUpdatesEnabled(False)

        try:
            for i, (k, v) in enumerate(items.items()):
                # --- 新增：第 0 列存原始索引 ---
                idx_item = QTableWidgetItem()
                idx_item.setData(Qt.ItemDataRole.DisplayRole, i)  # 存入数字 i
                self.setItem(i, 0, idx_item)

                # --- 修改：Key 放在第 1 列，Value 放在第 2 列 ---
                key_item = QTableWidgetItem(str(k))
                value_item = QTableWidgetItem(str(v))

                key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                if len(str(k)) > 30:
                    key_item.setToolTip(str(k))
                if len(str(v)) > 30:
                    value_item.setToolTip(str(v))

                self.setItem(i, 1, key_item)  # Key 在 1
                self.setItem(i, 2, value_item)  # Value 在 2
        finally:
            self.setUpdatesEnabled(True)


class SortState(Enum):
    ORIGINAL = auto()  # 原始顺序
    ASCENDING = auto()  # 升序
    DESCENDING = auto()  # 降序


SORT_TRANSITION = {
    SortState.ORIGINAL: (SortState.ASCENDING, BaseIcon.CHEVRON_UP, "升序"),
    SortState.ASCENDING: (SortState.DESCENDING, BaseIcon.CHEVRON_DOWN, "降序"),
    SortState.DESCENDING: (SortState.ORIGINAL, FluentIcon.SCROLL, "原始顺序"),
}


class KVTableToolWidget(SimpleCardWidget):
    """键值对表格工具条"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._sort_state = SortState.ORIGINAL
        self._items: dict[str, str] = {}
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self._tool_widget = ToolWidget(self)
        self._table_widget = KVTableWidget(self)

        self.copy_plain_button = TransparentTooltipButton(
            FluentIcon.COPY, self._tool_widget
        )
        self.copy_plain_button.setToolTip(self.tr("复制"))
        self.sort_order_button = TransparentTooltipButton(
            FluentIcon.SCROLL, self._tool_widget
        )
        self.sort_order_button.setToolTip(self.tr("排序"))
        self.copy_json_button = TransparentTooltipButton(
            FluentIcon.CODE, self._tool_widget
        )
        self.copy_json_button.setToolTip(self.tr("复制JSON"))

    def __init_layout(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._tool_widget)
        main_layout.addWidget(self._table_widget, stretch=1)

        tool_right_layout = self._tool_widget.right_layout
        tool_right_layout.addWidget(self.copy_plain_button)
        tool_right_layout.addWidget(self.sort_order_button)
        tool_right_layout.addWidget(self.copy_json_button)

    def __connect_signal_to_slot(self):
        self.copy_plain_button.clicked.connect(self.handle_copy_plain_button_clicked)
        self.sort_order_button.clicked.connect(self.handle_sort_order_button_clicked)
        self.copy_json_button.clicked.connect(self.handle_copy_json_button_clicked)

    def set_items(self, items: dict):
        self._items = items or {}
        self._table_widget.set_items(self._items)

    # —— 数据获取 ——

    def _get_items(self) -> dict[str, str]:
        """获取当前键值对：优先用缓存，降级从表格读取"""
        if self._items:
            return self._items
        return self._read_items_from_table()

    def _read_items_from_table(self) -> dict[str, str]:
        """从表格单元格读取键值对（应对排序等动态变化）"""
        items: dict[str, str] = {}
        for row in range(self._table_widget.rowCount()):
            key_item = self._table_widget.item(row, 0)
            value_item = self._table_widget.item(row, 1)
            if key_item is None:
                continue
            key = key_item.text()
            value = value_item.text() if value_item is not None else ""
            items[key] = value
        return items

    # —— 复制 ——

    def _copy_to_clipboard(self, text: str, success_message: str):
        """通用剪贴板复制方法"""
        if not text:
            show_warning(self.tr("提示"), self.tr("没有可复制的内容"), self.window())
            return
        QApplication.clipboard().setText(text)
        show_success(self.tr("成功"), success_message, self.window())

    @Slot()
    def handle_copy_plain_button_clicked(self):
        """复制为 Key: Value 格式（每行一个）"""
        items = self._get_items()
        text = "\n".join(f"{k}: {v}" for k, v in items.items())
        self._copy_to_clipboard(text, self.tr("已复制到剪贴板"))

    @Slot()
    def handle_copy_json_button_clicked(self):
        """复制为 JSON 格式（缩进 4 空格）"""
        items = self._get_items()
        text = json.dumps(items, indent=4, ensure_ascii=False)
        self._copy_to_clipboard(text, self.tr("JSON 已复制到剪贴板"))

    @Slot()
    def handle_sort_order_button_clicked(self):
        # 1. 获取下一阶段的信息
        next_state, icon, tip = SORT_TRANSITION[self._sort_state]

        # 2. 执行表格排序动作
        if next_state == SortState.ASCENDING:
            self._table_widget.sortItems(1, Qt.SortOrder.AscendingOrder)
        elif next_state == SortState.DESCENDING:
            self._table_widget.sortItems(1, Qt.SortOrder.DescendingOrder)
        else:
            self._table_widget.sortItems(
                0, Qt.SortOrder.AscendingOrder
            )  # 原始/默认排第一列

        # 3. 更新状态和 UI
        self._sort_state = next_state
        self.sort_order_button.setIcon(icon)
        self.sort_order_button.setToolTip(self.tr(tip))

    @property
    def tool_layout(self) -> QHBoxLayout:
        return self._tool_widget.left_layout


class ToolPlainTextEdit(SimpleCardWidget):
    """键值对编辑工具条"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._sort_state = SortState.ORIGINAL
        self._original_text = ""

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.tool_widget = ToolWidget(self)
        self.code_widget = LineNumberTextEdit(self)

        self._btn_search = TransparentTooltipButton(BaseIcon.DOCUMENT_SEARCH, self)
        self._btn_search.setToolTip("查找")
        self._btn_sort = TransparentTooltipButton(FluentIcon.SCROLL, self)
        self._btn_sort.setToolTip("排序")
        self._btn_wrap = TransparentTooltipButton(BaseIcon.LINE_BREAK, self)
        self._btn_wrap.setToolTip("换行")

    def __init_layout(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.tool_widget)
        main_layout.addWidget(self.code_widget, stretch=1)

        tool_right_layout = self.tool_widget.right_layout
        tool_right_layout.addWidget(self._btn_search)
        tool_right_layout.addWidget(self._btn_sort)
        tool_right_layout.addWidget(self._btn_wrap)

    def __connect_signal_to_slot(self):
        self._btn_search.clicked.connect(self.handle_btn_search_clicked)
        self._btn_sort.clicked.connect(self.handle_btn_sort_clicked)
        self._btn_wrap.clicked.connect(self.handle_btn_wrap_clicked)

    @Slot()
    def handle_btn_search_clicked(self):
        self.code_widget.toggle_search()

    @Slot()
    def handle_btn_sort_clicked(self):
        # 1. 获取下一阶段信息
        next_state, icon, tip = SORT_TRANSITION[self._sort_state]

        # 2. 执行文本排序动作
        if self._sort_state == SortState.ORIGINAL:  # 只有进入排序前才备份
            self._original_text = self.code_widget.to_text()

        lines = self.code_widget.to_text().splitlines()
        if next_state == SortState.ASCENDING:
            new_text = "\n".join(sorted(lines, key=str.lower))
        elif next_state == SortState.DESCENDING:
            new_text = "\n".join(sorted(lines, key=str.lower, reverse=True))
        else:
            new_text = self._original_text

        # 3. 更新状态、UI 和编辑器
        self._sort_state = next_state
        self._btn_sort.setIcon(icon)
        self._btn_sort.setToolTip(self.tr(tip))
        cursor = self.code_widget.text_edit.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(new_text)
        cursor.endEditBlock()

    @Slot()
    def handle_btn_wrap_clicked(self):
        self.code_widget.text_edit.toggle_wrap()

    @property
    def tool_layout(self) -> QHBoxLayout:
        return self.tool_widget.left_layout

    def set_text(self, text: str):
        self.code_widget.set_text(text)

    def set_read_only(self, read_only: bool):
        self.code_widget.set_read_only(read_only)


class KVDualPanel(QWidget):
    """文本、表格双重面板 可切换"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.text = ToolPlainTextEdit(self)
        self.table = KVTableToolWidget(self)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.table)

        self._btn_text = TransparentTooltipButton(BaseIcon.CONVERT_TO_TEXT, self)
        self._btn_text.setToolTip(self.tr("文本模式"))
        self._btn_table = TransparentTooltipButton(BaseIcon.CONVERT_TO_TABLE, self)
        self._btn_table.setToolTip(self.tr("表格模式"))

    def __init_layout(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.stack)

        self.text.tool_layout.addWidget(self._btn_table)
        self.table.tool_layout.addWidget(self._btn_text)

    def __connect_signal_to_slot(self):
        self._btn_text.clicked.connect(lambda: self.stack.setCurrentWidget(self.text))
        self._btn_table.clicked.connect(lambda: self.stack.setCurrentWidget(self.table))

    def set_items(self, items: dict):
        self.table.set_items(items)
        text_content = "\n".join(f"{k}: {v}" for k, v in items.items())
        self.text.set_text(text_content)

    def set_read_only(self, read_only: bool):
        self.text.set_read_only(read_only)
        # self.table.set_read_only(read_only)


if __name__ == "__main__":
    from qfluentwidgets import Theme, setTheme
    from qfluentwidgets.window.fluent_window import FluentWidget

    from ferret.config import resources_rc  # noqa: F401

    app = QApplication(sys.argv)
    setTheme(Theme.DARK)

    window = FluentWidget()
    window.setWindowTitle("KeyValueViewPanel Demo")
    # window.resize(700, 500)

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

    editor = KVDualPanel()
    editor.set_items(
        # "dsadadad:sdadasda\ndsaaaaaaaaaa"
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ91111111111111111111111111111111111111111111111111111111111111111111111111111111",
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Host": "api.example.com",
            "Connection": "keep-alive",
        }
    )
    layout.addWidget(editor, stretch=1)

    window.show()
    sys.exit(app.exec())
