import json
from enum import Enum, auto

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    SearchLineEdit,
    SimpleCardWidget,
    TableWidget,
    TreeWidget,
    isDarkTheme,
)

from ferret.apps.common.button import TransparentTooltipButton
from ferret.apps.common.icon import BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning

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


class ItemTableWidget(TableWidget):
    """键值对表格

    :param bool editable: 是否可编辑，默认为 False
    :param parent: 父控件
    """

    def __init__(
        self,
        editable: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._editable = editable
        self._init_table()

    def _init_table(self):
        self.verticalHeader().setDefaultSectionSize(28)
        self.setColumnCount(2)
        self.setWordWrap(False)
        self.horizontalHeader().setVisible(False)
        self.verticalHeader().setVisible(False)

        # 设置列宽策略：第0列=key(自适应)，第1列=value(拉伸)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        # 全局固化编辑触发方式：可编辑则双击/选中进入编辑，否则完全禁止。
        # 仅用 setEditTriggers 控制，无需逐格设置 ItemIsEditable 标志。
        if self._editable:
            self.setEditTriggers(
                QTableWidget.EditTrigger.DoubleClicked
                | QTableWidget.EditTrigger.SelectedClicked
            )
        else:
            self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

    def set_items(self, items: dict):
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


class ItemTableToolWidget(SimpleCardWidget):
    """带工具栏的键值对表格

    :param bool editable: 是否可编辑
    :param parent: 父控件
    """

    def __init__(self, editable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._editable = editable
        self._sort_state = SortState.ORIGINAL
        self._items: dict[str, str] = {}
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self._tool_widget = ToolWidget(self)
        self._table_widget = ItemTableWidget(self._editable, self)

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
        self._wrap_on = False  # 默认不换行
        self._search_visible = False
        self._search_results: list = []  # 匹配的 QTextCursor 列表
        self._search_index = -1  # 当前命中项索引

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.tool_widget = ToolWidget(self)
        self.code_widget = CodeEditor(self)

        self._btn_copy = TransparentTooltipButton(FluentIcon.COPY, self)
        self._btn_copy.setToolTip(self.tr("复制"))
        self._btn_wrap = TransparentTooltipButton(BaseIcon.LINE_BREAK, self)
        self._btn_wrap.setToolTip(self.tr("换行"))
        self._btn_search = TransparentTooltipButton(BaseIcon.DOCUMENT_SEARCH, self)
        self._btn_search.setToolTip(self.tr("查找"))

        # 查找栏：默认隐藏，点击"查找"按钮时展开
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText(self.tr("查找..."))
        self._search_bar.setFixedHeight(30)  # 与工具栏按钮同高
        self._search_bar.setVisible(False)
        self._search_prev = TransparentTooltipButton(FluentIcon.UP, self)
        self._search_prev.setToolTip(self.tr("上一个"))
        self._search_prev.setFixedSize(22, 22)
        self._search_prev.setIconSize(QSize(12, 12))
        self._search_prev.setVisible(False)
        self._search_next = TransparentTooltipButton(FluentIcon.DOWN, self)
        self._search_next.setToolTip(self.tr("下一个"))
        self._search_next.setFixedSize(22, 22)
        self._search_next.setIconSize(QSize(12, 12))
        self._search_next.setVisible(False)
        self._search_close = TransparentTooltipButton(FluentIcon.CLOSE, self)
        self._search_close.setToolTip(self.tr("关闭"))
        self._search_close.setFixedSize(22, 22)
        self._search_close.setIconSize(QSize(12, 12))
        self._search_close.setVisible(False)
        self._search_status = CaptionLabel(self)
        self._search_status.setMinimumWidth(
            36
        )  # 至少留出 3 个字符宽度，避免数字变化时抖动
        self._search_status.setVisible(False)

    def __init_layout(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.tool_widget)

        # 查找栏：独占一行，输入框+状态文字挨着，按钮靠右
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(0)
        self._search_bar.setFixedWidth(160)
        search_layout.addStretch(1)  # 左侧弹簧 → 整组靠右
        search_layout.addWidget(self._search_bar)
        search_layout.addWidget(self._search_status)  # 状态文字紧贴输入框右侧
        search_layout.addWidget(self._search_prev)
        search_layout.addWidget(self._search_next)
        search_layout.addWidget(self._search_close)
        self._search_container = QWidget(self)
        self._search_container.setLayout(search_layout)
        self._search_container.setVisible(False)
        main_layout.addWidget(self._search_container)

        main_layout.addWidget(self.code_widget, stretch=1)

        tool_right_layout = self.tool_widget.right_layout
        tool_right_layout.addWidget(self._btn_copy)
        tool_right_layout.addWidget(self._btn_wrap)
        tool_right_layout.addWidget(self._btn_search)

    def __connect_signal_to_slot(self):
        self._btn_copy.clicked.connect(self.handle_btn_copy_clicked)
        self._btn_wrap.clicked.connect(self.handle_btn_wrap_clicked)
        self._btn_search.clicked.connect(self.handle_btn_search_clicked)
        self._search_close.clicked.connect(self.close_search)
        self._search_next.clicked.connect(self.search_next)
        self._search_prev.clicked.connect(self.search_prev)
        # 输入即触发实时搜索（去掉回车/搜索按钮触发）
        self._search_bar.textChanged.connect(self.__on_search_text_changed)

    @Slot()
    def handle_btn_copy_clicked(self):
        """复制编辑器中的原始文本"""
        text = self.code_widget.toPlainText()
        if not text:
            show_warning(self.tr("提示"), self.tr("没有可复制的内容"), self.window())
            return
        QApplication.clipboard().setText(text)
        show_success(self.tr("成功"), self.tr("已复制到剪贴板"), self.window())

    @Slot()
    def handle_btn_wrap_clicked(self):
        """切换换行：默认不换行，点击换行，再次点击取消换行"""
        self._wrap_on = not self._wrap_on
        self.code_widget.set_word_wrap(self._wrap_on)

    @Slot()
    def handle_btn_search_clicked(self):
        self.toggle_search()

    def toggle_search(self):
        """切换查找栏显隐"""
        if self._search_visible:
            self.close_search()
        else:
            self.open_search()

    def open_search(self):
        self._search_visible = True
        self._search_container.setVisible(True)
        self._search_bar.setVisible(True)
        self._search_prev.setVisible(True)
        self._search_next.setVisible(True)
        self._search_close.setVisible(True)
        self._search_status.setVisible(True)
        self.code_widget.set_search_active(True)
        # 预填当前选中文本，方便继续查找
        cursor = self.code_widget.textCursor()
        if cursor.hasSelection():
            self._search_bar.setText(cursor.selectedText())
        self._search_bar.setFocus()
        self._search_bar.selectAll()
        text = self._search_bar.text().strip()
        if text:
            self.do_search(text)

    def close_search(self):
        self._search_visible = False
        self._search_container.setVisible(False)
        self._search_bar.setText("")
        self.clear_search()
        self.code_widget.set_search_active(False)
        self.code_widget.setFocus()

    def clear_search(self):
        """清除高亮与命中记录"""
        self._search_results = []
        self._search_index = -1
        self._search_status.setText("")
        self.code_widget.setExtraSelections([])
        self.code_widget.set_highlight_current_line()  # 恢复当前行高亮

    def __on_search_text_changed(self, text: str):
        """输入即实时搜索"""
        self.do_search(text.strip())

    def do_search(self, text: str):
        """从文档中查找所有命中项并高亮"""
        self._search_results = []
        self._search_index = -1
        if not text:
            self.clear_search()
            return

        doc = self.code_widget.document()
        cursor = doc.find(text)
        while not cursor.isNull():
            self._search_results.append(QTextCursor(cursor))
            cursor = doc.find(text, cursor)

        if self._search_results:
            self._search_index = 0
            self._apply_search_highlight()
            self._goto_current()
        else:
            self._search_status.setText(self.tr("无匹配"))
            self.code_widget.setExtraSelections([])

    def search_next(self):
        if not self._search_results:
            text = self._search_bar.text().strip()
            if text:
                self.do_search(text)
            return
        self._search_index = (self._search_index + 1) % len(self._search_results)
        self._apply_search_highlight()
        self._goto_current()

    def search_prev(self):
        if not self._search_results:
            return
        self._search_index = (self._search_index - 1) % len(self._search_results)
        self._apply_search_highlight()
        self._goto_current()

    def _apply_search_highlight(self):
        """用 ExtraSelection 高亮所有命中：当前项橙色，其余黄色"""
        selections = []
        is_dark = isDarkTheme()
        base_bg = QColor(255, 235, 120, 120) if is_dark else QColor(255, 235, 0, 120)
        cur_bg = QColor(255, 160, 40, 180) if is_dark else QColor(255, 150, 0, 180)
        for i, c in enumerate(self._search_results):
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(base_bg)
            if i == self._search_index:
                sel.format.setBackground(cur_bg)
            sel.cursor = QTextCursor(c)
            selections.append(sel)
        self.code_widget.setExtraSelections(selections)
        total = len(self._search_results)
        self._search_status.setText(f"{self._search_index + 1}/{total}")

    def _goto_current(self):
        if 0 <= self._search_index < len(self._search_results):
            cursor = QTextCursor(self._search_results[self._search_index])
            self.code_widget.setTextCursor(cursor)
            self.code_widget.centerCursor()

    @property
    def tool_layout(self) -> QHBoxLayout:
        return self.tool_widget.left_layout

    def set_text(self, text: str, lang: str = "http"):
        """设置文本并指定高亮语言。lang: http/headers/json/xml"""
        self.code_widget.set_language(lang)
        self.code_widget.setPlainText(text)
        # 文本变更时清空旧的查找结果
        self._search_results = []
        self._search_index = -1
        self._search_status.setText("")

    def set_read_only(self, read_only: bool):
        self.code_widget.setReadOnly(read_only)


class ItemDualPanel(QWidget):
    """带工具栏的文本、表格双重面板 可切换

    :param bool editable: 是否可编辑
    :param parent: 父控件
    """

    def __init__(self, editable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._editable = editable
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.text = ToolPlainTextEdit(self)
        self.table = ItemTableToolWidget(self._editable, self)

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

    def sizeHint(self) -> QSize:
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


class JsonTreeWidget(TreeWidget):
    """JSON 体树形视图 — 递归展示 dict / list / 标量，两列：键/值"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHeaderLabels([self.tr("键"), self.tr("值")])
        self.setColumnWidth(0, 120)  # 键列初始宽度
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header().setFixedHeight(22)  # 横向表头行高（紧凑）
        self.setAlternatingRowColors(False)
        self.setIndentation(14)

    def set_data(self, data):
        """用解析后的 JSON 对象（dict/list/标量）重建树（无根节点）"""
        self.clear()
        if data is None:
            return
        self._fill(self, data)
        self._apply_count_color()
        # 默认全部折叠，不展开

    def _fill(self, parent, value):
        if isinstance(value, dict):
            for k, v in value.items():
                item = QTreeWidgetItem(parent)
                item.setText(0, str(k))
                if isinstance(v, (dict, list)):
                    item.setText(1, self._count_label(v))
                    item.setFont(1, self._count_font())
                    self._fill(item, v)
                else:
                    item.setText(1, self._scalar_text(v))
        elif isinstance(value, list):
            for i, v in enumerate(value):
                item = QTreeWidgetItem(parent)
                item.setText(0, f"[{i}]")
                if isinstance(v, (dict, list)):
                    item.setText(1, self._count_label(v))
                    item.setFont(1, self._count_font())
                    self._fill(item, v)
                else:
                    item.setText(1, self._scalar_text(v))
        else:
            item = QTreeWidgetItem(parent)
            item.setText(0, self._scalar_text(value))

    @staticmethod
    def _count_label(v) -> str:
        """折叠节点显示子项数量：Object(x) / Array(x)"""
        if isinstance(v, dict):
            return f"Object({len(v)})"
        if isinstance(v, list):
            return f"Array({len(v)})"
        return ""

    @staticmethod
    def _count_font():
        """计数标签字体：斜体灰色，与基础字体区分"""
        from PySide6.QtGui import QFont

        f = QFont()
        f.setItalic(True)
        return f

    def _apply_count_color(self):
        """给带计数的单元格上灰色（遍历已建好的项）"""
        from PySide6.QtGui import QColor

        gray = QColor(128, 128, 128)
        stack: list[QTreeWidgetItem] = [
            item
            for i in range(self.topLevelItemCount())
            if (item := self.topLevelItem(i)) is not None
        ]
        while stack:
            it = stack.pop()
            if it.font(1).italic():
                it.setForeground(1, gray)
            for j in range(it.childCount()):
                child = it.child(j)
                if child is not None:
                    stack.append(child)

    @staticmethod
    def _scalar_text(v) -> str:
        if v is None:
            return "null"
        if isinstance(v, str):
            return v
        return str(v)


class JsonTreePanel(SimpleCardWidget):
    """树模式页面 — 自带工具栏插槽（tool_layout），与 KVDualPanel 的各页面一致"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._tool_widget = ToolWidget(self)
        self.tree = JsonTreeWidget(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._tool_widget)
        layout.addWidget(self.tree, stretch=1)

    @property
    def tool_layout(self) -> QHBoxLayout:
        return self._tool_widget.left_layout


class JsonDualPanel(QWidget):
    """JSON 体双重面板：文本模式 / 树模式 可切换（参考 KVDualPanel）"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.text = ToolPlainTextEdit(self)
        self.tree = JsonTreePanel(self)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.tree)

        self._btn_text = TransparentTooltipButton(BaseIcon.CONVERT_TO_TEXT, self)
        self._btn_text.setToolTip(self.tr("文本模式"))
        self._btn_tree = TransparentTooltipButton(BaseIcon.CONVERT_TO_TABLE, self)
        self._btn_tree.setToolTip(self.tr("树模式"))

    def __init_layout(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.stack)

        # 与 KVDualPanel 一致：每个页面只显示自己的切换按钮，不额外占一行
        self.text.tool_layout.addWidget(self._btn_tree)
        self.tree.tool_layout.addWidget(self._btn_text)

    def __connect_signal_to_slot(self):
        self._btn_text.clicked.connect(lambda: self.stack.setCurrentWidget(self.text))
        self._btn_tree.clicked.connect(lambda: self.stack.setCurrentWidget(self.tree))
        self.stack.currentChanged.connect(self.updateGeometry)

    def sizeHint(self) -> QSize:
        """把内部 QStackedWidget 当前页面的正确尺寸向上传递，解决嵌套错位。"""
        current = self.stack.currentWidget()
        if current:
            return current.sizeHint()
        return super().sizeHint()

    def set_text(self, text: str, lang: str = "json"):
        """设置文本并指定语言；lang=json 时自动建树。"""
        self.text.set_text(text, lang=lang)
        if lang == "json":
            try:
                import json

                parsed = json.loads(text)
                self.tree.tree.set_data(parsed)
            except (ImportError, ValueError, RuntimeError):
                self.tree.tree.clear()
        else:
            self.tree.tree.clear()

    def set_read_only(self, read_only: bool):
        self.text.set_read_only(read_only)


__all__ = [
    "SORT_TRANSITION",
    "ItemDualPanel",
    "ItemTableToolWidget",
    "ItemTableWidget",
    "JsonDualPanel",
    "JsonTreeWidget",
    "SortState",
    "ToolPlainTextEdit",
    "ToolWidget",
]
