"""JSON 编辑器卡片 — 继承 CodeCard，增加 JSON 格式化功能"""

import json
import sys

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QApplication, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    isDarkTheme,
)

from ferret.views.common.info_bar import show_error, show_success, show_warning
from ferret.views.common.line_edit import CodeCard


class JsonCard(CodeCard):
    """JSON 编辑器卡片 — 继承 CodeCard，增加 JSON 格式化按钮"""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            json_highlight=True,
            highlight_current_line=True,
            show_search=False,
        )

    def __init_widget(self, json_highlight: bool, highlight_current_line: bool):
        """重写父类初始化，添加 format 按钮"""
        super().__init_widget(json_highlight, highlight_current_line)

        self.formatBtn = TransparentToolButton(FluentIcon.CODE, self)
        self.formatBtn.setToolTip("格式化 JSON")
        self.formatBtn.installEventFilter(
            ToolTipFilter(self.formatBtn, 1000, ToolTipPosition.TOP)
        )

    def __init_layout(self):
        """重写父类布局，插入 format 按钮"""
        super().__init_layout()

        # 在工具栏中插入 format 按钮（在 wrap 按钮之前）
        button_layout: QHBoxLayout = self.button_bar.layout()
        # button_layout 顺序: stretch, [search], wrap, copy
        # 需要在 wrap 之前插入 format
        # 找到 wrap_btn 的位置
        for i in range(button_layout.count()):
            item = button_layout.itemAt(i)
            if item and item.widget() is self.wrap_btn:
                button_layout.insertWidget(i, self.formatBtn)
                break

    def __connect_signal_to_slot(self):
        """重写父类信号连接，添加 format 按钮信号"""
        super().__connect_signal_to_slot()
        self.formatBtn.clicked.connect(self.__format_json)

    @Slot()
    def __format_json(self):
        raw_text = self.text_edit.toPlainText().strip()
        if not raw_text:
            return

        try:
            parsed = json.loads(raw_text)
            formatted = json.dumps(parsed, indent=4, ensure_ascii=False)
            self.text_edit.setPlainText(formatted)
        except json.JSONDecodeError as e:
            show_error(
                title="解析失败",
                content=f"错误在第 {e.lineno} 行, 第 {e.colno} 列",
                parent=self.window(),
            )


class Demo(QWidget):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(
            f"background-color: {'#202020' if isDarkTheme() else '#f3f3f3'};"
        )
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(40, 40, 40, 40)
        self.card = JsonCard(self)
        self.layout.addWidget(SubtitleLabel("JSON 编辑器"))
        self.layout.addWidget(self.card)
        self.resize(800, 600)

        test_json = '{"project":"FluentEditor","author":"Python","is_active":true,"version":1.0}'
        self.card.set_text(test_json)
        self.card._JsonCard__format_json()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = Demo()
    w.show()
    app.exec()
