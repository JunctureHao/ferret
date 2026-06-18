"""通用键值对显示组件 — 以 TreeWidget 显示键值对，支持复制"""

from collections.abc import Callable

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QPlainTextEdit,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)
from qfluentwidgets import (
    FluentIcon,
    InfoBar,
    SimpleCardWidget,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    TreeWidget,
)


class HeaderCard(SimpleCardWidget):
    """键值对显示组件 — 以 TreeWidget 显示键值对，支持自定义格式复制"""

    def __init__(
        self,
        copy_tooltip: str = "复制 Headers",
        empty_copy_content: str = "没有可复制的 Headers",
        copy_success_content: str = "Headers 已复制到剪贴板",
        copy_formatter: Callable[[dict[str, str]], str] | None = None,
        key_column_width: int = 180,
        parent=None,
    ):
        """初始化组件

        Args:
            copy_tooltip: 复制按钮提示文本
            empty_copy_content: 空数据时复制的警告内容
            copy_success_content: 复制成功的提示内容
            copy_formatter: 自定义复制格式函数，接收 dict 返回 str。
                           默认使用 "Key: Value" 每行一个的格式。
            key_column_width: Key 列宽度
            parent: 父组件
        """
        super().__init__(parent)
        self._items: dict[str, str] = {}
        self._copy_tooltip = copy_tooltip
        self._empty_copy_content = empty_copy_content
        self._copy_success_content = copy_success_content
        self._copy_formatter = copy_formatter
        self._key_column_width = key_column_width
        self._is_text_mode = False
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.setBorderRadius(0)

        # 表格视图
        self.tree = TreeWidget()
        self.tree.setHeaderLabels(["Name", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(0)
        self.tree.header().setVisible(False)

        # 文本视图
        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        # 堆叠视图
        self.stacked = QStackedWidget()
        self.stacked.addWidget(self.tree)
        self.stacked.addWidget(self.text_edit)

        # 视图切换按钮
        self.toggle_button = TransparentToolButton(self)
        self.toggle_button.setIcon(FluentIcon.TILES)
        self.toggle_button.setToolTip(self.tr("切换视图"))
        self.toggle_button.installEventFilter(
            ToolTipFilter(self.toggle_button, 1000, ToolTipPosition.TOP)
        )

        # 复制按钮
        self.copy_button = TransparentToolButton(self)
        self.copy_button.setIcon(FluentIcon.COPY)
        self.copy_button.setToolTip(self.tr(self._copy_tooltip))
        self.copy_button.installEventFilter(
            ToolTipFilter(self.copy_button, 1000, ToolTipPosition.TOP)
        )

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.addWidget(self.toggle_button)
        btn_layout.addWidget(self.copy_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addWidget(self.stacked, 1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.copy_button.clicked.connect(self.__on_copy)
        self.toggle_button.clicked.connect(self.__toggle_view)

    def set_headers(self, headers: dict):
        """设置 header 数据（向后兼容）

        Args:
            headers: HTTP Headers 字典
        """
        self.set_items(headers)

    def set_items(self, items: dict[str, str]):
        """设置键值对数据

        Args:
            items: 键值对字典 {key: value, ...}
        """
        self._items = items if items else {}
        self.tree.clear()
        self.text_edit.clear()

        if not items:
            return

        # 填充表格视图
        for key, value in items.items():
            item = QTreeWidgetItem(self.tree)
            item.setText(0, str(key))
            item.setText(1, str(value))
            item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignLeft)

        self.tree.setColumnWidth(0, self._key_column_width)
        self.tree.header().setStretchLastSection(True)

        # 填充文本视图
        text_lines = [f"{k}: {v}" for k, v in items.items()]
        self.text_edit.setPlainText("\n".join(text_lines))

    @property
    def items(self) -> dict[str, str]:
        return self._items

    def __default_copy_formatter(self, items: dict[str, str]) -> str:
        """默认复制格式：Key: Value 每行一个"""
        return "\n".join(f"{k}: {v}" for k, v in items.items())

    @Slot()
    def __toggle_view(self):
        """切换表格/文本视图"""
        self._is_text_mode = not self._is_text_mode
        if self._is_text_mode:
            self.stacked.setCurrentWidget(self.text_edit)
            self.toggle_button.setIcon(FluentIcon.LIBRARY)
            self.toggle_button.setToolTip(self.tr("切换为表格视图"))
        else:
            self.stacked.setCurrentWidget(self.tree)
            self.toggle_button.setIcon(FluentIcon.TILES)
            self.toggle_button.setToolTip(self.tr("切换为文本视图"))

    @Slot()
    def __on_copy(self):
        """复制数据到剪贴板"""
        if not self._items:
            InfoBar.warning(
                title=self.tr("提示"),
                content=self.tr(self._empty_copy_content),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position="BottomCenter",
                duration=3000,
                parent=self.window(),
            )
            return

        formatter = self._copy_formatter or self.__default_copy_formatter
        text = formatter(self._items)
        QApplication.clipboard().setText(text)
        InfoBar.success(
            title=self.tr("成功"),
            content=self.tr(self._copy_success_content),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position="BottomCenter",
            duration=3000,
            parent=self.window(),
        )
