import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)
from qfluentwidgets import (
    PillPushButton,
    PushButton,
    SearchLineEdit,
    Theme,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    VerticalSeparator,
    isDarkTheme,
    setTheme,
    setThemeColor,
)


class FluentFilterBar(QWidget):
    """
    Ferret 风格过滤搜索栏
    包含：搜索框 + 互斥协议组 + 多选类型组 + 多选状态组
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(10, 5, 10, 5)
        self.h_layout.setSpacing(8)

        # 1. 搜索框 (左侧对齐)
        self.search_input = SearchLineEdit(self)
        self.search_input.setPlaceholderText("过滤请求...")
        self.search_input.setFixedWidth(200)
        self.h_layout.addWidget(self.search_input)

        self.h_layout.addWidget(VerticalSeparator())

        # 2. 互斥协议组 (All | HTTP | HTTPS)
        self.protocol_group = QButtonGroup(self)
        self.protocol_group.setExclusive(True)  # 开启互斥：点一个，另一个自动弹起

        protocols = ["All", "HTTP", "HTTPS"]
        for text in protocols:
            btn = PillPushButton(text, self)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            if text == "All":
                btn.setChecked(True)

            self.protocol_group.addButton(btn)
            self.h_layout.addWidget(btn)

        self.h_layout.addWidget(VerticalSeparator())

        # 3. 多选数据组 (JSON | XML | 文本)
        data_types = ["JSON", "XML", "文本"]
        for text in data_types:
            btn = PillPushButton(text, self)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            # 不加入 protocol_group，因此它们支持多选
            self.h_layout.addWidget(btn)

        self.h_layout.addWidget(VerticalSeparator())

        # 4. 多选状态组 (1xx | 2xx | 3xx)
        status_codes = ["1xx", "2xx", "3xx"]
        for text in status_codes:
            btn = PillPushButton(text, self)
            btn.setCheckable(True)
            btn.setFixedHeight(28)
            self.h_layout.addWidget(btn)

        # 弹性空间
        self.h_layout.addStretch(1)

        # 右侧辅助按钮
        self.clear_btn = TransparentToolButton(FIF.CLOSE, self)
        self.clear_btn.setToolTip("清除所有过滤条件")
        self.clear_btn.installEventFilter(
            ToolTipFilter(self.clear_btn, 1000, ToolTipPosition.TOP)
        )
        self.h_layout.addWidget(self.clear_btn)

        # 样式微调：底部细线增加层级感
        self.update_border()

    def update_border(self):
        color = "rgba(255, 255, 255, 0.1)" if isDarkTheme() else "rgba(0, 0, 0, 0.1)"
        self.setStyleSheet(f"FluentFilterBar {{ border-bottom: 1px solid {color}; }}")


class ThemeColorDemo(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ferret Filter Bar 交互演示")
        self.resize(1100, 450)

        setThemeColor("#00a3a9")  # 你的专属青色
        setTheme(Theme.DARK)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setSpacing(20)
        self.main_layout.setContentsMargins(30, 30, 30, 30)

        # 1. 模拟页面顶部工具栏
        title_label = QLabel("Ferret 网络请求过滤")
        title_label.setStyleSheet("font: 20px 'Segoe UI Semibold';")
        self.main_layout.addWidget(title_label)

        # 2. 核心过滤栏
        self.filter_bar = FluentFilterBar(self)
        self.main_layout.addWidget(self.filter_bar)

        # 3. 颜色切换测试区
        self.main_layout.addSpacing(40)
        self.main_layout.addWidget(QLabel("动态主题色测试 (观察下方选中药丸颜色变化):"))

        color_layout = QHBoxLayout()
        colors = [
            ("#00a3a9", "默认青"),
            ("#0078d4", "经典蓝"),
            ("#ff5722", "活力橙"),
            ("#00b140", "清新绿"),
        ]
        for color_hex, name in colors:
            btn = PushButton(name)
            btn.clicked.connect(lambda checked, c=color_hex: self.change_theme_color(c))
            color_layout.addWidget(btn)
        self.main_layout.addLayout(color_layout)

        # 4. 亮暗切换
        self.main_layout.addStretch(1)
        self.mode_btn = PushButton("切换 亮/暗 模式")
        self.mode_btn.setFixedWidth(150)
        self.mode_btn.clicked.connect(self.toggle_mode)
        self.main_layout.addWidget(self.mode_btn, 0, Qt.AlignmentFlag.AlignRight)

        self.update_bg()

    def change_theme_color(self, color):
        setThemeColor(color)

    def toggle_mode(self):
        new_theme = Theme.LIGHT if isDarkTheme() else Theme.DARK
        setTheme(new_theme)
        self.filter_bar.update_border()  # 同步更新边框颜色
        self.update_bg()

    def update_bg(self):
        bg = "#1e1e1e" if isDarkTheme() else "#fcfcfc"
        self.setStyleSheet(f"ThemeColorDemo {{ background-color: {bg}; }}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    demo = ThemeColorDemo()
    demo.show()
    sys.exit(app.exec())
