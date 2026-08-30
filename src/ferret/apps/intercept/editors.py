"""Editable request/response panels for a flow held at a breakpoint.

编辑器只展示**真实报文**，不做美化：`contentviews.prettify_message` 的产出是给人看
的排版，写回去就把 body 改坏了。所以这里只走 `Message.get_content(strict=False)`
（去掉 Content-Encoding 后的原始字节），拿到什么显示什么、显示什么写回什么。
"""

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, LineEdit

from ferret.apps.common.edit import ItemDualPanel, Language, ToolPlainTextEdit
from ferret.apps.common.splitter import BaseSplitter
from ferret.core.mitm import HTTPFlow, RequestEdit, ResponseEdit
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 只存标记：模块级求值赶在翻译器安装之前，`self.tr(变量)` 也提取不到。
_BINARY_HINT = QT_TRANSLATE_NOOP(
    "MessageEditor",
    "This content is not valid UTF-8 (an archive, an image, or a non-UTF-8 charset), so it is locked read-only; it goes out unchanged when released.",
)


def _body_lang(text: str) -> Language:
    """只在 JSON 与通用 HTTP 词法器之间二选一。

    编辑器里的 body 是原始文本，没有 contentview 报的 syntax 可用；HTTP 词法器对
    ``Key: Value`` 和散文都不会整片标红，当默认档最稳。
    """
    return Language.JSON if text.lstrip()[:1] in ("{", "[") else Language.HTTP


class MessageEditor(QWidget):
    """请求/响应编辑器的公共部分：头面板 + 体编辑器 + 二进制体的只读兜底。"""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # 原始体字节。UTF-8 解不开时不让改，放行时原样送回，别把 gzip / 图片改烂。
        self._raw_body: bytes = b""
        self._binary = False
        self._read_only = False

        self.form = QFormLayout()
        self.form.setSpacing(8)
        self.form.setContentsMargins(0, 0, 0, 0)

        self.headers_panel = ItemDualPanel(True, self)
        self.body_edit = ToolPlainTextEdit(self)
        self.body_hint = CaptionLabel(self)
        self.body_hint.setWordWrap(True)
        self.body_hint.hide()

        body_box = QWidget(self)
        body_layout = QVBoxLayout(body_box)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        body_layout.addWidget(self.body_edit, 1)
        body_layout.addWidget(self.body_hint)

        self.splitter = BaseSplitter(Qt.Orientation.Vertical, self)
        self.splitter.addWidget(self.headers_panel)
        self.splitter.addWidget(body_box)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        layout.addLayout(self.form)
        layout.addWidget(self.splitter, 1)

        self.headers_panel.changed.connect(self.changed)
        self.body_edit.changed.connect(self.changed)

    def _add_row(self, label: str, editor: LineEdit) -> None:
        editor.setClearButtonEnabled(True)
        editor.textChanged.connect(self.changed)
        self.form.addRow(BodyLabel(label, self), editor)

    def _load_message(self, message) -> None:
        """把一条 mitmproxy 报文灌进头面板与体编辑器。

        头用 `items(multi=True)` 读：同名头（Cookie / Set-Cookie）在字典里会被吃掉
        一条，而它们恰好是断点最常改的东西。
        """
        self.headers_panel.set_items(list(message.headers.items(multi=True)))
        self._raw_body = message.get_content(strict=False) or b""
        try:
            text = self._raw_body.decode("utf-8")
        except UnicodeDecodeError:
            # 严格解码故意不加兜底：能显示的一定能一字节不差地写回去。
            self._binary = True
            text = ""
        else:
            self._binary = False
        self.body_edit.set_text(text, _body_lang(text))
        self.body_hint.setText(
            QCoreApplication.translate("MessageEditor", _BINARY_HINT)
        )
        self.body_hint.setVisible(self._binary)
        self._sync_body_read_only()

    def _body_bytes(self) -> bytes:
        """写回用的体字节。二进制体原样返回；文本体的编码与显示时严格互逆。"""
        if self._binary:
            return self._raw_body
        return self.body_edit.text().encode("utf-8")

    def _sync_body_read_only(self) -> None:
        self.body_edit.set_read_only(self._read_only or self._binary)

    def set_read_only(self, read_only: bool) -> None:
        """整块锁定。没有选中流量、或流量已经放行时用。"""
        self._read_only = read_only
        self.headers_panel.set_read_only(read_only)
        for i in range(self.form.rowCount()):
            item = self.form.itemAt(i, QFormLayout.ItemRole.FieldRole)
            editor = item.widget() if item is not None else None
            if isinstance(editor, LineEdit):
                editor.setReadOnly(read_only)
        self._sync_body_read_only()

    def clear(self) -> None:
        self._raw_body = b""
        self._binary = False
        self.headers_panel.set_items([])
        self.body_edit.set_text("")
        self.body_hint.hide()


class RequestEditor(MessageEditor):
    """请求编辑器：方法 + URL + 头 + 体。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.method_edit = LineEdit(self)
        self.method_edit.setPlaceholderText("GET")
        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText("https://api.example.com/v1/user")
        self._add_row(self.tr("Method"), self.method_edit)
        self._add_row(self.tr("URL"), self.url_edit)

    def load(self, flow: HTTPFlow) -> None:
        self.method_edit.setText(flow.request.method)
        self.url_edit.setText(flow.request.url)
        self._load_message(flow.request)

    def edit(self) -> RequestEdit:
        """`apply_request_edit` 那边会校验方法非空、URL 可解析，这里不抢着判。"""
        return RequestEdit(
            method=self.method_edit.text().strip(),
            url=self.url_edit.text().strip(),
            headers=self.headers_panel.items(),
            content=self._body_bytes(),
        )

    def clear(self) -> None:
        self.method_edit.clear()
        self.url_edit.clear()
        super().clear()


class ResponseEditor(MessageEditor):
    """响应编辑器：状态码 + 原因短语 + 头 + 体。

    请求期断点（还没有响应）没有内容可编：窗口的「响应」标签那边显示的是占位提示，
    这里只把编辑器清空占着。伪造响应（填好直接回给客户端、不发往服务器）已从窗口
    UI 撤掉，入口留在内核 `MitmFacade.fake_response` 供别处使用。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.status_edit = LineEdit(self)
        self.status_edit.setPlaceholderText("200")
        self.reason_edit = LineEdit(self)
        self.reason_edit.setPlaceholderText(
            self.tr("Empty = standard phrase for the status code")
        )
        self._add_row(self.tr("Status code"), self.status_edit)
        self._add_row(self.tr("Reason phrase"), self.reason_edit)

    def load(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None:
            # 请求期：响应还不存在，清空占位（窗口那边显示的是占位提示页）。
            self.clear()
            return
        self.status_edit.setText(str(response.status_code))
        self.reason_edit.setText(response.reason or "")
        self._load_message(response)

    def edit(self) -> ResponseEdit:
        """状态码交给 `apply_response_edit` / `fake_response` 校验（须在 100-599）。

        这里只把「不是数字」折成 0：让内核那边统一报「状态码必须是……」，
        免得同一件事两处措辞不一样。
        """
        text = self.status_edit.text().strip()
        return ResponseEdit(
            status_code=int(text) if text.isdigit() else 0,
            headers=self.headers_panel.items(),
            content=self._body_bytes(),
            reason=self.reason_edit.text().strip(),
        )

    def clear(self) -> None:
        self.status_edit.clear()
        self.reason_edit.clear()
        super().clear()
