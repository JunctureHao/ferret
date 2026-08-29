"""手工请求编辑页视图：顶栏（方法/URL/发送）+ 左右 Splitter（请求编辑 / 响应展示）。"""

from urllib.parse import urlencode, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    InfoBadge,
    InfoLevel,
    LineEdit,
    PillToolButton,
    PrimaryToolButton,
    ToolTipFilter,
)

from ferret.apps.common.edit import (
    ItemDualPanel,
    Language,
    ToolPlainTextEdit,
)
from ferret.apps.common.flow.detail import ResponsePane, status_level
from ferret.apps.common.info_bar import show_error
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.compose.controllers import ComposeController
from ferret.core.mitm import ComposeResult, human

# 方法下拉的固定词表，下拉框只可从中选择（不可输入）。
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# 请求体「数据类型」下拉：(显示名, 高亮语言, 默认 Content-Type)。
# 显示名进翻译，语言/类型是常量。
BODY_KINDS: tuple[tuple[str, Language, str], ...] = (
    ("JSON", Language.JSON, "application/json"),
    ("XML", Language.XML, "application/xml"),
    ("Text", Language.HTTP, "text/plain"),
)


class ComposeInterface(QWidget):
    """编辑页：自己拼请求发出去，可选不进流量列表。"""

    def __init__(self, controller: ComposeController, parent=None):
        super().__init__(parent)
        self.setObjectName("ComposeInterface")
        self.controller = controller

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # ── 组件 ──────────────────────────────────

    def __init_widget(self):
        # 顶栏。方法只可从固定词表下拉选择，不可输入。
        self.method_combo = ComboBox(self)
        self.method_combo.addItems(METHODS)
        self.method_combo.setText("GET")
        self.method_combo.setFixedWidth(110)

        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText("https://example.com/api")
        self.url_edit.setClearButtonEnabled(True)

        # 「进入流量列表」：嵌在输入框尾部的胶囊开关，点一下选中（选中色），
        # 再点取消。挂进 url_edit 自己的布局并登记到 rightButtons，
        # _adjustTextMargins 会自动给文本让出右侧边距（宽度保持 30，
        # 与它的单按钮记账一致；库内 SearchLineEdit/PasswordLineEdit 同款做法）。
        self.record_btn = PillToolButton(FluentIcon.IOT, self.url_edit)
        self.record_btn.setChecked(True)
        self.record_btn.setFixedSize(30, 25)
        self.record_btn.setToolTip(
            self.tr(
                "Record to flow list; when off, the request is still sent through "
                "the proxy core (rewrite/gateway/intercept rules apply) but does "
                "not appear in the flow list"
            )
        )
        self.record_btn.installEventFilter(ToolTipFilter(self.record_btn, 700))
        self.url_edit.rightButtons.append(self.record_btn)
        self.url_edit.hBoxLayout.addWidget(
            self.record_btn, 0, Qt.AlignmentFlag.AlignRight
        )
        self.url_edit._adjustTextMargins()

        # 顶栏主操作：加宽的图标按钮（Fluent 强调按钮比例，16px 图标居中，
        # 高度保持控件默认值，与输入框一致）。
        self.send_btn = PrimaryToolButton(FluentIcon.SEND, self)
        self.send_btn.setFixedWidth(96)

        # 左侧：请求编辑
        self.request_panel = TabPanel(self)
        self.request_panel.setTabFontSize(12)
        self.request_panel.close_button.hide()

        self.params_card = ItemDualPanel(True, self)
        self.headers_card = ItemDualPanel(True, self)
        self.body_edit = ToolPlainTextEdit(self)
        self.body_kind_combo = ComboBox(self)
        self.body_kind_combo.addItems([kind for kind, _, _ in BODY_KINDS])
        self.body_kind_combo.setFixedWidth(96)
        self.body_edit.tool_layout.addWidget(BodyLabel(self.tr("Content type"), self))
        self.body_edit.tool_layout.addWidget(self.body_kind_combo)

        self.request_panel.addTab("Params", self.params_card, self.tr("Params"))
        self.request_panel.addTab("Headers", self.headers_card, self.tr("Headers"))
        self.request_panel.addTab("Body", self.body_edit, self.tr("Body"))
        self._sync_body_kind(0)

        # 右侧：响应展示（复用抓包详情页的 ResponsePane，Raw 页没有 flow 可问，
        # 详情字典手工拼的那份兜底已够）。
        self.response_panel = TabPanel(self)
        self.response_panel.setTabFontSize(12)
        self.response_panel.close_button.hide()
        self.response_pane = ResponsePane(self)
        self.response_panel.addTab("Response", self.response_pane, self.tr("Response"))
        self.timing_widget = QWidget(self)
        self.response_panel.addTab("Timing", self.timing_widget, self.tr("Timing"))

        timing_form = QFormLayout(self.timing_widget)
        timing_form.setContentsMargins(12, 12, 12, 12)
        self.timing_total = BodyLabel("—", self)
        self.timing_size = BodyLabel("—", self)
        self.timing_server = BodyLabel("—", self)
        timing_form.addRow(self.tr("Duration"), self.timing_total)
        timing_form.addRow(self.tr("Response size"), self.timing_size)
        timing_form.addRow(self.tr("Server address"), self.timing_server)

        # 状态徽标（右上角，仿抓包详情的状态格）
        self.status_badge = InfoBadge(self)
        self.status_badge.setLevel(InfoLevel.INFOAMTION)
        self.status_badge.hide()
        self.response_panel.tab_layout.insertWidget(2, self.status_badge)

        # 初始空态提示（右侧整页）：第一次出结果前显示。与 flow 表格的
        # FlowEmptyState 同一个模式 —— QStackedWidget 整页切换，不用 hide()
        # 叠加（悬空子控件会浮在左上角）。
        self.empty_hint = QWidget(self)
        hint_label = CaptionLabel(
            self.tr("Edit the request on the left and hit Send"), self.empty_hint
        )
        hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint_layout = QVBoxLayout(self.empty_hint)
        hint_layout.addStretch(1)
        hint_layout.addWidget(hint_label)
        hint_layout.addStretch(1)

        # 右侧：空态 ↔ 响应内容 整页切换
        self.response_stack = QStackedWidget(self)
        self.response_stack.addWidget(self.response_panel)
        self.response_stack.addWidget(self.empty_hint)
        self.response_stack.setCurrentWidget(self.empty_hint)

    def __init_layout(self):
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.method_combo)
        top.addWidget(self.url_edit, stretch=1)
        top.addWidget(self.send_btn)

        self.splitter = BaseSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.request_panel)
        self.splitter.addWidget(self.response_stack)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        # 初始 50/50 不能指望构造期：QSplitter 初始分位只认两侧 sizeHint 的
        # 比例（本页 384:510），Ignored 策略 / stretch / setSizes([1, 1]) 实测
        # 都掰不动它；要拿真实宽度换算（set_equal_sizes）才生效，所以挂到
        # 首次 showEvent 的下一拍（与 flow 页 _apply_equal_sizes 同一模式）。
        self._splitter_normalized = False

        main = QVBoxLayout(self)
        main.setContentsMargins(12, 8, 12, 12)
        main.setSpacing(8)
        main.addLayout(top)
        main.addWidget(self.splitter, stretch=1)

    def __connect_signal_to_slot(self):
        self.send_btn.clicked.connect(self._on_send)
        self.url_edit.returnPressed.connect(self._on_send)
        self.body_kind_combo.currentIndexChanged.connect(self._sync_body_kind)
        self.controller.sending_changed.connect(self._on_sending_changed)
        self.controller.result_ready.connect(self._on_result)
        self.controller.send_failed.connect(self._on_send_failed)

    # ── 行为 ──────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 只在首次可见时平分一次：singleShot 等布局把这页的真实宽度定下来，
        # 之后用户手动拖过分隔条，再切页回来不再打扰。
        if not self._splitter_normalized:
            self._splitter_normalized = True
            QTimer.singleShot(0, self.splitter.set_equal_sizes)

    @Slot(int)
    def _sync_body_kind(self, index: int) -> None:
        """「数据类型」下拉 → 编辑器高亮语言。Content-Type 不代写：用户可能在
        请求头里已经给了自己的值，静默覆盖比不写更糟（与 curl 一致）。"""
        _, lang, _ = BODY_KINDS[index]
        self.body_edit.code_widget.set_language(lang)

    def _collect_headers(self) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self.headers_card.items() if k.strip()]

    def _collect_url(self) -> str:
        """URL 输入框 + 参数页合并。参数页是权威 query：编辑页语义上
        「参数」就是 URL 的查询串，两边各存一份只会互相打脸。

        端口跟随 scheme：显式写出 `http://…:443` / `https://…:80` 几乎必是笔误
        （明文打到 HTTPS 端口，服务器直接回 400），还原为 scheme 的默认端口；
        其余显式端口（如 8080）不动。"""
        url = self.url_edit.text().strip()
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if (parts.scheme == "http" and port == 443) or (
            parts.scheme == "https" and port == 80
        ):
            host = parts.hostname or ""
            if ":" in host:  # IPv6：hostname 不带方括号，拼回去得补上
                host = f"[{host}]"
            parts = parts._replace(netloc=host)
        pairs = [(k, v) for k, v in self.params_card.items() if k.strip()]
        query = urlencode(pairs) if pairs else parts.query
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )

    @Slot()
    def _on_send(self):
        method = self.method_combo.currentText().strip()
        url = self._collect_url()
        if not url:
            show_error(self.tr("Send failed"), self.tr("The URL is empty"), self)
            return

        self.controller.send(
            method,
            url,
            self._collect_headers(),
            self.body_edit.text(),
            record=self.record_btn.isChecked(),
        )

    @Slot(bool)
    def _on_sending_changed(self, sending: bool):
        self.send_btn.setDisabled(sending)

    @Slot(object)
    def _on_result(self, result: ComposeResult):
        detail = result.detail
        if result.error:
            show_error(self.tr("Request failed"), result.error, self)
        self.response_stack.setCurrentWidget(self.response_panel)
        self.response_pane.set_data(detail)

        status = str(detail.get("Status Code", "Error" if result.error else ""))
        if status:
            self.status_badge.setText(status)
            self.status_badge.setLevel(status_level(status))
            self.status_badge.adjustSize()
            self.status_badge.show()
        else:
            self.status_badge.hide()

        self.timing_total.setText(str(detail.get("Duration", "—")))
        total = int(detail.get("res_total_size") or 0)
        self.timing_size.setText(human.pretty_size(total) if total else "—")
        self.timing_server.setText(str(detail.get("Server Address", "—")))

    @Slot(str, str)
    def _on_send_failed(self, title: str, content: str):
        show_error(title, content, self)
