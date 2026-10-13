"""Typed editors for held requests/responses; unchanged bodies are patches of None.

Panels only read snapshots. Preparing an edit freezes every field before a worker
reads files; the window owns the target guard and the eventual atomic release.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import QCoreApplication, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    EditableComboBox,
    FluentIcon,
    LineEdit,
    PrimaryToolButton,
    ToolButton,
)

from ferret.apps.common.edit import ItemDualPanel
from ferret.apps.common.edit.body import (
    BodyDraft,
    BodyEditor,
    BodyMode,
    BodyProfile,
)
from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.common.http_methods import METHODS
from ferret.apps.common.panel import TabPanel
from ferret.core.mitm import (
    HeldEditGuard,
    HTTPFlow,
    RequestEdit,
    ResponseEdit,
    held_edit_body,
    held_edit_guard,
    inspect_edit_body,
    response_body_allowed,
)


@dataclass(frozen=True)
class EditDraft:
    """Immutable input for FunctionTask; contains neither widgets nor live flows."""

    edit: RequestEdit | ResponseEdit
    body: BodyDraft | None

    @property
    def needs_worker(self) -> bool:
        return self.body is not None and self.body.needs_worker

    def build(self) -> RequestEdit | ResponseEdit:
        return replace(self.edit, content=self.body.build() if self.body else None)


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
        # 从 netloc 尾部剥掉端口段，而不是拿 hostname 重建 netloc ——
        # hostname 不含 userinfo，重建会把 URL 内嵌的 user:pass@ 一并丢掉。
        host, sep, port_part = parts.netloc.rpartition(":")
        if sep and port_part.isdigit() and int(port_part) == port:
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

    # 子类各自装配：详情标签与准备期间要禁用的标量控件。
    detail: TabPanel
    _scalar_widgets: tuple[QWidget, ...]

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        profile: BodyProfile = BodyProfile.REQUEST,
    ) -> None:
        super().__init__(parent)
        self.guard: HeldEditGuard | None = None

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

        self.headers_panel = HeaderDualPanel(True, self)
        self.body_panel = BodyEditor(self.headers_panel, profile=profile, parent=self)
        self.body_box = self.body_panel
        self.headers_panel.changed.connect(self._on_headers_edited)

    def _on_headers_edited(self) -> None:
        self._update_header_count(len(self.headers_panel.items()))

    def set_preparing(self, preparing: bool) -> None:
        """Keep drop available while the captured edit is being prepared."""
        self.detail.setEnabled(not preparing)
        self.release_button.setEnabled(not preparing)
        for widget in self._scalar_widgets:
            widget.setEnabled(not preparing)

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

    def _load_message(self, message, flow: HTTPFlow) -> None:
        """把一条 mitmproxy 报文灌进头面板与体面板。

        头用 `items(multi=True)` 读：同名头（Cookie / Set-Cookie）在字典里会被吃掉
        一条，而它们恰好是断点最常改的东西。
        """
        headers = list(message.headers.items(multi=True))
        self.headers_panel.set_items(headers)
        self._update_header_count(len(headers))

        # Facade prepares this on the mitm thread. The fallback serves callers
        # loading detached snapshots directly (including editor tests).
        source = held_edit_body(flow) or inspect_edit_body(message)
        self.body_panel.load(
            source.content,
            headers,
            available=source.available,
            decode_ok=source.decode_ok,
            strict_charset=True,
        )

    def _update_header_count(self, count: int) -> None:
        """`请求头(N)` / `响应头(N)`：条数直接挂在标签上，与流量详情页同一语言。

        抽象钩子：请求侧与响应侧的「头」译法不同，词条与翻译都在子类
        （context 也必须各自写成字面量，见 AGENTS.md §7）。
        """
        raise NotImplementedError

    def _clear_message(self) -> None:
        self.guard = None
        self.headers_panel.set_items([])
        self.body_panel.load(b"", [], strict_charset=True)
        self.body_panel.set_body_allowed(True)


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
        self._scalar_widgets = (self.method_combo, self.url_edit)

        self.params_panel = ItemDualPanel(True, self)

        # URL 保真（issues #78）：URL 栏与参数页都没动过时，写回整串原样返回，
        # 不走 parse_qsl/urlencode 的重编码路径。textChanged 对程序化 setText
        # 也发，所以装载后再把标记归零（见 load）。
        self._url_dirty = False
        self._params_dirty = False
        self.url_edit.textChanged.connect(self._on_url_edited)
        self.params_panel.changed.connect(self._on_params_edited)

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

    @Slot()
    def _on_url_edited(self) -> None:
        self._url_dirty = True

    @Slot()
    def _on_params_edited(self) -> None:
        self._params_dirty = True

    def load(self, flow: HTTPFlow) -> None:
        """URL 整串进地址栏，query 同时解析进参数页 —— 编辑过才由参数页说了算。"""
        request = flow.request
        self.guard = held_edit_guard(flow)
        # setText 而不是 setCurrentText：后者只认词表内项（findText 落空就静默
        # 不动），词表外方法（PROPFIND 等）会显示成上一条的旧值并照样写回。
        self.method_combo.setText(request.method)
        self.url_edit.setText(request.url)
        parts = urlsplit(request.url)
        self.params_panel.set_items(parse_qsl(parts.query, keep_blank_values=True))
        self._load_message(request, flow)
        # 程序化装载不是用户编辑：setText 的 textChanged 同步发完了，这里归零。
        self._url_dirty = False
        self._params_dirty = False

    def prepare(self) -> EditDraft:
        body = self.body_panel.snapshot(mode=BodyMode.PATCH)
        return EditDraft(
            RequestEdit(
                method=self.method_combo.currentText().strip(),
                url=self._merge_url(),
                headers=body.headers,
                content=None,
            ),
            body.body,
        )

    def edit(self) -> RequestEdit:
        edit = self.prepare().build()
        assert isinstance(edit, RequestEdit)
        return edit

    def _merge_url(self) -> str:
        """参数页为 query 权威源；URL 与参数都没动过时整串原样返回（issues #78）：
        `?q=a%20b&flag` 经 parse_qsl/urlencode 会重排成 `q=a+b&flag=`，签名类
        参数就废了 —— 没改过的东西不该被「规范化」。"""
        # Reading commits the active delegate before checking the dirty flag.
        pairs = self.params_panel.items()
        if not self._url_dirty and not self._params_dirty:
            return self.url_edit.text()
        return _merge_query(self.url_edit.text().strip(), pairs)

    def clear(self) -> None:
        self.method_combo.setCurrentText("GET")
        self.url_edit.clear()
        self.params_panel.set_items([])
        self._clear_message()
        self._url_dirty = False
        self._params_dirty = False


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
        super().__init__(parent, profile=BodyProfile.RESPONSE)
        self._request_method = "GET"
        self._loading = False
        self.code_edit = LineEdit(self)
        self.code_edit.setPlaceholderText("200")
        self.code_edit.setFixedWidth(110)
        self.code_edit.setClearButtonEnabled(True)
        self._scalar_widgets = (self.code_edit,)
        self.code_edit.textChanged.connect(self._sync_body_permission)

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
        self.guard = held_edit_guard(flow)
        self._request_method = flow.request.method
        self._loading = True
        self.code_edit.setText(str(response.status_code))
        self._load_message(response, flow)
        self._loading = False
        self._sync_body_permission()

    def _sync_body_permission(self) -> None:
        if self._loading:
            return
        status = self._status_code()
        if not 100 <= status <= 599:
            return
        allowed = response_body_allowed(self._request_method, status)
        self.body_panel.set_body_allowed(
            allowed,
            self.tr("此请求方法或响应状态不允许响应体；放行时不发送正文。"),
        )

    def _status_code(self) -> int:
        """状态码交给 `apply_response_edit` 校验（须在 100-599）。

        这里只把「不是数字」折成 0：让内核那边统一报「状态码必须是……」，
        免得同一件事两处措辞不一样。判定用 isdecimal —— 它与 int() 接受的
        字符集一致；isdigit 对上标（"²"）也 True 而 int() 抛 ValueError，
        异常会从放行槽逃逸，流量停在断点上界面毫无反应。
        """
        text = self.code_edit.text().strip()
        return int(text) if text.isdecimal() else 0

    def prepare(self) -> EditDraft:
        body = self.body_panel.snapshot(mode=BodyMode.PATCH)
        return EditDraft(
            ResponseEdit(
                status_code=self._status_code(),
                headers=body.headers,
                content=None,
            ),
            body.body,
        )

    def edit(self) -> ResponseEdit:
        edit = self.prepare().build()
        assert isinstance(edit, ResponseEdit)
        return edit

    def clear(self) -> None:
        self._request_method = "GET"
        self.code_edit.clear()
        self._clear_message()
