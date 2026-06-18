import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QWidget,
    QVBoxLayout,
)
from qfluentwidgets import (
    FluentIcon,
    TransparentTogglePushButton,
    TransparentToggleToolButton,
    TransparentToolButton,
    VerticalSeparator,
    Theme,
    isDarkTheme,
    setTheme,
    setThemeColor,
    PushButton,
)

from ferret.views.common.icon import BaseIcon


class FilterBar(QWidget):
    def __init__(self, parent: QWidget):
        super().__init__(parent)

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.web_button_group = QButtonGroup(self)
        self.web_button_group.setExclusive(True)
        self.all_button = TransparentTogglePushButton(self)
        self.all_button.setText("All")
        self.all_button.setChecked(True)
        self.http_button = TransparentTogglePushButton(self)
        self.http_button.setText("HTTP")
        self.https_button = TransparentTogglePushButton(self)
        self.https_button.setText("HTTPS")

        self.web_button_group.addButton(self.all_button)
        self.web_button_group.addButton(self.http_button)
        self.web_button_group.addButton(self.https_button)
        
        self.sep = VerticalSeparator(self)
        self.sep.setFixedHeight(20)

        self.json_button = TransparentTogglePushButton(self)
        self.json_button.setText("JSON")


        self.add_button = TransparentToolButton(self)
        self.add_button.setIcon(BaseIcon.BOOKMARK_ADD)
        self.add_button.hide()
        self.search_button = TransparentToggleToolButton(self)
        self.search_button.setIcon(FluentIcon.SEARCH)

    def __init_layout(self):
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setSpacing(1)
        self.h_layout.setContentsMargins(0, 0, 0, 0)

        self.h_layout.addWidget(self.all_button)
        self.h_layout.addWidget(self.http_button)
        self.h_layout.addWidget(self.https_button)
        self.h_layout.addWidget(self.sep)
        self.h_layout.addWidget(self.json_button)
        self.h_layout.addStretch(1)
        self.h_layout.addWidget(self.add_button)
        self.h_layout.addWidget(self.search_button)

    def __connect_signal_to_slot(self):
        self.search_button.clicked.connect(
            lambda: self.add_button.setVisible(self.add_button.isHidden())
        )


if __name__ == "__main__":
    from ferret.config import resources_rc

    class ThemeDemoWrapper(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Ferret FilterBar 主题切换演示")
            self.resize(800, 300)

            # 设置初始主题色（可选）
            setThemeColor("#00a3a9")

            self.main_layout = QVBoxLayout(self)
            self.main_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            # 1. 加入你的 FilterBar
            self.filter_bar = FilterBar(self)
            self.main_layout.addWidget(self.filter_bar)

            # 2. 加入切换按钮
            self.toggle_theme_btn = PushButton("切换 亮/暗 模式", self)
            self.toggle_theme_btn.setFixedWidth(200)
            self.toggle_theme_btn.clicked.connect(self.toggle_theme)
            self.main_layout.addWidget(self.toggle_theme_btn)

            # 初始化背景颜色
            self.update_background()

        def toggle_theme(self):
            # 核心逻辑：判断当前主题并切换
            new_theme = Theme.LIGHT if isDarkTheme() else Theme.DARK
            setTheme(new_theme)

            # 切换后需要手动更新一下父容器的背景色
            self.update_background()

        def update_background(self):
            # 根据亮暗模式设置不同的背景色，让界面更有层次感
            color = "#202020" if isDarkTheme() else "#f3f3f3"
            self.setStyleSheet(f"background-color: {color};")

    app = QApplication(sys.argv)

    # 创建包裹窗口
    demo = ThemeDemoWrapper()
    demo.show()

    sys.exit(app.exec())