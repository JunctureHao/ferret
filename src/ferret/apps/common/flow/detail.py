"""详情面板：一层导航 + 卡片式全字段展示。

改造前是「请求面板 | 响应面板」左右两块，各自一排 Pivot 标签，中间还夹一个内层
分割器。两排标签加起来十条，窄面板下横向挤成一团；「概览」偏偏只挂在请求那半边 ——
一条流量的整体信息（状态、时序、连接、证书）却要从「请求」里找。而且外层分割器
每次开合都要把内层重算成 50:50，两层分割器互相牵扯。

现在导航只有一层：概览 / 请求 / 响应 / 消息 / 原始状态，请求与响应各自的细分退到
页内的次级 Pivot。内层分割器整个消失，那套 50:50 重算跟着删掉。

「消息」页只对 WebSocket 与 SSE 流量出现（见 `messages.py`），其余一律整页隐藏 ——
普通请求点进去只会看到一张空表。

「原始状态」页是**完整性兜底**：概览只显示 `fields.SECTIONS` 列出来的字段，
而这一页把 `Flow.get_state()` 整棵树原样摆出来 —— 以后 mitmproxy 加了新字段、
或者某个字段我们没想到要显示，在这里都还找得到。

这个模块只搬「详情」那一半；表格、外层分割器、空状态仍留在 `views.py`，而
`views.py` 继续 re-export `FlowDataPanel`，两个挂载点的 import 不受影响。
"""

import json
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CommandBar,
    FluentIcon,
    InfoBadge,
    InfoBadgePosition,
    InfoLevel,
    SegmentedWidget,
    SimpleCardWidget,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    TreeWidget,
)

from ferret.apps.common.dialog import CommentDialog
from ferret.apps.common.edit import (
    ItemDualPanel,
    JsonDualPanel,
    Language,
    ToolPlainTextEdit,
)
from ferret.apps.common.flow.fields import OverviewPane
from ferret.apps.common.flow.messages import (
    MessagesPane,
    is_websocket,
    looks_like_json,
)
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.icon import BaseAction, BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.apps.common.panel import TabPanel
from ferret.core.log import get_logger
from ferret.core.mitm import MARKER_DEFAULT, WsClose, WsFrame, human

log = get_logger("flow.detail")

# `bytes` 值在「原始状态」页的预览长度。够看出协议头是什么，又不会把整份 JSON
# 撑成几 MB —— 完整报文在请求/响应两页的 Body 与 Raw 里本来就有。
_STATE_BYTES_PREVIEW = 64

# 状态码档位 → `InfoBadge` 的语义等级。
# 改造前这里是一张五个十六进制色值的表加一句内联样式表，主题一换就对不上（那五个
# 色值是照亮色主题挑的），而且和 Fluent 自己的语义色系统各说各话。`InfoLevel`
# 这五档正好一一对应：3xx 用 `ATTENTION`（主题色），未完成/不认识的用
# `INFOAMTION`（中性灰）。
_STATUS_LEVELS: tuple[tuple[int, InfoLevel], ...] = (
    (200, InfoLevel.SUCCESS),
    (300, InfoLevel.ATTENTION),
    (400, InfoLevel.WARNING),
    (500, InfoLevel.ERROR),
)


def _body_lang(syntax: str, text: str) -> Language:
    """contentview 声明的高亮语言 → ferret 词法器。

    mitmproxy 侧取值有 css / javascript / xml / yaml / none / error 六种，
    ferret 只有 http / json / xml 三套词法器，按最近的一档落位：

    - JSON 视图输出的是真 JSON（mitmproxy 把它归到 yaml），单独走 json 词法器，
      顺带喂 ``JsonDualPanel`` 的树面板。`{` / `[` 那一下嗅探借 `messages.py` 的
      `looks_like_json` —— 消息页给帧和事件挑词法器时问的是同一个问题，两处各写
      一遍迟早只改一边；
    - xml（XML/HTML、WBXML）走 xml；
    - 其余（yaml 的 ``key: value``、css、javascript、none、error）走 http，
      HTTP 词法器对 ``Key: Value`` 行有原生分支，不会整片标红。
    """
    if syntax == "yaml" and looks_like_json(text):
        return Language.JSON
    if syntax == "xml":
        return Language.XML
    return Language.HTTP


def status_level(status: str) -> InfoLevel:
    """状态码文案 → `InfoBadge` 语义等级。

    参数是**字符串**而不是 int：详情字典里这一格可能是 ``"Error"``、可能是
    ``"Pending..."``（未完成的流量），也可能是真的状态码。
    """
    if status == "Error":
        return InfoLevel.ERROR
    if not status.isdigit():
        return InfoLevel.INFOAMTION
    code = int(status)
    level = InfoLevel.INFOAMTION
    for floor, candidate in _STATUS_LEVELS:
        if code >= floor:
            level = candidate
    return level


def _state_key(key: object) -> str:
    """字典键 → JSON 能用的字符串。

    JSON 的键只能是字符串，而原生状态里有 `bytes` 键。不能直接 `str()`：
    那会把 ``b"host"`` 写成字面的 ``b'host'``，引号和前缀一并进 JSON。
    """
    if isinstance(key, (bytes, bytearray)):
        return bytes(key).decode("utf-8", errors="replace")
    return str(key)


