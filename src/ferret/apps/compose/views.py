"""手工请求编辑页视图：顶栏（方法/URL/发送）+ 左右 Splitter（请求编辑 / 响应展示）。

顶栏独占一行；左侧是请求详情（参数/请求头/请求体，复用详情面板的可编辑组件），
右侧是响应区——`ResponsePane`（with_raw=False）承载 响应头/响应体/性能 三条标签，
「性能」页展示瀑布图和时间/流量两组详情。发送前显示空态，等待响应时显示进度
提示，避免把上次结果当成当前响应。
"""

from __future__ import annotations

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
    CheckBox,
    ComboBox,
    FluentIcon,
    IconWidget,
    IndeterminateProgressRing,
    LineEdit,
    PrimaryPushButton,
    ToolButton,
    ToolTipFilter,
)

from ferret.apps.common.edit import (
    ItemDualPanel,
    JsonDualPanel,
    Language,
)
from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.common.flow.detail import ResponsePane
from ferret.apps.common.flow.fields import (
    Field,
    OverviewPane,
    Section,
    _decoded_size,
    _ms,
    _size_of,
    format_duration,
    format_time,
)
from ferret.apps.common.flow.timing import WaterfallBar, phases
from ferret.apps.common.http_methods import METHODS
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import OrientationSplitter
from ferret.apps.compose.controllers import ComposeController
from ferret.apps.compose.curl_import import parse_curl
from ferret.core.mitm import ComposeResult, RequestEdit
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
    "这段内容不是合法的 UTF-8（压缩包、图片，或非 UTF-8 字符集），已锁定为只读；发送时按原样发出。",
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
        title=QT_TRANSLATE_NOOP("ComposePerf", "时间"),
        fields=(
            Field("Flow ID", "Flow ID", mono=True),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "流量创建"),
                "Flow Created",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "客户端 TLS 握手"),
                "Front TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求开始"),
                "req_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求结束"),
                "req_timestamp_end",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求时长"),
                "req_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "TCP 握手"),
                "Back TCP Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "服务端 TLS 握手"),
                "Back TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应开始"),
                "res_timestamp_start",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应结束"),
                "res_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应时长"),
                "res_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "总耗时"),
                "duration_ms",
                fmt=format_duration,
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("ComposePerf", "流量"),
        fields=(
            Field(QT_TRANSLATE_NOOP("ComposePerf", "请求"), _size_of("req_total_size")),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求头"),
                _size_of("req_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求体（线上）"),
                _size_of("req_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求体（解压）"),
                _decoded_size("req_wire_size", "req_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "响应"), _size_of("res_total_size")),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应头"),
                _size_of("res_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应体（线上）"),
                _size_of("res_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应体（解压）"),
                _decoded_size("res_wire_size", "res_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "总计"), _size_of("total_size")),
        ),
    ),
)


