"""手工请求编辑页视图：顶栏（方法/URL/发送）+ 左右 Splitter（请求编辑 / 响应展示）。

顶栏独占一行；左侧是请求详情（参数/请求头/请求体，复用详情面板的可编辑组件），
右侧是响应区——`ResponsePane`（with_raw=False）承载 响应头/响应体/性能 三条标签：
「性能」页顶部是状态行（状态徽标 + 一句话摘要），下面按 时间/流量 两组卡片展示
`ComposeResult.detail` 里的时序与字节键。发送前右侧整页显示空态提示。
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QCoreApplication, Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    EditableComboBox,
    FluentIcon,
    InfoBadge,
    InfoLevel,
    LineEdit,
    PillToolButton,
    PrimaryToolButton,
    ToolButton,
    ToolTipFilter,
)

from ferret.apps.common.edit import (
    ItemDualPanel,
    JsonDualPanel,
    Language,
)
from ferret.apps.common.flow.detail import ResponsePane, status_level
from ferret.apps.common.flow.fields import (
    Field,
    OverviewPane,
    Section,
    _decoded_size,
    _ms,
    _size_of,
    format_time,
)
from ferret.apps.common.http_methods import METHODS
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import OrientationSplitter
from ferret.apps.compose.controllers import ComposeController
from ferret.apps.compose.curl_import import parse_curl
from ferret.core.mitm import ComposeResult, RequestEdit, human
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 请求体「数据类型」下拉：(显示名, 高亮语言, 默认 Content-Type)。
# 显示名进翻译，语言/类型是常量。
BODY_KINDS: tuple[tuple[str, Language, str], ...] = (
    ("JSON", Language.JSON, "application/json"),
    ("XML", Language.XML, "application/xml"),
    ("Text", Language.HTTP, "text/plain"),
)

# 只存标记：模块级求值赶在翻译器安装之前。与断点面板同一个锁模式，
# 文案各自一份（发送语义不同：那边是「放行」，这边是「发送」）。
_BINARY_HINT = QT_TRANSLATE_NOOP(
    "ComposeView",
    "This content is not valid UTF-8 (an archive, an image, or a non-UTF-8 charset), "
    "so it is locked read-only; it goes out unchanged when sent.",
)


def _body_lang(text: str) -> Language:
    """prefill 时按内容推断高亮语言：只在 JSON 与通用 HTTP 词法器之间二选一。"""
    return Language.JSON if text.lstrip()[:1] in ("{", "[") else Language.HTTP


# 「性能」页的两组卡片：键全部来自 `build_flow_detail` 的既有字段，渲染直接复用
# 概览页的声明式规格 + FieldCard（时间/流量两组与概览的 Timing/Size 同源，只是
# 挂在 compose 的详情字典上）。fields 里那几个下划线格式化器是同族模块的既有
# 积木，直接借力，不再抄一份。
_PERF_SECTIONS: tuple[Section, ...] = (
    Section(
        title=QT_TRANSLATE_NOOP("ComposePerf", "Time"),
        fields=(
            Field("Flow ID", "Flow ID", mono=True),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Flow created"),
                "Flow Created",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Client TLS handshake"),
                "Front TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Request start"),
                "req_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Request end"),
                "req_timestamp_end",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Request duration"),
                "req_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "TCP handshake"),
                "Back TCP Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Server TLS handshake"),
                "Back TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Response start"),
                "res_timestamp_start",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Response end"),
                "res_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Response duration"),
                "res_duration",
                fmt=_ms,
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "Total duration"), "Duration"),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("ComposePerf", "Traffic"),
        fields=(
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Request"), _size_of("req_total_size")
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Request headers"),
                _size_of("req_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Request body on the wire"),
                _size_of("req_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Request body decoded"),
                _decoded_size("req_wire_size", "req_decoded_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "Response"), _size_of("res_total_size")
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Response headers"),
                _size_of("res_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Response body on the wire"),
                _size_of("res_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- Response body decoded"),
                _decoded_size("res_wire_size", "res_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "Total"), _size_of("total_size")),
        ),
    ),
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
        # 顶栏。方法用可编辑下拉：prefill 可能带来词表外的方法（PROPFIND 等）。
        self.method_combo = EditableComboBox(self)
        self.method_combo.addItems(METHODS)
        self.method_combo.setCurrentText("GET")
        self.method_combo.setFixedWidth(110)

        # 二进制体直通：prefill 进非 UTF-8 内容时锁只读，原始字节存这里原样发出。
        self._raw_body: bytes = b""
        self._binary = False

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

        # 顶栏主操作：主色图标按钮。高度不动，**横向加宽** —— 用左右尺寸凸显
        # 它是这一页唯一的主动作，宽出周围一圈才镇得住顶栏。
        self.send_btn = PrimaryToolButton(FluentIcon.SEND, self)
        self.send_btn.setFixedWidth(96)
        self.send_btn.setToolTip(self.tr("Send this request"))

        # cURL 粘贴导入：读剪贴板灌表单，解析错误就地 show_error（v1 不做
        # 粘贴编辑对话框）。
        self.paste_curl_btn = ToolButton(FluentIcon.PASTE, self)
        self.paste_curl_btn.setToolTip(
            self.tr("Import a curl command from the clipboard")
        )
        self.paste_curl_btn.installEventFilter(ToolTipFilter(self.paste_curl_btn, 700))

        # 左侧：请求详情（参数/请求头/请求体，复用详情面板的可编辑组件）。
        self.request_panel = TabPanel(self)
        self.request_panel.setTabFontSize(12)
        self.request_panel.close_button.hide()

        self.params_card = ItemDualPanel(True, self)
        self.headers_card = ItemDualPanel(True, self)
        self.body_panel = JsonDualPanel(self)
        self.body_kind_combo = ComboBox(self)
        self.body_kind_combo.addItems([kind for kind, _, _ in BODY_KINDS])
        self.body_kind_combo.setFixedWidth(96)
        self.body_panel.text.tool_layout.addWidget(
            BodyLabel(self.tr("Content type"), self)
        )
        self.body_panel.text.tool_layout.addWidget(self.body_kind_combo)

        self.request_panel.addTab("Params", self.params_card, self.tr("Params"))
        self.request_panel.addTab("Headers", self.headers_card, self.tr("Headers"))
        # 二进制锁的提示条压在体编辑器下方（与断点面板的 body_box 同一个模式）。
        self.body_hint = CaptionLabel(
            QCoreApplication.translate("ComposeView", _BINARY_HINT), self
        )
        self.body_hint.setWordWrap(True)
        self.body_hint.hide()
        self.body_box = QWidget(self)
        body_layout = QVBoxLayout(self.body_box)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addWidget(self.body_panel, 1)
        body_layout.addWidget(self.body_hint)
        self.request_panel.addTab("Body", self.body_box, self.tr("Body"))
        self._sync_body_kind(0)
        # 请求头(N)：条数挂标签，随编辑实时变（参数变化实时写回 URL，见下）。
        self.headers_card.changed.connect(self._update_header_count)
        self.params_card.changed.connect(self._sync_params_to_url)
        self._update_header_count()

        # 右侧：响应区。`with_raw=False`：compose 的结果不需要原始报文兜底，
        # 标签只剩 响应头/响应体，「性能」由本页追加到末位。
        self.response_pane = ResponsePane(self, with_raw=False)

        # 「性能」页 = 状态行（徽标 + 摘要）+ 时间/流量两组卡片。状态行原先是
        # 右侧顶部一条独立横排，收进性能页后右侧标签行少了一层。
        self.status_badge = InfoBadge(self)
        self.status_badge.hide()
        self.status_label = CaptionLabel(self)
        status_row = QWidget(self)
        status_layout = QHBoxLayout(status_row)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        status_layout.addWidget(self.status_badge)
        status_layout.addWidget(self.status_label, 1)

        self.perf_overview = OverviewPane(sections=_PERF_SECTIONS)
        perf_page = QWidget(self)
        perf_layout = QVBoxLayout(perf_page)
        perf_layout.setContentsMargins(0, 0, 0, 0)
        perf_layout.setSpacing(8)
        perf_layout.addWidget(status_row)
        perf_layout.addWidget(self.perf_overview, 1)

        self.response_pane.addTab("Perf", perf_page, self.tr("Performance"))

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

        # 右侧：空态 ↔ 响应内容 整页切换。
        self.response_stack = QStackedWidget(self)
        self.response_stack.addWidget(self.response_pane)
        self.response_stack.addWidget(self.empty_hint)
        self.response_stack.setCurrentWidget(self.empty_hint)

    def __init_layout(self):
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.method_combo)
        top.addWidget(self.url_edit, stretch=1)
        top.addWidget(self.paste_curl_btn)
        top.addWidget(self.send_btn)

        # 跟随全局布局：设置里水平 → 左右排，垂直 → 上下排（本页不反转）。
        self.splitter = OrientationSplitter(parent=self)
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
        self.paste_curl_btn.clicked.connect(self._on_paste_curl)
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
        self.body_panel.text.code_widget.set_language(lang)

    @Slot()
    def _update_header_count(self) -> None:
        """`请求头(N)`：条数挂标签，随编辑实时变。"""
        count = len([k for k, _ in self.headers_card.items() if k.strip()])
        self.request_panel.setTabText(
            "Headers", self.tr("Request headers ({count})").format(count=count)
        )

    @Slot()
    def _sync_params_to_url(self) -> None:
        """参数变化实时写回 URL 栏：参数页是 query 的权威源。

        只在有键值时合并（幂等，urlencode 的结果再合并还是它自己）；参数清空时
        不动 URL —— 「删光参数」和「还没填」在空列表里是同一个信号，前者想让
        URL 上的查询串消失应该在 URL 栏里删。
        """
        pairs = [(k, v) for k, v in self.params_card.items() if k.strip()]
        if pairs:
            self.url_edit.setText(
                self._merge_query(self.url_edit.text().strip(), pairs)
            )

    def _merge_query(self, url: str, pairs: list[tuple[str, str]]) -> str:
        """参数合并进 URL 查询串（与断点面板 `_merge_query` 同一套端口规范：
        `http://…:443` / `https://…:80` 这类跨协议笔误还原为默认端口，
        本协议显式端口（含 443 本尊、8080）不动。"""
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
        pairs = [(k, v) for k, v in pairs if k.strip()]
        query = urlencode(pairs) if pairs else parts.query
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )

    def _collect_headers(self) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self.headers_card.items() if k.strip()]

    def _collect_url(self) -> str:
        """发送时的 URL = URL 栏 + 参数页合并。参数页是权威 query：编辑页语义上
        「参数」就是 URL 的查询串，两边各存一份只会互相打脸。"""
        return self._merge_query(self.url_edit.text().strip(), self.params_card.items())

    def prefill(self, edit: RequestEdit) -> None:
        """把一份请求草稿灌进表单（右键「Edit in Compose」/ cURL 导入）。

        静默覆盖正在编辑的内容：compose 页没有「未保存草稿」概念，加确认框
        成本大于收益。响应区复位——旧结果不属于这张新表单。
        """
        # setText 而不是 setCurrentText：后者的实现只认词表内项（findText 落空
        # 就静默不动），PROPFIND 这类词表外方法会留在旧值上。
        self.method_combo.setText(edit.method)
        self.url_edit.setText(edit.url)
        # query 拆进参数页（断点面板 `RequestPanel.load` 同款）：URL 栏留整串，
        # 参数页是权威源，发送时再合并回去。
        parts = urlsplit(edit.url)
        self.params_card.set_items(parse_qsl(parts.query, keep_blank_values=True))
        self.headers_card.set_items(list(edit.headers))
        self._load_body(edit.content)
        self.response_stack.setCurrentWidget(self.empty_hint)
        self.status_badge.hide()

    def _load_body(self, content: bytes) -> None:
        """UTF-8 可解码 → 进编辑器；不可解码 → 二进制锁，原样直通发送。"""
        self._raw_body = content
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            # 严格解码故意不加兜底：能显示的一定能一字节不差地发出去。
            self._binary = True
            text = ""
        else:
            self._binary = False
        lang = _body_lang(text)
        self.body_panel.set_text(text, lang)
        # 同步「数据类型」下拉：它驱动高亮语言，不能让它和实际高亮打架。
        self.body_kind_combo.setCurrentIndex(0 if lang is Language.JSON else 2)
        self.body_hint.setVisible(self._binary)
        self.body_panel.set_read_only(self._binary)

    @Slot()
    def _on_paste_curl(self) -> None:
        text = QApplication.clipboard().text().strip()
        # 空剪贴板 / 非 curl 开头在这里挡；语法细节错误由解析器报。
        if not text:
            show_error(
                self.tr("Import failed"), self.tr("The clipboard is empty"), self
            )
            return
        if not text.lower().startswith(("curl ", "curl.exe")):
            show_error(
                self.tr("Import failed"),
                self.tr("The clipboard does not contain a curl command"),
                self,
            )
            return
        try:
            edit = parse_curl(text)
        except ValueError as exc:
            show_error(self.tr("Import failed"), str(exc), self)
            return
        self.prefill(edit)
        show_success(self.tr("Success"), self.tr("Request imported from curl"), self)

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
            self._raw_body if self._binary else self.body_panel.plain_text(),
            record=self.record_btn.isChecked(),
        )

    @Slot(bool)
    def _on_sending_changed(self, sending: bool):
        self.send_btn.setDisabled(sending)
        if sending:
            self.status_badge.hide()
            self.status_label.setText(self.tr("Sending…"))

    @Slot(object)
    def _on_result(self, result: ComposeResult):
        detail = result.detail
        if result.error:
            show_error(self.tr("Request failed"), result.error, self)
        self.response_stack.setCurrentWidget(self.response_pane)
        self.response_pane.set_data(detail)
        self.perf_overview.set_data(detail)

        status = str(detail.get("Status Code", "Error" if result.error else ""))
        self.status_label.setText(self._summarize(detail, status))
        if status:
            self.status_badge.setText(status)
            self.status_badge.setLevel(status_level(status))
            self.status_badge.adjustSize()
            self.status_badge.show()
        else:
            self.status_badge.hide()

    @staticmethod
    def _summarize(detail: dict, status: str) -> str:
        """性能页状态行摘要：`200 OK · 12 ms · 1.2k · 1.2.3.4:443`。

        全部取自详情字典的既有键。
        """
        reason = str(detail.get("Reason", ""))
        head = f"{status} {reason}".strip() or "—"
        total = int(detail.get("res_total_size") or 0)
        parts = [
            part
            for part in (
                str(detail.get("Duration", "")),
                human.pretty_size(total) if total else "",
                str(detail.get("Server Address", "")),
            )
            if part
        ]
        return " · ".join([head, *parts])

    @Slot(str, str)
    def _on_send_failed(self, title: str, content: str):
        show_error(title, content, self)
        self.status_badge.setText("Error")
        self.status_badge.setLevel(InfoLevel.ERROR)
        self.status_badge.adjustSize()
        self.status_badge.show()
        self.status_label.setText(content)