def _encode_state(value: object) -> Any:
    """原生状态子树 → 可 JSON 序列化的等价结构。**保留嵌套，不打平。**

    `Flow.get_state()` 里混着 `bytes`（body、ALPN、证书 DER）、`tuple`（地址对）
    以及 mitmproxy 自己的对象，`json.dumps` 撞上任何一个就整份抛 `TypeError` ——
    而这一页的意义正是「面板漏了什么这里都还在」，一个字段拖垮整页就白搭了。
    所以未知类型统一降级成 ``{"__type__": …, "repr": …}``，绝不放过整棵树。

    `bytes` 不原样转码：一个 body 可能几 MB，而这里要回答的是「这个字段大概装了
    什么」。前 64 字节给两份 —— 十六进制和 hexdump 那样的可打印列，够认出协议头。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        head = bytes(value[:_STATE_BYTES_PREVIEW])
        return {
            "__type__": "bytes",
            "size": len(value),
            "hex": head.hex(" "),
            # 非可打印字节一律 "."，和 hexdump 的 ASCII 列一个规矩 —— 比
            # `decode(errors="replace")` 满屏 U+FFFD 好认，也不用猜编码。
            "text": "".join(chr(b) if 32 <= b < 127 else "." for b in head),
        }
    if isinstance(value, dict):
        return {_state_key(key): _encode_state(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_encode_state(item) for item in value]
    return {"__type__": type(value).__name__, "repr": repr(value)}


def state_json(raw_state: object) -> str:
    """原生状态 → 缩进过的 JSON 文本。

    不排序键：`get_state()` 的顺序是 mitmproxy 自己的字段声明顺序，比字母序更好读。
    ``ensure_ascii=False`` 保留中文 —— 备注字段就在里面。
    """
    if not raw_state:
        return ""
    try:
        return json.dumps(_encode_state(raw_state), indent=2, ensure_ascii=False)
    except (TypeError, ValueError) as e:  # pragma: no cover - 兜底的兜底
        log.warning("failed to serialize the native flow state: %s", e)
        return repr(raw_state)


class CookieWidget(QWidget):
    """Cookie 显示组件 - 以 TreeWidget 显示键值对，支持复制"""

    def __init__(self, parent=None):
        """初始化 Cookie 组件

        Args:
            parent: 父组件
        """
        super().__init__(parent)
        self.cookies = {}
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.tree = TreeWidget()
        self.tree.setHeaderLabels(["Name", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(0)
        self.tree.header().setVisible(False)

        self.copy_button = TransparentToolButton(self)
        self.copy_button.setIcon(FluentIcon.COPY)
        self.copy_button.setToolTip(self.tr("Copy cookies"))
        self.copy_button.installEventFilter(
            ToolTipFilter(self.copy_button, 1000, ToolTipPosition.TOP)
        )

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.addWidget(self.copy_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addWidget(self.tree, 1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.copy_button.clicked.connect(self.__on_copy)

    def set_cookies(self, cookies: dict | list[dict]):
        """设置 cookie 数据 {name: value, ...}

        Args:
            cookies: Cookie 字典
        """
        if isinstance(cookies, list):
            normalized = {
                str(item.get("name", "")): str(item.get("value", ""))
                for item in cookies
                if item.get("name")
            }
        else:
            normalized = cookies
        self.cookies = normalized
        self.tree.clear()

        for key, value in normalized.items():
            item = QTreeWidgetItem(self.tree)
            item.setText(0, str(key))
            item.setText(1, str(value))
            item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignLeft)

        self.tree.setColumnWidth(0, 150)
        self.tree.header().setStretchLastSection(True)

    @Slot()
    def __on_copy(self):
        """复制 Cookie 到剪贴板"""
        if not self.cookies:
            show_warning(
                self.tr("Notice"), self.tr("No cookies to copy"), self.window()
            )
            return

        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        QApplication.clipboard().setText(cookie_str)
        show_success(
            self.tr("Success"), self.tr("Cookies copied to clipboard"), self.window()
        )


class MessagePane(TabPanel):
    """请求页与响应页的共同部分：Headers / Cookies / Body / Trailers / Raw。

    改造前 `RequestPanel` 和 `ResponsePanel` 各自抄了一份 `_fill_raw` /
    `_fill_body` / `_set_body_tab_label`，逐字几乎相同 —— 改一处就必须记得改另一处
    （实际上没记住：响应那侧的 controller 调用包了 try/except，请求那侧没包）。
    两页真正的差别只有三处，做成三个钩子：详情字典的键前缀、controller 取原始报文
    的方法、以及 Raw 的首行是请求行还是状态行。

    「空的标签页不出现」也收在这里。Query / Form / Cookies / Trailers 四页缺内容
    时整条标签隐藏，而不是点进去看见一片空白 —— 这几页多数流量都用不上。
    Headers / Body / Raw 三页始终在：它们缺内容本身就是要看的信息。
    """

    # 详情字典的键前缀：``Request`` 或 ``Response``。
    PREFIX = ""

    # 空内容时隐藏的标签页 route key，子类各自声明。
    OPTIONAL_TABS: tuple[str, ...] = ()

    def __init__(self, parent=None, controller=None):
        super().__init__(parent)
        self.datas: dict | None = None
        self.controller = controller  # 保存 controller 引用

        self._init_widget()
        self._init_layout()
        self.setTabFontSize(12)
        # 徽标插在弹簧之后、关闭按钮之前，所以永远贴着右侧动作区。
        self.tab_layout.insertWidget(2, self.body_view_badge)

    # —— 子类给出的三处差异 ——

    def _raw_from_controller(self, controller, flow_id: str) -> bytes | str | None:
        """从 controller 取线上原始报文；子类各自指向请求或响应那一路。

        controller 由 `_fill_raw` 校完非空再传进来，钩子里不必再判一次。
        """
        raise NotImplementedError

    def _raw_start_line(self) -> str:
        """Raw 页手工拼装时的首行：请求行或状态行。"""
        raise NotImplementedError

    # —— 组件 ——

    def _init_widget(self):
        """初始化界面组件

        注意：所有通过 addTab 加入 stacked 的子组件，不要传 parent=self，
        否则 addTab 内部 QStackedWidget.addWidget() 会触发二次 reparenting，
        导致内部工具栏/行号区几何偏移（左上角错位）。
        """
        self.header_card = ItemDualPanel()
        self.header_card.set_read_only(True)

        self.body_card = JsonDualPanel()
        self.body_card.set_read_only(True)

        self.trailers_widget = ItemDualPanel()
        self.trailers_widget.set_read_only(True)

        self.raw_edit = ToolPlainTextEdit()
        self.raw_edit.set_read_only(True)

        self.cookie_widget = CookieWidget()
        self.cookie_card = SimpleCardWidget()
        self.cookie_card.setBorderRadius(0)
        cookie_layout = QVBoxLayout(self.cookie_card)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.addWidget(self.cookie_widget)

        # contentview 的视图名（JSON / gRPC / Multipart Form …）。改造前是把它拼进
        # Body 标签的文字里再 `adjustSize()`，标签宽度跟着每条流量跳；挪成右侧一枚
        # 徽标之后标签栏宽度稳定，视图名也不再和标签文案抢位置。
        self.body_view_badge = InfoBadge(self)
        self.body_view_badge.setLevel(InfoLevel.INFOAMTION)
        self.body_view_badge.hide()

    def _init_layout(self):
        """子类按自己的顺序 addTab。"""
        raise NotImplementedError

    # —— 数据 ——

    def set_data(self, data: dict):
        """填充数据

        Args:
            data: 数据字典（已由 mitmproxy 阶段预解析结构化字段）
        """
        self.datas = data
        prefix = self.PREFIX

        self.header_card.set_items(data.get(f"{prefix} Headers", {}))

        cookies = data.get(f"{prefix} Cookies", {})
        self.cookie_widget.set_cookies(cookies)

        trailers = data.get(f"{prefix} Trailers", {})
        self.trailers_widget.set_items(trailers)

        self._set_body_view(data.get(f"{prefix} Body View", ""))
        self._fill_body(data)
        self._fill_raw(data.get(f"{prefix} Body", b""), data.get("Flow ID", ""))

        self.setTabVisible("Cookies", bool(cookies))
        self.setTabVisible("Trailers", bool(trailers))

    def _fill_body(self, data: dict):
        """填充报文体（消费 mitmproxy 阶段预解析字段，不再重复解码/格式化）

        Args:
            data: 完整数据字典，含 ``… Body Pretty`` / ``… Body Text``
        """
        text = data.get(f"{self.PREFIX} Body Pretty")
        if text is None:
            text = data.get(f"{self.PREFIX} Body Text") or ""
        lang = _body_lang(data.get(f"{self.PREFIX} Body Syntax", "none"), text)
        self.body_card.set_text(text, lang=lang)

    def _fill_raw(self, body, flow_id: str = ""):
        """线上原始报文；controller 给不出来就按详情字典手工拼一份

        Args:
            body: 报文体
            flow_id: 流 ID
        """
        if self.controller and flow_id:
            raw_data = None
            try:
                raw_data = self._raw_from_controller(self.controller, flow_id)
            except (AttributeError, ValueError, TypeError, RuntimeError) as e:
                log.warning("failed to read the raw HTTP payload: %s", e)
            if raw_data:
                if isinstance(raw_data, bytes):
                    text = raw_data.decode("utf-8", errors="replace")
                else:
                    text = str(raw_data)
                self.raw_edit.set_text(text)
                return

        if not self.datas:
            return
        raw_lines = [self._raw_start_line()]
        headers = self.datas.get(f"{self.PREFIX} Headers", {})
        raw_lines.extend(f"{key}: {value}" for key, value in headers.items())
        # 空行分隔头部和 body
        raw_lines.append("")
        if body:
            if isinstance(body, bytes):
                # 产出侧已用 `Message.get_text` 按 charset 解码好，直接消费。
                # 切勿在这里再解一次：body 是解压后的内容，重跑解压会产乱码。
                raw_lines.append(self.datas.get(f"{self.PREFIX} Body Text") or "")
            else:
                raw_lines.append(str(body))
        self.raw_edit.set_text("\n".join(raw_lines))

    def _set_body_view(self, view: str) -> None:
        """Body 页右侧那枚视图名徽标；没有视图名就不显示。"""
        self.body_view_badge.setText(str(view))
        self.body_view_badge.setVisible(bool(view))
        self.body_view_badge.adjustSize()


class RequestPane(MessagePane):
    """请求页：Headers / Query / Form / Cookies / Body / Trailers / Raw。

    `Query` 是 URL 上的查询串，`Form` 是 body 里的 urlencoded 表单 —— 两者改造前
    合在一个叫 `Params` 的标签里，看不出手上这份到底来自哪。
    """

    PREFIX = "Request"

    def _init_widget(self):
        super()._init_widget()
        self.query_widget = ItemDualPanel()
        self.query_widget.set_read_only(True)
        self.form_widget = ItemDualPanel()
        self.form_widget.set_read_only(True)

    def _init_layout(self):
        self.addTab("Headers", self.header_card, "Headers")
        self.addTab("Query", self.query_widget, "Query")
        self.addTab("Form", self.form_widget, "Form")
        self.addTab("Cookies", self.cookie_card, "Cookies")
        self.addTab("Body", self.body_card, "Body")
        self.addTab("Trailers", self.trailers_widget, "Trailers")
        self.addTab("Raw", self.raw_edit, "Raw")

    def set_data(self, data: dict):
        super().set_data(data)
        query = data.get("Request Params", {})
        self.query_widget.set_items(query)
        self.setTabVisible("Query", bool(query))

        form = data.get("Request Form", {})
        self.form_widget.set_items(form)
        self.setTabVisible("Form", bool(form))

    def _raw_from_controller(self, controller, flow_id: str) -> bytes | str | None:
        return controller.get_raw_request(flow_id)

    def _raw_start_line(self) -> str:
        data = self.datas or {}
        method = data.get("Method", "GET")
        path = data.get("Path", "/")
        version = data.get("HTTP Version", "HTTP/1.1")
        return f"{method} {path} {version}"


class ResponsePane(MessagePane):
    """响应页：Headers / Cookies / Body / Trailers / Raw。"""

    PREFIX = "Response"

    def _init_layout(self):
        self.addTab("Headers", self.header_card, "Headers")
        self.addTab("Cookies", self.cookie_card, "Cookies")
        self.addTab("Body", self.body_card, "Body")
        self.addTab("Trailers", self.trailers_widget, "Trailers")
        self.addTab("Raw", self.raw_edit, "Raw")

    def _raw_from_controller(self, controller, flow_id: str) -> bytes | str | None:
        # `get_raw_response` 给的是「状态行 + 头 + 空行 + body」的完整报文，
        # 直接按文本解码即可，不要走 body 解码器。
        return controller.get_raw_response(flow_id)

    def _raw_start_line(self) -> str:
        data = self.datas or {}
        version = data.get("Response HTTP Version", "HTTP/1.1")
        status = data.get("Status Code", 200)
        reason = data.get("Reason", "OK")
        return f"{version} {status} {reason}"


class RawStatePane(QWidget):
    """原始状态页：`Flow.get_state()` 整棵树，JSON 化之后交给现成的 JSON 面板。

    这一页是**完整性兜底**，所以不筛选、不打平：概览的卡片只显示
    `fields.SECTIONS` 列出来的字段，而以后 mitmproxy 加了新字段、或者某个字段我们
    没想到要显示，在这里都还找得到。

    复用 `JsonDualPanel` 而不是新写一个树：它的「文本 / 树」双视图正好是这一页要的
    两种读法，AGENTS.md 也记着编辑器不再新增。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.json_panel = JsonDualPanel()
        self.json_panel.set_read_only(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.json_panel)

    def set_data(self, data: dict) -> None:
        self.json_panel.set_text(state_json(data.get("raw_state")), lang=Language.JSON)

    def text(self) -> str:
        return self.json_panel.plain_text()


