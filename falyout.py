import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FlowLayout,
    Theme,
    TransparentTogglePushButton,
    TransparentToggleToolButton,
    TransparentToolButton,
    isDarkTheme,
    setTheme,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)


class FilterBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        # 1. 左侧过滤组
        self.web_btn_group = QButtonGroup(self)
        self.web_btn_group.setExclusive(True)

        # 模拟大量标签
        tags = [
            "All",
            "HTTP",
            "HTTPS",
            "WebSocket",
            "SSE",
            "HTTP1",
            "HTTP2",
            "JSON",
            "XML",
            "文本",
            "HTML",
            "JS",
            "图片",
            "1xx",
            "2xx",
            "3xx",
            "4xx",
            "5xx",
        ]

        self.filter_btns = []
        for t in tags:
            btn = TransparentTogglePushButton(t, self)
            btn.setMinimumWidth(0)  # 消除默认留白
            btn.setFixedHeight(24)
            btn.setCheckable(True)
            self.web_btn_group.addButton(btn)
            self.filter_btns.append(btn)

        self.filter_btns[0].setChecked(True)

        # 2. 右侧工具组 (注意顺序：搜索在前，保存在后，或者根据你的喜好)
        # 根据图2，搜索在上面，保存在下面。所以在流式布局中，搜索应该先添加。
        self.search_btn = TransparentToggleToolButton(FIF.SEARCH, self)
        self.search_btn.setFixedSize(28, 28)

        self.save_btn = TransparentToolButton(FIF.SAVE_AS, self)
        self.save_btn.setFixedSize(28, 28)

    def __init_layout(self):
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(5, 5, 5, 5)
        self.main_layout.setSpacing(0)

        # 1. 左侧（保持不变）
        self.left_container = QWidget()
        self.left_flow = FlowLayout(self.left_container)
        for btn in self.filter_btns:
            self.left_flow.addWidget(btn)

        # 2. 右侧：关键修改点
        self.right_container = QWidget()

        # 将 70 增加到 85。
        # 理由：28(搜) + 28(存) + 5(间距) + 5(边距) + 10(冗余缓冲区) ≈ 76px
        self.right_container.setFixedWidth(85)

        self.right_flow = FlowLayout(self.right_container)
        # 消除不必要的边距，给图标留出最大空间
        self.right_flow.setContentsMargins(0, 0, 0, 0)
        self.right_flow.setSpacing(4)

        self.right_flow.addWidget(self.search_btn)
        self.right_flow.addWidget(self.save_btn)

        # 3. 组装
        self.main_layout.addWidget(self.left_container, 1)
        self.main_layout.addWidget(self.right_container, 0, Qt.AlignmentFlag.AlignTop)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    setTheme(Theme.DARK)
    window = QWidget()
    window.resize(800, 200)
    layout = QVBoxLayout(window)
    layout.addWidget(FilterBar(window))
    layout.addStretch(1)

    bg = "#1e1e1e" if isDarkTheme() else "#f3f3f3"
    window.setStyleSheet(f"background-color: {bg};")
    window.show()
    sys.exit(app.exec())
