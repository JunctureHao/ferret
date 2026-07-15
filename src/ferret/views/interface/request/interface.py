"""API 请求界面 — 发送 HTTP 请求并查看响应"""

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    TextEdit,
    TransparentToolButton,
)

from ferret.controllers.request import RequestController


class RequestToolBar(QWidget):
    """请求工具栏 — URL 输入 + 方法选择 + 发送按钮。

    直接持有 controller，发送在本地闭环：采集输入 → 调 controller.send()。
    线程细节完全由 controller 内部管理，UI 无感知。
    """

    def __init__(self, controller: RequestController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.method_combo = ComboBox(self)
        self.method_combo.addItems(self.controller.get_http_methods())
        self.method_combo.setFixedWidth(110)

        self.url_input = LineEdit(self)
        self.url_input.setPlaceholderText("https://httpbin.org/get")
        self.url_input.setClearButtonEnabled(True)

        self.send_btn = PrimaryPushButton(FluentIcon.SEND, "发送", self)
        self.send_btn.setFixedWidth(110)

    def __init_layout(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.method_combo)
        layout.addWidget(self.url_input, stretch=1)
        layout.addWidget(self.send_btn)

    def __connect_signal_to_slot(self):
        self.send_btn.clicked.connect(self.__on_send_clicked)
        self.url_input.returnPressed.connect(self.__on_send_clicked)
        # 内容返回（成功或失败）即结束转圈
        self.controller.responseReady.connect(self.stop_loading)
        self.controller.errorOccurred.connect(self.stop_loading)

    def start_loading(self):
        """进入发送中状态：禁用按钮 + 显示转圈。"""
        self.send_btn.setDisabled(True)

    def stop_loading(self):
        """发送结束：恢复按钮 + 隐藏转圈。"""
        self.send_btn.setDisabled(False)

    @Slot()
    def __on_send_clicked(self):
        method = self.method_combo.currentText()
        url = self.url_input.text().strip()
        if url:
            # 点按钮即转圈
            self.start_loading()
            # 直接调 controller，线程管理与结果广播都在 controller 内
            self.controller.send(method, url)


class RequestBodyPanel(QWidget):
    """请求体面板"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        self.title_label = StrongBodyLabel("请求体 (Body)", self)
        self.body_edit = TextEdit(self)
        self.body_edit.setPlaceholderText("输入请求体内容 (JSON / Form Data / Raw)")
        self.body_edit.setMinimumHeight(150)

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_edit)


class ResponsePanel(QWidget):
    """响应面板"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        self.title_label = StrongBodyLabel("响应", self)
        self.status_label = BodyLabel("", self)
        self.response_edit = TextEdit(self)
        self.response_edit.setReadOnly(True)
        self.response_edit.setPlaceholderText("响应内容将显示在这里...")

        self.copy_btn = TransparentToolButton(FluentIcon.COPY, self)
        self.copy_btn.setToolTip("复制响应")

    def __init_layout(self):
        header_layout = QHBoxLayout()
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.status_label)
        header_layout.addWidget(self.copy_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(header_layout)
        layout.addWidget(self.response_edit)

    def set_response(self, status_code: int, body: str, version: str = ""):
        """设置响应内容"""
        color = "green" if 200 <= status_code < 300 else "red"
        self.status_label.setText(
            f"<span style='color: {color}'>Status: {status_code}</span>"
        )
        prefix = f"[{version}]\n\n" if version else ""
        self.response_edit.setPlainText(prefix + body)


class RequestInterface(SimpleCardWidget):
    """API 请求界面 — 仅负责布局，不掺和发送逻辑。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("RequestInterface")
        self.controller = RequestController(self)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.toolbar = RequestToolBar(self.controller, self)
        self.body_panel = RequestBodyPanel(self)
        self.response_panel = ResponsePanel(self)

    def __init_layout(self):
        # 使用分割器让请求体和响应可调整大小
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        splitter.addWidget(self.body_panel)
        splitter.addWidget(self.response_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self.toolbar)
        main_layout.addWidget(splitter)

    def __connect_signal_to_slot(self):
        # 响应结果由 controller 信号广播给各面板
        self.controller.responseReady.connect(self.response_panel.set_response)


if __name__ == "__main__":

    def main():
        """独立运行以测试请求界面窗口"""
        import sys

        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)
        window = RequestInterface()
        window.resize(800, 600)
        window.setWindowTitle("API 请求界面 - 测试")
        window.show()
        sys.exit(app.exec())

    main()