class FlowDataPanel(QWidget):
    """Flow 详情面板：头部一行上下文 + 一层导航 + 每页一屏。"""

    collapseRequested = Signal()  # 请求折叠面板

    # 页面顺序。route key 同时是 `set_page_visible` 的参数。
    PAGES: tuple[str, ...] = (
        "Overview",
        "Request",
        "Response",
        "Messages",
        "RawState",
    )

    def __init__(
        self,
        parent: QWidget,
        controller=None,
        capabilities: FlowViewCapabilities | None = None,
    ):
        super().__init__(parent=parent)
        self.controller = controller  # 保存 controller 引用
        self.capabilities = capabilities or CAPTURE_CAPABILITIES
        self.datas: dict = {}
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def minimumSizeHint(self) -> QSize:
        """Keep the outer 50/50 split usable despite the header and nav rows.

        比改造前宽（220 → 280）、比改造前矮（220 → 200）：横向要放得下一行
        ``[GET] url (200)`` 加导航，纵向反倒因为内层分割器消失省下一块。
        """
        return QSize(280, 200)

    def __init_widget(self):
        """初始化界面组件"""
        # 空
        self.empty_page = QWidget()
        self.empty_label = SubtitleLabel(self.empty_page)
        self.empty_label.setText(self.tr("Nothing to show"))
        self.empty_close_button = TransparentToolButton(self.empty_page)  # 空页面的 X
        self.empty_close_button.setIcon(FluentIcon.CLOSE)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 有数据
        self.overview = OverviewPane()
        self.req_panel = RequestPane(controller=self.controller)
        self.res_panel = ResponsePane(controller=self.controller)
        self.messages = MessagesPane()
        self.raw_state_panel = RawStatePane()

        self.nav = SegmentedWidget(self)
        self.pages = QStackedWidget(self)
        # route key → 页面。不用 `pages.findChild(QWidget, key)` 取：`findChild` 是递归的，
        # 而请求/响应页内部的每个标签页也都带 objectName，重名只是暂时没发生。
        self._pages: dict[str, QWidget] = {}
        self.__add_page("Overview", self.overview, self.tr("Overview"))
        self.__add_page("Request", self.req_panel, self.tr("Request"))
        self.__add_page("Response", self.res_panel, self.tr("Response"))
        self.__add_page("Messages", self.messages, self.tr("Messages"))
        self.__add_page("RawState", self.raw_state_panel, self.tr("Raw state"))
        self.nav.setItemFontSize(12)
        # 帧数/事件数挂在导航项右侧。`InfoBadge.make` 给的 manager 会跟着目标的
        # Resize / Move 重新定位，但**不管**徽标自己变宽（数字从 9 涨到 1024 时），
        # 所以 `__update_message_badge` 里 `setText` 之后要自己再 `position()` 一次。
        #
        # 位置取 `RIGHT` 而不是 `TOP_RIGHT`：后者把 y 放在 `-h/2`，而导航行是零边距
        # 布局，徽标上半截会被裁掉。
        self.message_badge = InfoBadge.make(
            "",
            parent=self.nav,
            level=InfoLevel.ATTENTION,
            target=self.nav.items["Messages"],
            position=InfoBadgePosition.RIGHT,
        )
        self.message_badge.hide()
        # 导航信号还没接上（`__connect_signal_to_slot` 在后面），两边各自置一下。
        self.nav.setCurrentItem("Overview")
        self.pages.setCurrentWidget(self.overview)

        self.command_bar = CommandBar(self)
        self.command_bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.copy_url_action = BaseAction(
            icon=FluentIcon.LINK, text=self.tr("Copy URL"), parent=self
        )
        self.copy_curl_action = BaseAction(
            icon=FluentIcon.COMMAND_PROMPT, text=self.tr("Copy cURL"), parent=self
        )
        self.command_bar.addActions([self.copy_url_action, self.copy_curl_action])
        # 重放按能力门控，和右键菜单同一个判据 —— 会话页是只读的，
        # `SessionViewController` 根本没有 `replay_flow`。
        self.replay_action = BaseAction(
            icon=FluentIcon.SYNC, text=self.tr("Replay"), parent=self
        )
        if self.capabilities.can_replay:
            self.command_bar.addAction(self.replay_action)
        # 标记与备注同样按能力门控：两者都要改**活** flow，而会话页那批流量是从
        # `.flow` 文件回来的死对象（`MitmFacade._mutate` 内核没跑就抛）。
        #
        # `setCheckable(True)` 就够了 —— `CommandButton` 本身是
        # `TransparentToggleToolButton` 的子类，会把 action 的
        # `isCheckable()` / `isChecked()` 照搬过去，不用自己往 bar 里塞裸控件。
        #
        # 哨兵位要先于 action 就位，见 `__set_mark_checked`：它分开「代码在同步勾选
        # 态」和「人点了按钮」。
        self.__syncing_mark = False
        self.mark_action = BaseAction(
            icon=BaseIcon.BOOKMARK_ADD, text=self.tr("Mark"), parent=self
        )
        self.mark_action.setCheckable(True)
        self.comment_action = BaseAction(
            icon=FluentIcon.EDIT, text=self.tr("Comment"), parent=self
        )
        if self.capabilities.can_mark:
            self.command_bar.addAction(self.mark_action)
        if self.capabilities.can_comment:
            self.command_bar.addAction(self.comment_action)
        # 导出**刻意不放**在这里：`FlowExportMenu` 是绑在 `FlowContextMenu` 的选中
        # 上下文上的（多选导出、HAR 落盘都读它的 `flows`），右键菜单里那一份已经够，
        # 搬到这里等于把选区语义复制一遍。

        self.detail_page = QWidget()

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty_page)  # index 0
        self.stack.addWidget(self.detail_page)  # index 1

        self.context_bar = QWidget(self)
        self.context_bar.setFixedHeight(40)
        self.context_method = BodyLabel(self.context_bar)
        method_font = self.context_method.font()
        method_font.setBold(True)
        self.context_method.setFont(method_font)
        self.context_url = BodyLabel(self.context_bar)
        self.context_url.setMinimumWidth(0)
        self.context_url.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.context_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # `InfoBadge` 是 `QLabel` 的子类，`setText`/`text()` 照常可用 —— 换掉的只是
        # 「谁决定这块的颜色」：从一句内联样式表换成 Fluent 的 level 语义。
        # 刻意用默认 level 构造再 `setLevel`：`InfoBadge.__init__` 先把 `self.level`
        # 置成 `INFOAMTION` 再调 `setLevel`，而 `setLevel` 相等就直接返回 ——
        # 构造时传 `INFOAMTION` 会让样式属性一次都没设上。
        self.context_status = InfoBadge(self.context_bar)
        self.context_status.setMinimumWidth(38)
        self.context_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.context_status.hide()
        self.context_duration = CaptionLabel(self.context_bar)
        self.context_duration.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.context_size = CaptionLabel(self.context_bar)
        self.context_size.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.context_close_button = TransparentToolButton(
            FluentIcon.CLOSE, self.context_bar
        )
        self.context_close_button.setFixedSize(32, 32)
        self.context_close_button.setIconSize(QSize(16, 16))
        self.context_close_button.setToolTip(self.tr("Close details"))
        self.context_close_button.setAccessibleName(self.tr("Close details"))

        self.__update_close_buttons()

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.context_bar)
        layout.addWidget(self.stack)

        context_layout = QHBoxLayout(self.context_bar)
        context_layout.setContentsMargins(10, 4, 8, 4)
        context_layout.setSpacing(8)
        context_layout.addWidget(self.context_method)
        context_layout.addWidget(self.context_url, 1)
        context_layout.addWidget(self.context_status)
        context_layout.addWidget(self.context_duration)
        context_layout.addWidget(self.context_size)
        context_layout.addWidget(self.context_close_button)

        # 导航和动作共用一行：详情面板常常只有两百来像素高，再给命令栏单开一行等于
        # 拿掉半屏内容。窄到放不下时 `CommandBar` 自己会把按钮收进「更多」。
        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(8, 4, 8, 0)
        detail_layout.setSpacing(4)
        nav_layout = QHBoxLayout()
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(8)
        nav_layout.addWidget(self.nav, 0)
        nav_layout.addStretch(1)
        nav_layout.addWidget(self.command_bar, 0)
        detail_layout.addLayout(nav_layout)
        detail_layout.addWidget(self.pages, 1)

        # 空页面布局：顶部右侧 X + 中间文字
        empty_layout = QVBoxLayout(self.empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)

        # 顶部行：弹簧 + X 按钮（靠右）
        top_layout = QHBoxLayout()
        top_layout.addStretch(1)
        top_layout.addWidget(self.empty_close_button)

        empty_layout.addLayout(top_layout)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_label, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch(1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.req_panel.close_button.clicked.connect(self.__collapse_panel)
        self.res_panel.close_button.clicked.connect(self.__collapse_panel)
        self.empty_close_button.clicked.connect(self.__collapse_panel)
        self.context_close_button.clicked.connect(self.__collapse_panel)
        self.nav.currentItemChanged.connect(self.__on_nav_changed)
        self.copy_url_action.triggered.connect(self.__on_copy_url)
        self.copy_curl_action.triggered.connect(self.__on_copy_curl)
        self.replay_action.triggered.connect(self.__on_replay)
        self.mark_action.toggled.connect(self.__on_mark_toggled)
        self.comment_action.triggered.connect(self.__on_comment)
        self.__connect_controller(self.controller)

    def __connect_controller(self, controller, connect: bool = True) -> None:
        """接上/断开 controller 的三条 websocket 信号。

        用 `getattr` 探而不是直接 `controller.websocket_frame`：只读的
        `SessionViewController` **刻意**一条都不提供（会话文件里的流量早就结束了，
        没有「新帧到达」这件事），硬接会 `AttributeError`。
        """
        if controller is None:
            return
        slots = {
            "websocket_started": self.__on_ws_started,
            "websocket_frame": self.__on_ws_frame,
            "websocket_closed": self.__on_ws_closed,
        }
        for name, slot in slots.items():
            signal = getattr(controller, name, None)
            if signal is None:
                continue
            if connect:
                signal.connect(slot)
            else:
                # 换 controller 时旧的可能压根没接上（构造时 controller 是 None，
                # 或者上一个是只读的）—— 没接过的 disconnect 会抛。
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

    # —— 页面 ——

    def __add_page(self, route_key: str, widget: QWidget, text: str) -> None:
        widget.setObjectName(route_key)
        self._pages[route_key] = widget
        self.pages.addWidget(widget)
        self.nav.addItem(routeKey=route_key, text=text)

    @Slot(str)
    def __on_nav_changed(self, route_key: str) -> None:
        """导航项→堆叠页。

        不用 `addItem(onClick=...)`：那个只接到按钮的 `clicked`，只有真被点才走，
        `setCurrentItem()` 这种代码里的切页（初始选中、当前页被隐藏后回退）
        就什么也不会发生。`currentItemChanged` 两种路径都走。
        """
        page = self._pages.get(route_key)
        if page is not None:
            self.pages.setCurrentWidget(page)

    def set_page_visible(self, route_key: str, visible: bool) -> None:
        """隐藏/显示一整页（消息页这类「没有就不该出现」的）。

        不走 `Pivot.removeWidget`：那个会把导航项 `deleteLater` 掉，下一条流量又有
        内容时得重新建一遍还得记住原来插在第几位。隐藏就够了 —— `QHBoxLayout`
        不给隐藏控件留位置。
        """
        item = self.nav.items.get(route_key)
        if item is None:
            return
        item.setVisible(visible)
        if not visible and self.nav.currentRouteKey() == route_key:
            self.nav.setCurrentItem(self.__first_visible_page())

    def __first_visible_page(self) -> str:
        """第一个没被隐藏的页 —— 当前页被藏起来时落到这里。

        用 `isHidden()` 而不是 `isVisible()`：后者连祖先一起算，面板还没显示出来时
        每一项都是不可见的。
        """
        for route_key in self.PAGES:
            item = self.nav.items.get(route_key)
            if item is not None and not item.isHidden():
                return route_key
        return self.PAGES[0]

    # —— 槽 ——

    @Slot()
    def __update_close_buttons(self):
        """The outer context bar owns the single detail close affordance."""
        self.empty_close_button.hide()
        self.req_panel.close_button.hide()
        self.res_panel.close_button.hide()

    @Slot()
    def __collapse_panel(self):
        """折叠面板"""
        self.collapseRequested.emit()

    @Slot()
    def __on_copy_url(self) -> None:
        self.__copy(self.datas.get("URL", ""), "URL")

    @Slot()
    def __on_copy_curl(self) -> None:
        self.__copy(self.datas.get("curl_command", ""), "cURL")

    def __copy(self, text: str, label: str) -> None:
        """复制到剪贴板；没内容就说清是「还没有」而不是静默无反应。"""
        if not text:
            show_warning(
                self.tr("Nothing to copy"),
                self.tr("%s is not ready yet") % label,
                self.window(),
            )
            return
        QApplication.clipboard().setText(str(text))
        show_success(
            self.tr("Success"),
            self.tr("%s copied to clipboard") % label,
            self.window(),
        )

    @Slot()
    def __on_replay(self) -> None:
        """重放当前这一条。多选重放仍归右键菜单 —— 面板里只有「这一条」。"""
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            return
        try:
            self.controller.replay_flow(flow_id)
        except (AttributeError, ValueError, RuntimeError) as exc:
            show_warning(self.tr("Replay failed"), str(exc), self.window())

    # —— 标记与备注 ——

    @Slot(bool)
    def __on_mark_toggled(self, checked: bool) -> None:
        """切标记。写回失败要把按钮弹回去 —— 否则界面说「标了」而 flow 上没有。"""
        if self.__syncing_mark:
            return
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            self.__set_mark_checked(bool(self.datas.get("marked")))
            return
        marked = MARKER_DEFAULT if checked else ""
        try:
            self.controller.set_flow_marked(flow_id, marked)
        except (AttributeError, ValueError, RuntimeError) as exc:
            show_warning(self.tr("Failed to mark"), str(exc), self.window())
            self.__set_mark_checked(bool(self.datas.get("marked")))
            return
        self.__store("marked", marked)

    @Slot()
    def __on_comment(self) -> None:
        """编辑备注。

        复用右键菜单那只 `CommentDialog` 而不是另开一个气泡：同一件事在两处长成两个
        样子（模态框 / 浮出层、两份占位文案、两种保存反馈）本身就是毛病，何况对话框
        那一份已经在用了。"""
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            return
        dialog = CommentDialog(str(self.datas.get("comment") or ""), self.window())
        if not dialog.exec():
            return
        comment = dialog.comment()
        try:
            self.controller.set_flow_comment(flow_id, comment)
        except (AttributeError, ValueError, RuntimeError) as exc:
            show_warning(self.tr("Failed to save comment"), str(exc), self.window())
            return
        show_success(self.tr("Success"), self.tr("Comment saved"), self.window())
        self.__store("comment", comment)

    def __store(self, key: str, value: str) -> None:
        """写回成功后就地更新缓存的详情并只重画概览卡片。

        刻意不走整个 `set_data`：那会连消息页一起重建，把 WS 帧表的选中行和滚动位置
        清掉 —— 帧还在一秒几十条地进来，改个标记就把人看的位置弄丢说不过去。
        重新问一趟 `flow_detail` 也没必要，改的就是这一个字段。"""
        self.datas[key] = value
        self.overview.set_data(self.datas)

    def __set_mark_checked(self, checked: bool) -> None:
        """摆按钮的勾选态，且不触发写回。

        `toggled` 对 `setChecked` 和真人点击一样会发 —— 不拦一道，光是切换选中的流量
        就会把「上一条的标记」写到刚选中的那条上去。

        用一个哨兵位而不是 `blockSignals`：`CommandButton.setAction` 是靠
        `action.toggled` / `action.changed` 把勾选态搬到按钮上的，掐掉 action 的信号
        等于让按钮画的还是上一条流量的样子。"""
        self.__syncing_mark = True
        try:
            self.mark_action.setChecked(checked)
        finally:
            self.__syncing_mark = False

    # —— WebSocket 实时 ——

    def __is_current(self, flow_id: str) -> bool:
        """这条信号说的是不是面板上正显示的那一条。

        信号是广播的：抓包时几十条 WS 连接同时在推帧，不过滤等于把所有连接的帧混进
        同一张表。"""
        return bool(flow_id) and flow_id == self.datas.get("id")

    @Slot(str)
    def __on_ws_started(self, flow_id: str) -> None:
        """握手成功。选中时还是普通 HTTP 流量（消息页藏着）的那一条，从这里开始有帧。"""
        if not self.__is_current(flow_id):
            return
        self.messages.show_websocket(self.__frames(flow_id), self.__close(flow_id))
        self.__refresh_message_page()

    @Slot(str, object)
    def __on_ws_frame(self, flow_id: str, frame: WsFrame) -> None:
        if not self.__is_current(flow_id):
            return
        self.messages.append_frame(frame)
        self.__refresh_message_page()

    @Slot(str, object)
    def __on_ws_closed(self, flow_id: str, close: WsClose) -> None:
        if not self.__is_current(flow_id):
            return
        self.messages.set_close(close)

    def __frames(self, flow_id: str) -> list[WsFrame]:
        """向 controller 要帧。跨线程那一步归门面，这里只兜异常。

        取不到帧时给空表而不是让异常炸穿：一条流量的帧读不出来，不该连带把整个详情
        面板打空。"""
        if not self.controller or not flow_id:
            return []
        try:
            return self.controller.websocket_frames(flow_id)
        except (AttributeError, RuntimeError) as exc:
            log.warning("读取 websocket 帧失败 flow_id=%s: %s", flow_id, exc)
            return []

    def __close(self, flow_id: str) -> WsClose:
        if not self.controller or not flow_id:
            return WsClose()
        try:
            return self.controller.websocket_close(flow_id)
        except (AttributeError, RuntimeError) as exc:
            log.warning("读取 websocket 关闭信息失败 flow_id=%s: %s", flow_id, exc)
            return WsClose()

    def __refresh_message_page(self) -> None:
        """消息页的可见性与计数徽标 —— 换流量、新帧到达都要过这里。"""
        self.set_page_visible("Messages", self.messages.applicable)
        count = self.messages.count
        if not self.messages.applicable or not count:
            self.message_badge.hide()
            return
        self.message_badge.setText(str(count))
        self.message_badge.adjustSize()
        # `InfoBadgeManager` 只在目标 Resize / Move 时重定位，徽标自己变宽不算。
        self.message_badge.move(self.message_badge.manager.position())
        self.message_badge.show()

    # —— 数据 ——

    def set_controller(self, controller) -> None:
        """更新 Flow 查看控制器并同步到请求、响应面板。"""
        if controller is self.controller:
            return
        self.__connect_controller(self.controller, connect=False)
        self.controller = controller
        self.req_panel.controller = controller
        self.res_panel.controller = controller
        self.__connect_controller(controller)

    def set_data(self, data: dict):
        """有数据时调用，切换到详情页并填充

        Args:
            data: 数据字典
        """
        self.datas = data
        self.overview.set_data(data)
        self.req_panel.set_data(data)
        self.res_panel.set_data(data)
        self.raw_state_panel.set_data(data)
        self.__set_messages(data)
        self._update_context_bar(data)
        # 只抓到请求的流量没有响应可看 —— 那一页整条藏掉，别让人点进空白。
        self.set_page_visible(
            "Response", bool(data.get("res_headers_size") is not None)
        )
        self.copy_url_action.setEnabled(bool(data.get("URL")))
        self.copy_curl_action.setEnabled(bool(data.get("curl_command")))
        # 重放 / 标记 / 备注都得有个活控制器加一条认得的 flow 才谈得上。
        editable = bool(self.controller and data.get("id"))
        self.replay_action.setEnabled(editable)
        self.mark_action.setEnabled(editable)
        self.comment_action.setEnabled(editable)
        self.__set_mark_checked(bool(data.get("marked")))
        self.stack.setCurrentIndex(1)

    def __set_messages(self, data: dict) -> None:
        """填消息页。

        帧要现取：详情字典是在 mitm 线程上一次性构建的（`core/mitm/detail.py`），
        塞进去上千帧等于让每一条流量都背着一份帧列表过界，而九成流量压根不是 WS。
        SSE 那一路相反 —— 事件全在已缓冲的响应体里，`MessagesPane` 自己解析就够。"""
        flow_id = str(data.get("id") or "")
        if is_websocket(data):
            self.messages.set_data(data, self.__frames(flow_id), self.__close(flow_id))
        else:
            self.messages.set_data(data)
        self.__refresh_message_page()

    def _update_context_bar(self, data: dict) -> None:
        method = str(data.get("Method", "—"))
        url = str(data.get("URL", "—"))
        status = str(data.get("Status Code", self.tr("Pending")))
        duration = str(data.get("Duration", ""))
        total = int(data.get("total_size") or 0)
        self.context_method.setText(method)
        self.context_url.setText(url)
        self.context_url.setToolTip(url)
        self.context_status.setText(status)
        self.context_status.setLevel(status_level(status))
        self.context_status.adjustSize()
        self.context_status.show()
        self.context_duration.setText(duration)
        self.context_size.setText(human.pretty_size(total) if total else "")
