import json
from collections.abc import Mapping, Sequence
from enum import Enum, auto
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QFont, QTextCursor
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
    RoundMenu,
    SearchLineEdit,
    SimpleCardWidget,
    TableWidget,
    TreeWidget,
)

from ferret.apps.common.button import TransparentTooltipButton
from ferret.apps.common.icon import BaseAction, BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.utils.i18n import QT_TRANSLATE_NOOP

from .editor import CodeEditor
from .syntax import Language
from .theme import EditorPalette

# 键值对的对外类型：既收 dict（只读页面的既有调用点），也收有序键值对序列。
# 键和值都不限类型 —— 两边都过 `_cell_text`，bytes 会被解码、其余一律 str()。
ItemSource = Mapping[Any, Any] | Sequence[tuple[Any, Any]] | None


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def normalize_items(items: ItemSource) -> list[tuple[str, str]]:
    """把 dict / 键值对序列统一成**有序键值对列表**。

    HTTP 头是 multidict —— 重复的 ``Set-Cookie`` / ``Cookie`` 完全合法，用 dict
    存会把重名的挤掉。所以内部一律用列表；仍然接受 dict 只是为了兼容只读页面
    既有的调用点（`apps/common/flow` 传的就是 dict）。
    """
    if not items:
        return []
    if isinstance(items, Mapping):
        return [(_cell_text(k), _cell_text(v)) for k, v in items.items()]
    return [(_cell_text(k), _cell_text(v)) for k, v in items]


def items_to_text(items: ItemSource) -> str:
    """渲染成 ``Key: Value`` 文本（文本页与"复制"用的就是这一份）。"""
    return "\n".join(f"{k}: {v}" for k, v in normalize_items(items))


