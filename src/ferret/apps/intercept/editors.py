"""断点面板：请求 / 响应两个阶段面板，复用详情面板的可编辑组件。

编辑器只展示**真实报文**，不做美化：`contentviews.prettify_message` 的产出是给人看
的排版，写回去就把 body 改坏了。所以这里只走 `Message.get_content(strict=False)`
（去掉 Content-Encoding 后的原始字节），拿到什么显示什么、显示什么写回什么。

组件与流量详情面板是同一批类：标签容器 `TabPanel`、键值 `ItemDualPanel`（editable
档）、体 `JsonDualPanel` —— 断点这边只是 tab 更少（请求：参数/请求头/请求体；响应：
响应头/响应体）加一条页头行（方法/URL 或状态码 + 放行/丢弃）。参数页是断点新增的
结构化 query 编辑：写回时参数页是 query 的权威源，合并进 URL（与 compose 页
`_collect_url` 的端口规范保持同一套规则）。
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QCoreApplication, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    EditableComboBox,
    FluentIcon,
    LineEdit,
    PrimaryToolButton,
    ToolButton,
)

from ferret.apps.common.edit import ItemDualPanel, JsonDualPanel, Language
from ferret.apps.common.http_methods import METHODS
from ferret.apps.common.panel import TabPanel
from ferret.core.mitm import HTTPFlow, RequestEdit, ResponseEdit
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 只存标记：模块级求值赶在翻译器安装之前，`self.tr(变量)` 也提取不到。
_BINARY_HINT = QT_TRANSLATE_NOOP(
    "MessageEditor",
    "这段内容不是合法的 UTF-8（压缩包、图片，或非 UTF-8 字符集），已锁定为只读；放行时按原样发出。",
)


def _body_lang(text: str) -> Language:
    """只在 JSON 与通用 HTTP 词法器之间二选一。

    编辑器里的 body 是原始文本，没有 contentview 报的 syntax 可用；HTTP 词法器对
    ``Key: Value`` 和散文都不会整片标红，当默认档最稳。
    """
    return Language.JSON if text.lstrip()[:1] in ("{", "[") else Language.HTTP


def _merge_query(url: str, pairs: list[tuple[str, str]]) -> str:
    """参数页为 query 权威源：把键值对合并进 URL 的查询串。

    与 compose 页 `_collect_url` 同一套端口规范：显式写出 `http://…:443` /
    `https://…:80` 几乎必是笔误，还原为 scheme 默认端口；其余显式端口不动。
    参数全空时不覆盖 URL 上手写的查询串。
    """
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
    pairs = [(key, value) for key, value in pairs if key.strip()]
    query = urlencode(pairs) if pairs else parts.query
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class PhasePanel(QWidget):
    """请求/响应两个阶段面板的公共骨架：页头行 + 详情标签。

    页头行右侧固定一对「放行 / 丢弃」图标按钮，只发信号不干活 —— 写回和丢弃都要
    窗口拿着**当前选中的流量**去做，面板自己不认识队列。
    """

    releaseRequested = Signal()
    dropRequested = Signal()
    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 原始体字节。UTF-8 解不开时不让改，放行时原样送回，别把 gzip / 图片改烂。
        self._raw_body: bytes = b""
        self._binary = False

        self.release_button = PrimaryToolButton(FluentIcon.SEND, self)
        self.release_button.setToolTip(
            QCoreApplication.translate("MessageEditor", "写回改动并放行这条流量")
        )
        self.release_button.setAccessibleName(
            QCoreApplication.translate("MessageEditor", "放行")
        )
        self.release_button.clicked.connect(self.releaseRequested)
        self.drop_button = ToolButton(FluentIcon.CANCEL, self)
        self.drop_button.setToolTip(
            QCoreApplication.translate(
                "MessageEditor", "断开这条流量，客户端什么都收不到"
            )
        )
        self.drop_button.setAccessibleName(
            QCoreApplication.translate("MessageEditor", "丢弃")
        )
        self.drop_button.clicked.connect(self.dropRequested)

        self.headers_panel = ItemDualPanel(True, self)
        self.body_panel = JsonDualPanel(self)
        self.body_hint = CaptionLabel(self)
        self.body_hint.setWordWrap(True)
        self.body_hint.hide()

        self.body_box = QWidget(self)
        body_layout = QVBoxLayout(self.body_box)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addWidget(self.body_panel, 1)
        body_layout.addWidget(self.body_hint)

        self.headers_panel.changed.connect(self.changed)
        self.body_panel.changed.connect(self.changed)

    # —— 骨架 ——

    def _build_header(self, *leading: QWidget, stretch_last: bool = False) -> QWidget:
        """页头行：调用方的标量控件 + 弹簧 + 放行/丢弃。

        `stretch_last=True` 时最后一个标量控件吃掉剩余宽度（请求页的 URL 框）。
        """
        header = QWidget(self)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        for widget in leading:
            layout.addWidget(widget)
        if stretch_last and leading:
            layout.setStretch(len(leading) - 1, 1)
        layout.addStretch(1)
        layout.addWidget(self.release_button)
        layout.addWidget(self.drop_button)
        return header

    def _build_detail(self) -> TabPanel:
        """详情标签。TabPanel 自带的关闭按钮在这里没有意义，藏掉。"""
        detail = TabPanel(self)
        detail.setTabFontSize(12)
        detail.close_button.hide()
        return detail

    # —— 报文装载（详情侧：头 + 体，请求/响应通用） ——

    def _load_message(self, message) -> None:
        """把一条 mitmproxy 报文灌进头面板与体面板。

        头用 `items(multi=True)` 读：同名头（Cookie / Set-Cookie）在字典里会被吃掉
        一条，而它们恰好是断点最常改的东西。
        """
        headers = list(message.headers.items(multi=True))
        self.headers_panel.set_items(headers)
        self._update_header_count(len(headers))

        self._raw_body = message.get_content(strict=False) or b""
        try:
            text = self._raw_body.decode("utf-8")
        except UnicodeDecodeError:
            # 严格解码故意不加兜底：能显示的一定能一字节不差地写回去。
            self._binary = True
            text = ""
        else:
            self._binary = False
        self.body_panel.set_text(text, _body_lang(text))
        self.body_hint.setText(
            QCoreApplication.translate("MessageEditor", _BINARY_HINT)
        )
        self.body_hint.setVisible(self._binary)
        self.body_panel.set_read_only(self._binary)

    def _body_bytes(self) -> bytes:
        """写回用的体字节。二进制体原样返回；文本体的编码与显示时严格互逆。"""
        if self._binary:
            return self._raw_body
        return self.body_panel.plain_text().encode("utf-8")

    def _update_header_count(self, count: int) -> None:
        """`请求头(N)` / `响应头(N)`：条数直接挂在标签上，与流量详情页同一语言。

        抽象钩子：请求侧与响应侧的「头」译法不同，词条与翻译都在子类
        （context 也必须各自写成字面量，见 AGENTS.md §7）。
        """
        raise NotImplementedError

    def _clear_message(self) -> None:
        self._raw_body = b""
        self._binary = False
        self.headers_panel.set_items([])
        self.body_panel.set_text("")
        self.body_hint.hide()


class RequestPanel(PhasePanel):
    """请求阶段面板：方法 + URL + 放行/丢弃；详情：参数 / 请求头 / 请求体。"""

    def _update_header_count(self, count: int) -> None:
        self.detail.setTabText(
            "Headers", self.tr("请求头 ({count})").format(count=count)
        )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.method_combo = EditableComboBox(self)
        self.method_combo.addItems(METHODS)
        self.method_combo.setCurrentText("GET")
        self.method_combo.setFixedWidth(110)
        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText("https://api.example.com/v1/user")
        self.url_edit.setClearButtonEnabled(True)
        self.method_combo.currentTextChanged.connect(self.changed)
        self.url_edit.textChanged.connect(self.changed)

        self.params_panel = ItemDualPanel(True, self)
        self.params_panel.changed.connect(self.changed)

        header = self._build_header(self.method_combo, self.url_edit, stretch_last=True)

        self.detail = self._build_detail()
        self.detail.addTab("Params", self.params_panel, self.tr("参数"))
        self.detail.addTab("Headers", self.headers_panel, self.tr("请求头"))
        self.detail.addTab("Body", self.body_box, self.tr("请求体"))
        self.detail.setCurrentTab("Params")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(header)
        layout.addWidget(self.detail, 1)

    def load(self, flow: HTTPFlow) -> None:
        """URL 整串进地址栏，query 同时解析进参数页 —— 写回时参数页说了算。"""
        request = flow.request
        # setText 而不是 setCurrentText：后者只认词表内项（findText 落空就静默
        # 不动），词表外方法（PROPFIND 等）会显示成上一条的旧值并照样写回。
        self.method_combo.setText(request.method)
        self.url_edit.setText(request.url)
        parts = urlsplit(request.url)
        self.params_panel.set_items(parse_qsl(parts.query, keep_blank_values=True))
        self._load_message(request)

    def edit(self) -> RequestEdit:
        """`apply_request_edit` 那边会校验方法非空、URL 可解析，这里不抢着判。"""
        return RequestEdit(
            method=self.method_combo.currentText().strip(),
            url=self._merge_url(),
            headers=self.headers_panel.items(),
            content=self._body_bytes(),
        )

    def _merge_url(self) -> str:
        return _merge_query(self.url_edit.text().strip(), self.params_panel.items())

    def clear(self) -> None:
        self.method_combo.setCurrentText("GET")
        self.url_edit.clear()
        self.params_panel.set_items([])
        self._clear_message()


class ResponsePanel(PhasePanel):
    """响应阶段面板：状态码 + 放行/丢弃；详情：响应头 / 响应体。

    原因短语没有输入位：留空走 `apply_response_edit` 的默认短语（状态码的标准
    reason），断点场景几乎没人手写 reason。
    """

    def _update_header_count(self, count: int) -> None:
        self.detail.setTabText(
            "Headers", self.tr("响应头 ({count})").format(count=count)
        )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.code_edit = LineEdit(self)
        self.code_edit.setPlaceholderText("200")
        self.code_edit.setFixedWidth(110)
        self.code_edit.setClearButtonEnabled(True)
        self.code_edit.textChanged.connect(self.changed)

        header = self._build_header(self.code_edit)

        self.detail = self._build_detail()
        self.detail.addTab("Headers", self.headers_panel, self.tr("响应头"))
        self.detail.addTab("Body", self.body_box, self.tr("响应体"))
        self.detail.setCurrentTab("Headers")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(header)
        layout.addWidget(self.detail, 1)

    def load(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None:  # 窗口保证只在响应期装载；防御一下罢了。
            self.clear()
            return
        self.code_edit.setText(str(response.status_code))
        self._load_message(response)

    def edit(self) -> ResponseEdit:
        """状态码交给 `apply_response_edit` 校验（须在 100-599）。

        这里只把「不是数字」折成 0：让内核那边统一报「状态码必须是……」，
        免得同一件事两处措辞不一样。
        """
        text = self.code_edit.text().strip()
        return ResponseEdit(
            status_code=int(text) if text.isdigit() else 0,
            headers=self.headers_panel.items(),
            content=self._body_bytes(),
        )

    def clear(self) -> None:
        self.code_edit.clear()
        self._clear_message()
