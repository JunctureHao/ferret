"""API 请求界面 — 发送 HTTP 请求并查看响应"""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
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


class RequestToolBar(QWidget):
    """请求工具栏 — URL 输入 + 方法选择 + 发送按钮"""

    requestSent = Signal(str, str)  # method, url

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.method_combo = ComboBox(self)
        self.method_combo.addItems(["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
        self.method_combo.setFixedWidth(100)

        self.url_input = LineEdit(self)
        self.url_input.setPlaceholderText("输入请求 URL，例如 https://api.example.com/users")
        self.url_input.setClearButtonEnabled(True)

        self.send_btn = PrimaryPushButton("发送", self)
        self.send_btn.setIcon(FluentIcon.SEND)
        self.send_btn.setFixedWidth(100)

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

    @Slot()
    def __on_send_clicked(self):
        method = self.method_combo.currentText()
        url = self.url_input.text().strip()
        if url:
            self.requestSent.emit(method, url)


class RequestBodyPanel(SimpleCardWidget):
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


class ResponsePanel(SimpleCardWidget):
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

    def set_response(self, status_code: int, body: str):
        """设置响应内容"""
        color = "green" if 200 <= status_code < 300 else "red"
        self.status_label.setText(f"<span style='color: {color}'>Status: {status_code}</span>")
        self.response_edit.setPlainText(body)


class RequestInterface(QWidget):
    """API 请求界面"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("RequestInterface")
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.toolbar = RequestToolBar(self)
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
        self.toolbar.requestSent.connect(self.__on_request_sent)

    @Slot(str, str)
    def __on_request_sent(self, method: str, url: str):
        """处理请求发送（这里先做 mock，后续接入实际 HTTP 客户端）"""
        # TODO: 接入实际的 HTTP 请求逻辑
        self.response_panel.set_response(
            200,
            f"Mock Response\n\nMethod: {method}\nURL: {url}\n\n请接入实际的 HTTP 请求逻辑。"
        )