class ComposeInterface(QWidget):
    """编辑页：自己拼请求发出去，可选不进流量列表。"""

    def __init__(self, controller: ComposeController, parent=None):
        super().__init__(parent)
        self.setObjectName("ComposeInterface")
        self.controller = controller
        self._sending = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # ── 组件 ──────────────────────────────────

    def __init_widget(self):
        # 顶栏。方法下拉不可编辑：prefill 可能带来词表外的方法（PROPFIND 等），
        # 写入统一走 _set_method（缺项动态补条目）。
        self.method_combo = ComboBox(self)
        self.method_combo.addItems(METHODS)
        self.method_combo.setCurrentText("GET")
        self.method_combo.setFixedWidth(110)

        # 二进制体直通：prefill 进非 UTF-8 内容时锁只读，原始字节存这里原样发出。
        self._raw_body: bytes = b""
        self._binary = False
        # 「没动过就原样发」的三处脏标记（断点面板 `_body_dirty` 同一思路）：
        # QTextDocument 会把 \r\n 归一成 \n、合并参数会重编码 query —— 未编辑的
        # 内容必须绕开这两条有损路径原样送达。_loading_form 挡住 prefill 的
        # setText 自己触发的 textChanged（程序化装载不是用户编辑）。
        self._body_dirty = False
        self._url_dirty = False
        self._params_dirty = False
        self._loading_form = False

        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText("https://example.com/api")
        self.url_edit.setClearButtonEnabled(True)

        # 记录是请求选项，独立于 URL；文字与勾选状态一起表达用途和当前选择。
        self.record_btn = CheckBox(self.tr("记录流量"), self)
        self.record_btn.setChecked(True)
        self.record_btn.setToolTip(
            self.tr(
                "进入流量列表；关闭时请求仍会经过代理内核发出（重写/网关/断点规则照常生效），但不会出现在流量列表中"
            )
        )
        self.record_btn.installEventFilter(ToolTipFilter(self.record_btn, 700))

        # 按当前语言的最长状态文案预留宽度，发送中不挤动 URL，也不截断英文。
        self.send_btn = PrimaryPushButton(FluentIcon.SEND, self.tr("发送中…"), self)
        sending_width = self.send_btn.sizeHint().width()
        self.send_btn.setText(self.tr("发送"))
        self.send_btn.setFixedWidth(
            max(96, sending_width, self.send_btn.sizeHint().width())
        )
        self.send_btn.setToolTip(self.tr("发送这条请求"))
        self.send_btn.installEventFilter(ToolTipFilter(self.send_btn, 700))

        # cURL 粘贴导入：读剪贴板灌表单，解析错误就地 show_error（v1 不做
        # 粘贴编辑对话框）。
        self.paste_curl_btn = ToolButton(FluentIcon.PASTE, self)
        self.paste_curl_btn.setToolTip(self.tr("从剪贴板导入 cURL 命令"))
        self.paste_curl_btn.setAccessibleName(self.tr("导入 cURL"))
        self.paste_curl_btn.installEventFilter(ToolTipFilter(self.paste_curl_btn, 700))

        # 左侧：请求详情（参数/请求头/请求体，复用详情面板的可编辑组件）。
        self.request_panel = TabPanel(self)
        self.request_panel.setTabFontSize(12)
        self.request_panel.close_button.hide()

        self.params_card = ItemDualPanel(True, self)
        self.headers_card = HeaderDualPanel(True, self)
        self.body_panel = JsonDualPanel(self)
        self.body_kind_combo = ComboBox(self)
        self.body_kind_combo.addItems([kind for kind, _, _ in BODY_KINDS])
        self.body_kind_combo.setFixedWidth(96)
        self.body_panel.text.tool_layout.addWidget(BodyLabel(self.tr("数据类型"), self))
        self.body_panel.text.tool_layout.addWidget(self.body_kind_combo)

        self.request_panel.addTab("Params", self.params_card, self.tr("参数"))
        self.request_panel.addTab("Headers", self.headers_card, self.tr("请求头"))
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
        self.request_panel.addTab("Body", self.body_box, self.tr("请求体"))
        self._sync_body_kind(0)
        # 请求头(N)：条数挂标签，随编辑实时变（参数变化实时写回 URL，见下）。
        self.headers_card.changed.connect(self._update_header_count)
        self.params_card.changed.connect(self._sync_params_to_url)
        self.params_card.changed.connect(self._on_params_edited)
        self._update_header_count()

        # 右侧：响应区。`with_raw=False`：compose 的结果不需要原始报文兜底，
        # 标签只剩 响应头/响应体，「性能」由本页追加到末位。
        self.response_pane = ResponsePane(self, with_raw=False)

        self.perf_overview = OverviewPane(sections=_PERF_SECTIONS)
        # 瀑布条压在「时间」组卡片头部（滚动区之上）：compose 回放
        # 的连接段嵌在等待里（lane="nested"，虚线框），与详情页时序同一模型。
        self.perf_waterfall = WaterfallBar(self)
        perf_page = QWidget(self)
        perf_layout = QVBoxLayout(perf_page)
        perf_layout.setContentsMargins(0, 0, 0, 0)
        perf_layout.setSpacing(8)
        perf_layout.addWidget(self.perf_waterfall)
        perf_layout.addWidget(self.perf_overview, 1)

        self.response_pane.addTab("Perf", perf_page, self.tr("性能"))

        # 与 FlowEmptyState 同一层级：小图标、标题、说明。等待时只切换图标
        # 和文案，QStackedWidget 管住子控件，避免悬空控件浮到左上角。
        self.empty_hint = QWidget(self)
        self.empty_icon_stack = QStackedWidget(self.empty_hint)
        self.empty_icon_stack.setFixedSize(32, 32)
        self.empty_icon = IconWidget(FluentIcon.SEND, self.empty_icon_stack)
        self.loading_ring = IndeterminateProgressRing(
            self.empty_icon_stack, start=False
        )
        self.loading_ring.setFixedSize(32, 32)
        self.loading_ring.setStrokeWidth(3)
        self.empty_icon_stack.addWidget(self.empty_icon)
        self.empty_icon_stack.addWidget(self.loading_ring)
        self.empty_title = BodyLabel(self.empty_hint)
        self.empty_subtitle = CaptionLabel(self.empty_hint)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setWordWrap(True)
        hint_layout = QVBoxLayout(self.empty_hint)
        hint_layout.setContentsMargins(16, 16, 16, 16)
        hint_layout.addStretch(1)
        hint_layout.addWidget(self.empty_icon_stack, 0, Qt.AlignmentFlag.AlignCenter)
        hint_layout.addSpacing(8)
        hint_layout.addWidget(self.empty_title)
        hint_layout.addWidget(self.empty_subtitle)
        hint_layout.addStretch(1)
        self._set_waiting(False)

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
        top.addWidget(self.record_btn)
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
        self.url_edit.textChanged.connect(self._on_url_edited)
        self.body_panel.changed.connect(self._on_body_edited)
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
            "Headers", self.tr("请求头 ({count})").format(count=count)
        )

    @Slot()
    def _on_url_edited(self) -> None:
        if not self._loading_form:
            self._url_dirty = True

    @Slot()
    def _on_params_edited(self) -> None:
        if not self._loading_form:
            self._params_dirty = True

    @Slot()
    def _on_body_edited(self) -> None:
        self._body_dirty = True

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
        「参数」就是 URL 的查询串，两边各存一份只会互相打脸。

        未编辑保真（issues #78）：URL 栏与参数页都没动过时整串原样返回，不走
        parse_qsl/urlencode 的重编码路径 —— `?q=a%20b&flag` 重排成 `q=a+b&flag=`
        会让签名类参数失效。编辑过才合并（端口规范见 `_merge_query`）。
        """
        pairs = self.params_card.items()
        if not self._url_dirty and not self._params_dirty:
            return self.url_edit.text()
        return self._merge_query(self.url_edit.text().strip(), pairs)

    def _set_method(self, method: str) -> None:
        """写入方法下拉。纯 ComboBox 有两个坑：setText 只改按钮字面、不动选中项，
        发送时读的 currentText() 会留在旧方法上（显示 PROPFIND、按 GET 发出）；
        setCurrentText 又只认词表内项，落空静默不动。所以缺项先补进条目列表
        再选中，补过的条目留在下拉里。
        """
        if self.method_combo.findText(method) < 0:
            self.method_combo.addItem(method)
        self.method_combo.setCurrentText(method)

    def prefill(self, edit: RequestEdit) -> None:
        """把一份请求草稿灌进表单（右键「Edit in Compose」/ cURL 导入）。

        静默覆盖正在编辑的内容：compose 页没有「未保存草稿」概念，加确认框
        成本大于收益。响应区复位——旧结果不属于这张新表单。
        """
        # 程序化装载：setText 的 textChanged 不算用户编辑，脏标记全部归零。
        self._loading_form = True
        try:
            self._set_method(edit.method)
            self.url_edit.setText(edit.url)
            # query 拆进参数页（断点面板 `RequestPanel.load` 同款）：URL 栏留整串，
            # 参数页是权威源，发送时再合并回去。
            parts = urlsplit(edit.url)
            self.params_card.set_items(parse_qsl(parts.query, keep_blank_values=True))
            self.headers_card.set_items(list(edit.headers))
            self._load_body(edit.content)
            self._body_dirty = False
            self._url_dirty = False
            self._params_dirty = False
        finally:
            self._loading_form = False
        self.response_stack.setCurrentWidget(self.empty_hint)
        if not self._sending:
            self._set_waiting(False)

    def _load_body(self, content: bytes | None) -> None:
        """UTF-8 可解码 → 进编辑器；不可解码 → 二进制锁，原样直通发送。"""
        # None 只在类型上可能（`RequestEdit.content` 的「未编辑」值），等价空体。
        content = content or b""
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
            show_error(self.tr("导入失败"), self.tr("剪贴板是空的"), self)
            return
        if not text.lower().startswith(("curl ", "curl.exe")):
            show_error(
                self.tr("导入失败"),
                self.tr("剪贴板里没有 cURL 命令"),
                self,
            )
            return
        try:
            edit = parse_curl(text)
        except ValueError as exc:
            show_error(self.tr("导入失败"), str(exc), self)
            return
        self.prefill(edit)
        show_success(self.tr("成功"), self.tr("已从 cURL 导入请求"), self)

    @Slot()
    def _on_send(self):
        if self._sending:
            return
        method = self.method_combo.currentText().strip()
        url = self._collect_url()
        if not url:
            self._on_send_failed(self.tr("发送失败"), self.tr("URL 为空"))
            return

        self.controller.send(
            method,
            url,
            self._collect_headers(),
            self._body_content(),
            record=self.record_btn.isChecked(),
        )

    def _body_content(self) -> bytes:
        """发送的体字节。未编辑（或改回了原文）一律走 `_raw_body`：QTextDocument
        把 \\r\\n 归一成 \\n，multipart 边界、签名正文里的 CRLF 经不得这条有损路径
        （issues #77）。只有真编辑过的文本体才按 UTF-8 编码发出。"""
        if self._binary or not self._body_dirty:
            return self._raw_body
        text = self.body_panel.plain_text()
        if text.encode("utf-8") == self._raw_body:
            return self._raw_body
        return text.encode("utf-8")

    @Slot(bool)
    def _on_sending_changed(self, sending: bool):
        self._sending = sending
        self.send_btn.setDisabled(sending)
        self.send_btn.setText(self.tr("发送中…") if sending else self.tr("发送"))
        self.send_btn.setIcon(FluentIcon.SYNC if sending else FluentIcon.SEND)
        self._set_waiting(sending)
        if sending:
            self.response_stack.setCurrentWidget(self.empty_hint)

    def _set_waiting(self, waiting: bool) -> None:
        if waiting:
            self.empty_icon_stack.setCurrentWidget(self.loading_ring)
            self.loading_ring.start()
            self.empty_title.setText(self.tr("正在等待响应"))
            self.empty_subtitle.setText(self.tr("收到响应后会显示在这里"))
        else:
            self.loading_ring.stop()
            self.empty_icon_stack.setCurrentWidget(self.empty_icon)
            self.empty_title.setText(self.tr("暂无响应"))
            self.empty_subtitle.setText(self.tr("编辑请求，然后点击「发送」"))

    @Slot(object)
    def _on_result(self, result: ComposeResult):
        self._on_sending_changed(False)
        detail = result.detail
        if result.error:
            show_error(self.tr("请求失败"), result.error, self)
        self.response_stack.setCurrentWidget(self.response_pane)
        self.response_pane.set_data(detail)
        self.perf_overview.set_data(detail)
        self.perf_waterfall.set_model(phases(detail))

    @Slot(str, str)
    def _on_send_failed(self, title: str, content: str):
        self._on_sending_changed(False)
        self.response_stack.setCurrentWidget(self.empty_hint)
        show_error(title, content, self)