def items_from_text(text: str) -> list[tuple[str, str]]:
    """把 ``Key: Value`` 文本解析回键值对；空行丢弃，无冒号的行整行当键。

    只切第一个冒号：``Date: Mon, 01 Jan 2024 00:00:00 GMT`` 的值里还有冒号。
    """
    items: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if separator:
            items.append((key.strip(), value.strip()))
        else:
            items.append((line.strip(), ""))
    return items


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
    """键值对表格。

    **表格自己就是唯一真相**：不缓存任何数据副本，`items` 逐格读回。排序、编辑、
    增删行之后读到的都是当前值 —— 之前那份 `_items` 缓存会让用户改过的单元格
    对外完全不可见（连"复制"都复制旧值）。

    :param bool editable: 是否可编辑，默认为 False
    :param parent: 父控件
    """

    items_changed = Signal()

    def __init__(
        self,
        editable: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self._editable = editable
        # 程序化填表期间要屏蔽 itemChanged，否则每建一个单元格都当成"用户编辑"。
        self._loading = False
        self._init_table()
        self.itemChanged.connect(self._on_item_changed)

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

        self._apply_edit_triggers()

    def _apply_edit_triggers(self):
        # 全局固化编辑触发方式：可编辑则双击/选中进入编辑，否则完全禁止。
        # 仅用 setEditTriggers 控制，无需逐格设置 ItemIsEditable 标志。
        if self._editable:
            self.setEditTriggers(
                QTableWidget.EditTrigger.DoubleClicked
                | QTableWidget.EditTrigger.SelectedClicked
            )
        else:
            self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

    @property
    def editable(self) -> bool:
        return self._editable

    def set_read_only(self, read_only: bool):
        self._editable = not read_only
        self._apply_edit_triggers()

    @staticmethod
    def _make_item(text: str) -> QTableWidgetItem:
        # Key 放在第 0 列，Value 放在第 1 列
        # （不保留隐藏占位列，否则 TableItemDelegate 的选中指示条
        #  会画到隐藏列上导致不可见）
        item = QTableWidgetItem(text)
        if len(text) > 30:
            item.setToolTip(text)
        return item

    # 表格自身这一对读写器刻意**不**叫 items / set_items：`QTableWidget` 已经有一个
    # 受保护虚函数 `items(QMimeData*)`（拖放时 Qt 内部会调），重名会把它顶掉，签名还
    # 对不上。对外的 items() 由上面两层容器提供，那两层不继承 QTableWidget，没这问题。
    def set_rows(self, items: ItemSource):
        pairs = normalize_items(items)
        self._loading = True
        # 批量设置，减少界面更新次数
        self.setUpdatesEnabled(False)
        try:
            self.setRowCount(len(pairs))
            for i, (k, v) in enumerate(pairs):
                self.setItem(i, 0, self._make_item(k))
                self.setItem(i, 1, self._make_item(v))
        finally:
            self.setUpdatesEnabled(True)
            self._loading = False

    def rows(self) -> list[tuple[str, str]]:
        """逐格读回当前键值对；键为空的行整行丢弃（就是用户还没填完的新行）。"""
        result: list[tuple[str, str]] = []
        for row in range(self.rowCount()):
            key_item = self.item(row, 0)
            if key_item is None:
                continue
            key = key_item.text().strip()
            if not key:
                continue
            value_item = self.item(row, 1)
            result.append((key, value_item.text() if value_item is not None else ""))
        return result

    def add_row(self, key: str = "", value: str = "") -> int:
        """追加一行并立刻进入编辑 —— 加一行空行本身没意义，用户要的是填内容。"""
        row = self.rowCount()
        self._loading = True
        try:
            self.insertRow(row)
            self.setItem(row, 0, self._make_item(key))
            self.setItem(row, 1, self._make_item(value))
        finally:
            self._loading = False
        self.items_changed.emit()
        self.setCurrentCell(row, 0)
        if self._editable:
            item = self.item(row, 0)
            if item is not None:
                self.editItem(item)
        return row

    def remove_selected_rows(self) -> int:
        """删除选中行，返回删除条数。倒序删，避免删前面的行把后面的行号挪掉。"""
        rows = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        if not rows:
            return 0
        self._loading = True
        try:
            for row in rows:
                self.removeRow(row)
        finally:
            self._loading = False
        self.items_changed.emit()
        return len(rows)

    @Slot(QTableWidgetItem)
    def _on_item_changed(self, _item: QTableWidgetItem):
        if self._loading:
            return
        self.items_changed.emit()


class SortState(Enum):
    ORIGINAL = auto()  # 原始顺序
    ASCENDING = auto()  # 升序
    DESCENDING = auto()  # 降序


# 文案只做标记、不在这里求值：模块级求值会赶在翻译器安装之前（`core/application.py`
# 在顶层就 import 了 MainWindow），译文会永久冻结成英文。求值在使用点
# `handle_sort_order_button_clicked` 里用 `QCoreApplication.translate` 做。
SORT_TRANSITION = {
    SortState.ORIGINAL: (
        SortState.ASCENDING,
        BaseIcon.CHEVRON_UP,
        QT_TRANSLATE_NOOP("ItemTableToolWidget", "Ascending"),
    ),
    SortState.ASCENDING: (
        SortState.DESCENDING,
        BaseIcon.CHEVRON_DOWN,
        QT_TRANSLATE_NOOP("ItemTableToolWidget", "Descending"),
    ),
    SortState.DESCENDING: (
        SortState.ORIGINAL,
        FluentIcon.SCROLL,
        QT_TRANSLATE_NOOP("ItemTableToolWidget", "Original order"),
    ),
}


class ItemTableToolWidget(SimpleCardWidget):
    """带工具栏的键值对表格

    :param bool editable: 是否可编辑
    :param parent: 父控件
    """

    items_changed = Signal()

    def __init__(self, editable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._editable = editable
        self._sort_state = SortState.ORIGINAL
        # 只为"原始顺序"那一档留的快照，**不是**读数据的来源（读数据一律走表格）。
        self._original: list[tuple[str, str]] = []
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self._tool_widget = ToolWidget(self)
        self._table_widget = ItemTableWidget(self._editable, self)
        self._table_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.add_row_button = TransparentTooltipButton(
            FluentIcon.ADD, self._tool_widget
        )
        self.add_row_button.setToolTip(self.tr("Add row"))
        self.remove_row_button = TransparentTooltipButton(
            FluentIcon.DELETE, self._tool_widget
        )
        self.remove_row_button.setToolTip(self.tr("Delete selected rows"))
        self.copy_plain_button = TransparentTooltipButton(
            FluentIcon.COPY, self._tool_widget
        )
        self.copy_plain_button.setToolTip(self.tr("Copy"))
        self.sort_order_button = TransparentTooltipButton(
            FluentIcon.SCROLL, self._tool_widget
        )
        self.sort_order_button.setToolTip(self.tr("Sort"))
        self.copy_json_button = TransparentTooltipButton(
            FluentIcon.CODE, self._tool_widget
        )
        self.copy_json_button.setToolTip(self.tr("Copy as JSON"))
        self._apply_editable()

    def __init_layout(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._tool_widget)
        main_layout.addWidget(self._table_widget, stretch=1)

        tool_right_layout = self._tool_widget.right_layout
        tool_right_layout.addWidget(self.add_row_button)
        tool_right_layout.addWidget(self.remove_row_button)
        tool_right_layout.addWidget(self.copy_plain_button)
        tool_right_layout.addWidget(self.sort_order_button)
        tool_right_layout.addWidget(self.copy_json_button)

    def __connect_signal_to_slot(self):
        self.add_row_button.clicked.connect(self.add_row)
        self.remove_row_button.clicked.connect(self.remove_selected_rows)
        self.copy_plain_button.clicked.connect(self.handle_copy_plain_button_clicked)
        self.sort_order_button.clicked.connect(self.handle_sort_order_button_clicked)
        self.copy_json_button.clicked.connect(self.handle_copy_json_button_clicked)
        self._table_widget.items_changed.connect(self.items_changed)
        self._table_widget.customContextMenuRequested.connect(self._show_context_menu)

    def _apply_editable(self):
        """增删按钮只在可编辑时露面；排序按钮反之。

        排序的"原始顺序"档要靠重填表格才能复位，而重填必然吃掉用户改了一半的
        内容 —— 与其做一个会丢编辑的按钮，不如在编辑态干脆不给。
        """
        self.add_row_button.setVisible(self._editable)
        self.remove_row_button.setVisible(self._editable)
        self.sort_order_button.setVisible(not self._editable)

    @property
    def editable(self) -> bool:
        return self._editable

    def set_read_only(self, read_only: bool):
        self._editable = not read_only
        self._table_widget.set_read_only(read_only)
        self._apply_editable()

    def set_items(self, items: ItemSource):
        self._original = normalize_items(items)
        self._table_widget.set_rows(self._original)
        self._sort_state = SortState.ORIGINAL
        self.sort_order_button.setIcon(FluentIcon.SCROLL)
        self.sort_order_button.setToolTip(self.tr("Sort"))

    # —— 数据获取 ——

    def items(self) -> list[tuple[str, str]]:
        """当前键值对（含用户编辑），顺序即行序。表格是唯一真相，逐格读回。"""
        return self._table_widget.rows()

    @Slot()
    def add_row(self):
        self._table_widget.add_row()

    @Slot()
    def remove_selected_rows(self):
        if not self._table_widget.remove_selected_rows():
            show_warning(
                self.tr("Notice"),
                self.tr("Select the rows you want to delete first"),
                self.window(),
            )

    @Slot(object)
    def _show_context_menu(self, pos):
        if not self._editable:
            return
        menu = RoundMenu(parent=self._table_widget)
        menu.addAction(BaseAction(FluentIcon.ADD, self.tr("Add row"), self.add_row))
        remove = BaseAction(
            FluentIcon.DELETE,
            self.tr("Delete selected rows"),
            self.remove_selected_rows,
        )
        remove.setEnabled(bool(self._table_widget.selectedIndexes()))
        menu.addAction(remove)
        viewport = self._table_widget.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    # —— 复制 ——

    def _copy_to_clipboard(self, text: str, success_message: str):
        """通用剪贴板复制方法"""
        if not text:
            show_warning(self.tr("Notice"), self.tr("Nothing to copy"), self.window())
            return
        QApplication.clipboard().setText(text)
        show_success(self.tr("Success"), success_message, self.window())

    @Slot()
    def handle_copy_plain_button_clicked(self):
        """复制为 Key: Value 格式（每行一个）"""
        self._copy_to_clipboard(
            items_to_text(self.items()), self.tr("Copied to clipboard")
        )

    @Slot()
    def handle_copy_json_button_clicked(self):
        """复制为 JSON 格式（缩进 2 空格）。

        JSON 对象不允许重名键，而 HTTP 头允许 —— 重名的只能收敛成数组。
        """
        payload: dict[str, Any] = {}
        for key, value in self.items():
            if key not in payload:
                payload[key] = value
                continue
            existing = payload[key]
            if isinstance(existing, list):
                existing.append(value)
            else:
                payload[key] = [existing, value]
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        self._copy_to_clipboard(text, self.tr("JSON copied to clipboard"))

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
            self.set_items(self._original)

        # 3. 更新状态和 UI
        self._sort_state = next_state
        self.sort_order_button.setIcon(icon)
        self.sort_order_button.setToolTip(
            QCoreApplication.translate("ItemTableToolWidget", tip)
        )

    @property
    def tool_layout(self) -> QHBoxLayout:
        return self._tool_widget.left_layout


class ToolPlainTextEdit(SimpleCardWidget):
    """带工具栏（复制 / 换行 / 查找）的代码编辑器面板。"""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._wrap_on = False  # 默认不换行
        self._search_visible = False
        self._search_results: list = []  # 匹配的 QTextCursor 列表
        self._search_index = -1  # 当前命中项索引
        # set_text 是程序化换文本，不该被当成"用户改了内容"往外发 changed。
        self._loading = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.code_widget.textChanged.connect(self._on_text_changed)

    def __init_widget(self):
        self.tool_widget = ToolWidget(self)
        self.code_widget = CodeEditor(self)

        self._btn_copy = TransparentTooltipButton(FluentIcon.COPY, self)
        self._btn_copy.setToolTip(self.tr("Copy"))
        self._btn_wrap = TransparentTooltipButton(BaseIcon.LINE_BREAK, self)
        self._btn_wrap.setToolTip(self.tr("Word wrap"))
        self._btn_search = TransparentTooltipButton(BaseIcon.DOCUMENT_SEARCH, self)
        self._btn_search.setToolTip(self.tr("Find"))

        # 查找栏：默认隐藏，点击"查找"按钮时展开
        self._search_bar = SearchLineEdit(self)
        self._search_bar.setPlaceholderText(self.tr("Find..."))
        self._search_bar.setFixedHeight(30)  # 与工具栏按钮同高
        self._search_bar.setVisible(False)
        self._search_prev = TransparentTooltipButton(FluentIcon.UP, self)
        self._search_prev.setToolTip(self.tr("Previous"))
        self._search_prev.setFixedSize(22, 22)
        self._search_prev.setIconSize(QSize(12, 12))
        self._search_prev.setVisible(False)
        self._search_next = TransparentTooltipButton(FluentIcon.DOWN, self)
        self._search_next.setToolTip(self.tr("Next"))
        self._search_next.setFixedSize(22, 22)
        self._search_next.setIconSize(QSize(12, 12))
        self._search_next.setVisible(False)
        self._search_close = TransparentTooltipButton(FluentIcon.CLOSE, self)
        self._search_close.setToolTip(self.tr("Close"))
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
            show_warning(self.tr("Notice"), self.tr("Nothing to copy"), self.window())
            return
        QApplication.clipboard().setText(text)
        show_success(self.tr("Success"), self.tr("Copied to clipboard"), self.window())

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
            self._search_status.setText(self.tr("No matches"))
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
        base_bg = EditorPalette.search_match()
        cur_bg = EditorPalette.search_current()
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

    def set_text(self, text: str, lang: Language | str = Language.HTTP):
        """设置文本并指定高亮语言（见 ``syntax.Language``）。"""
        self._loading = True
        try:
            self.code_widget.set_language(lang)
            self.code_widget.setPlainText(text)
            # 整体换文本是程序化路径，绕过 highlighter 的输入防抖：否则新报文会先
            # 以无色状态闪一帧（点一条 flow 就闪一次）。这一句必须留在 _loading 里：
            # `rehighlight()` 自己会再发一轮 textChanged，挪到外面就等于把程序化
            # 换文本当成用户编辑往外发 changed。
            self.code_widget.highlighter.relex_now()
        finally:
            self._loading = False
        # 文本变更时清空旧的查找结果
        self._search_results = []
        self._search_index = -1
        self._search_status.setText("")

    def text(self) -> str:
        """当前文本（含用户编辑）。"""
        return self.code_widget.toPlainText()

    def set_read_only(self, read_only: bool):
        self.code_widget.setReadOnly(read_only)

    def is_read_only(self) -> bool:
        return self.code_widget.isReadOnly()

    @Slot()
    def _on_text_changed(self):
        if self._loading:
            return
        self.changed.emit()


class ItemDualPanel(QWidget):
    """带工具栏的文本、表格双重面板 可切换

    :param bool editable: 是否可编辑
    :param parent: 父控件
    """

    changed = Signal()

    def __init__(self, editable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._editable = editable
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.set_read_only(not editable)

    def __init_widget(self):
        self.text = ToolPlainTextEdit(self)
        self.table = ItemTableToolWidget(self._editable, self)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.text)
        self.stack.addWidget(self.table)

        self._btn_text = TransparentTooltipButton(BaseIcon.CONVERT_TO_TEXT, self)
        self._btn_text.setToolTip(self.tr("Text view"))
        self._btn_table = TransparentTooltipButton(BaseIcon.CONVERT_TO_TABLE, self)
        self._btn_table.setToolTip(self.tr("Table view"))

    def __init_layout(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.stack)

        self.text.tool_layout.addWidget(self._btn_table)
        self.table.tool_layout.addWidget(self._btn_text)

    def __connect_signal_to_slot(self):
        # 切页之前先把当前页的内容搬到目标页，否则两页各自记着一份、互相看不见。
        self._btn_text.clicked.connect(self._show_text_page)
        self._btn_table.clicked.connect(self._show_table_page)
        # 内层 stack 切换页面时通知外层布局重新计算尺寸
        self.stack.currentChanged.connect(self.updateGeometry)
        self.text.changed.connect(self.changed)
        self.table.items_changed.connect(self.changed)

    @Slot()
    def _show_text_page(self):
        if self._editable and self.stack.currentWidget() is self.table:
            self.text.set_text(items_to_text(self.table.items()), lang=Language.HEADERS)
        self.stack.setCurrentWidget(self.text)

    @Slot()
    def _show_table_page(self):
        if self._editable and self.stack.currentWidget() is self.text:
            self.table.set_items(items_from_text(self.text.text()))
        self.stack.setCurrentWidget(self.table)

    def sizeHint(self) -> QSize:
        """把内部 QStackedWidget 当前页面的正确尺寸向上传递，
        解决嵌套 QStackedWidget 时外层拿不到正确 sizeHint 导致内容错位的问题。"""
        current = self.stack.currentWidget()
        if current:
            return current.sizeHint()
        return super().sizeHint()

    def set_items(self, items: ItemSource):
        pairs = normalize_items(items)
        self.table.set_items(pairs)
        # 请求头/响应头/参数为 Key: Value 结构，用 headers 高亮避免全红
        self.text.set_text(items_to_text(pairs), lang=Language.HEADERS)

    def items(self) -> list[tuple[str, str]]:
        """当前键值对：以**正在显示的那一页**为准。

        用户在文本页手敲的行还没同步到表格（同步发生在切页时），反过来也一样，
        所以读哪一页必须跟着当前页走，不能固定读表格。
        """
        if self.stack.currentWidget() is self.text:
            return items_from_text(self.text.text())
        return self.table.items()

    def set_read_only(self, read_only: bool):
        """两页一起锁。只锁文本页会留下一个能改却读不出来的表格。"""
        self._editable = not read_only
        self.text.set_read_only(read_only)
        self.table.set_read_only(read_only)


class JsonTreeWidget(TreeWidget):
    """JSON 体树形视图 — 递归展示 dict / list / 标量，两列：键/值"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setHeaderLabels([self.tr("Key"), self.tr("Value")])
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
        f = QFont()
        f.setItalic(True)
        return f

    def _apply_count_color(self):
        """给带计数的单元格上灰色（遍历已建好的项）"""
        gray = EditorPalette.tree_count()
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

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._lang = Language.JSON
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
        self._btn_text.setToolTip(self.tr("Text view"))
        self._btn_tree = TransparentTooltipButton(BaseIcon.CONVERT_TO_TABLE, self)
        self._btn_tree.setToolTip(self.tr("Tree view"))

    def __init_layout(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.stack)

        # 与 KVDualPanel 一致：每个页面只显示自己的切换按钮，不额外占一行
        self.text.tool_layout.addWidget(self._btn_tree)
        self.tree.tool_layout.addWidget(self._btn_text)

    def __connect_signal_to_slot(self):
        self._btn_text.clicked.connect(lambda: self.stack.setCurrentWidget(self.text))
        self._btn_tree.clicked.connect(self._show_tree_page)
        self.stack.currentChanged.connect(self.updateGeometry)
        self.text.changed.connect(self.changed)

    @Slot()
    def _show_tree_page(self):
        # 树是文本的派生视图（只读），切过去之前按当前文本重建一次，
        # 否则用户改完 JSON 切到树上看到的还是旧结构。
        self._rebuild_tree(self.text.text())
        self.stack.setCurrentWidget(self.tree)

    def sizeHint(self) -> QSize:
        """把内部 QStackedWidget 当前页面的正确尺寸向上传递，解决嵌套错位。"""
        current = self.stack.currentWidget()
        if current:
            return current.sizeHint()
        return super().sizeHint()

    def set_text(self, text: str, lang: Language | str = Language.JSON):
        """设置文本并指定语言；JSON 时顺带建树。"""
        self._lang = Language.coerce(lang)
        self.text.set_text(text, lang=self._lang)
        self._rebuild_tree(text)

    def _rebuild_tree(self, text: str):
        if self._lang is not Language.JSON:
            self.tree.tree.clear()
            return
        try:
            self.tree.tree.set_data(json.loads(text))
        except ValueError:
            # json.JSONDecodeError 是 ValueError 子类；body 常常是被截断的 JSON。
            self.tree.tree.clear()

    def plain_text(self) -> str:
        """当前文本（含用户编辑）。树是只读派生视图，不参与回读。

        故意不叫 ``text()``：``self.text`` 已经是文本页控件，同名会被实例属性遮住。
        """
        return self.text.text()

    def set_read_only(self, read_only: bool):
        self.text.set_read_only(read_only)


__all__ = [
    "SORT_TRANSITION",
    "ItemDualPanel",
    "ItemSource",
    "ItemTableToolWidget",
    "ItemTableWidget",
    "JsonDualPanel",
    "JsonTreePanel",
    "JsonTreeWidget",
    "SortState",
    "ToolPlainTextEdit",
    "ToolWidget",
    "items_from_text",
    "items_to_text",
    "normalize_items",
]
