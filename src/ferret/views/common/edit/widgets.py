import json
from enum import Enum, auto

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    SimpleCardWidget,
    TableItemDelegate,
    TableWidget,
)

from ferret.views.common.button import TransparentTooltipButton
from ferret.views.common.icon import BaseIcon
from ferret.views.common.info_bar import show_success, show_warning

from .editor import CodeEditor


class ToolWidget(QWidget):
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
        self.setColumnCount(2)
        self.setWordWrap(False)
        self.horizontalHeader().setVisible(False)
        self.verticalHeader().setVisible(False)

        self.setItemDelegate(TableItemDelegate(self))

        # 设置列宽策略：第0列=key(自适应)，第1列=value(拉伸)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

    def set_items(self, items: dict):
        """设置键值对数据 - 优化大数据量处理"""
        data_size = len(items)
        self.setRowCount(data_size)
        # 批量设置，减少界面更新次数
        self.setUpdatesEnabled(False)

        try:
            for i, (k, v) in enumerate(items.items()):
                # Key 放在第 0 列，Value 放在第 1 列
                # （不保留隐藏占位列，否则 TableItemDelegate 的选中指示条
                #  会画到隐藏列上导致不可见）
                key_item = QTableWidgetItem(str(k))
                value_item = QTableWidgetItem(str(v))

                key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                value_item.setFlags(value_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

                if len(str(k)) > 30:
                    key_item.setToolTip(str(k))
                if len(str(v)) > 30:
                    value_item.setToolTip(str(v))

                self.setItem(i, 0, key_item)  # Key 在 0
                self.setItem(i, 1, value_item)  # Value 在 1
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
        text = json.dumps(items, indent=2, ensure_ascii=False)
        self._copy_to_clipboard(text, self.tr("JSON 已复制到剪贴板"))

    @Slot()
    def handle_sort_order_button_clicked(self):
        # 1. 获取下一阶段的信息
        next_state, icon, tip = SORT_TRANSITION[self._sort_state]

        # 2. 执行表格排序动作（统一按第 0 列 = key 排序，保证语义一致）
        if next_state == SortState.ASCENDING:
            self._table_widget.sortItems(0, Qt.SortOrder.AscendingOrder)
        elif next_state == SortState.DESCENDING:
            self._table_widget.sortItems(0, Qt.SortOrder.DescendingOrder)
        else:
            # 原始：恢复插入顺序（用 set_items 重新填充，避免按列重排的歧义）
            self.set_items(self._items)

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
        self.code_widget = CodeEditor(self)

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

    def set_text(self, text: str, lang: str = "http"):
        """设置文本并指定高亮语言。lang: http/headers/json/xml"""
        self.code_widget.set_language(lang)
        self.code_widget.setPlainText(text)

    def set_read_only(self, read_only: bool):
        self.code_widget.setReadOnly(read_only)


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
        # 内层 stack 切换页面时通知外层布局重新计算尺寸
        self.stack.currentChanged.connect(self.updateGeometry)

    def sizeHint(self) -> QSize:  # noqa: N802 — Qt 命名约定
        """把内部 QStackedWidget 当前页面的正确尺寸向上传递，
        解决嵌套 QStackedWidget 时外层拿不到正确 sizeHint 导致内容错位的问题。"""
        current = self.stack.currentWidget()
        if current:
            return current.sizeHint()
        return super().sizeHint()

    def set_items(self, items: dict):
        self.table.set_items(items)
        text_content = "\n".join(f"{k}: {v}" for k, v in items.items())
        # 请求头/响应头/参数为 Key: Value 结构，用 headers 高亮避免全红
        self.text.set_text(text_content, lang="headers")

    def set_read_only(self, read_only: bool):
        self.text.set_read_only(read_only)

    # self.table.set_read_only(read_only)


__all__ = [
    "ToolWidget",
    "KVTableWidget",
    "SortState",
    "SORT_TRANSITION",
    "KVTableToolWidget",
    "ToolPlainTextEdit",
    "KVDualPanel",
]
