"""详情面板：左右分栏（请求区 | 响应区），各一排扁平标签。

改造前是「一层导航：概览/请求/响应/消息/原始状态」+ 请求/响应页内的次级
Pivot（Headers/Query/Form/…/Raw），两层导航点两次才到一份报文头。现在回到
左右分栏：左栏是请求区（总览/原始/请求头/请求体/查询参数/Cookies/备注），
右栏是响应区（原始/响应头/响应体/消息）。全局布局设置说的是「表格 vs 详情」
的排布，内层分栏与它相反（`inverted=True`）才放得下：全局横向（详情窄而高）
时内层上下排，全局纵向（详情宽而矮）时内层左右排，比例在切换时保持
（`OrientationSplitter` 自己处理）。

「消息」栏只对 WebSocket 与 SSE 流量出现（见 `messages.py`），其余一律整条
隐藏 —— 普通请求点进去只会看到一张空表。

没有响应的流量（只抓到请求）右栏整体隐藏；× 收起整个详情面板，横向时挂在
右栏标签行右端、纵向时挂在上栏（请求区）标签行右端 —— 永远贴着离表格最远的
那条边。

`ResponsePane` 除了做详情面板的右栏，还被 compose 页整个复用（那边的响应
展示就是这一栏；没有 flow 可问，controller 为 None，Raw 走手工拼装的兜底）。

这个模块只搬「详情」那一半；表格、外层分割器、空状态仍留在 `views.py`，而
`views.py` 继续 re-export `FlowDataPanel`，两个挂载点的 import 不受影响。
"""

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, QTimer, Signal, Slot
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
    FluentIcon,
    InfoBadge,
    InfoLevel,
    RoundMenu,
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
from ferret.apps.common.splitter import OrientationSplitter
from ferret.core.log import get_logger
from ferret.core.mitm import MARKER_DEFAULT, SseEvent, WsClose, WsFrame
from ferret.core.settings import CONFIG

log = get_logger("flow.detail")

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


def _request_start_line(data: dict) -> str:
    """Raw 页手工拼装时的请求行。"""
    method = data.get("Method", "GET")
    path = data.get("Path", "/")
    version = data.get("HTTP Version", "HTTP/1.1")
    return f"{method} {path} {version}"


def _response_start_line(data: dict) -> str:
    """Raw 页手工拼装时的状态行。"""
    version = data.get("Response HTTP Version", "HTTP/1.1")
    status = data.get("Status Code", 200)
    reason = data.get("Reason", "OK")
    return f"{version} {status} {reason}"


def _fill_raw(
    edit: ToolPlainTextEdit,
    datas: dict | None,
    prefix: str,
    start_line: str,
    controller,
    getter: str,
) -> None:
    """线上原始报文；controller 给不出来就按详情字典手工拼一份。

    Args:
        edit: 目标编辑器
        datas: 详情字典；为空且没有 controller 报文时不填
        prefix: ``Request`` / ``Response``
        start_line: 手工拼装时的首行：请求行或状态行
        controller: flow 查看控制器；可为 None（compose 那一路就没有）
        getter: controller 上取原始报文的方法名（`get_raw_request` /
            `get_raw_response`）
    """
    raw_data = None
    flow_id = str((datas or {}).get("Flow ID") or "")
    if controller and flow_id:
        try:
            raw_data = getattr(controller, getter)(flow_id)
        except (AttributeError, ValueError, TypeError, RuntimeError) as e:
            log.warning("failed to read the raw HTTP payload: %s", e)
        if raw_data:
            if isinstance(raw_data, bytes):
                text = raw_data.decode("utf-8", errors="replace")
            else:
                text = str(raw_data)
            edit.set_text(text)
            return

    if not datas:
        return
    raw_lines = [start_line]
    headers = datas.get(f"{prefix} Headers", {})
    raw_lines.extend(f"{key}: {value}" for key, value in headers.items())
    # 空行分隔头部和 body
    raw_lines.append("")
    body = datas.get(f"{prefix} Body", b"")
    if body:
        if isinstance(body, bytes):
            # 产出侧已用 `Message.get_text` 按 charset 解码好，直接消费。
            # 切勿在这里再解一次：body 是解压后的内容，重跑解压会产乱码。
            raw_lines.append(datas.get(f"{prefix} Body Text") or "")
        else:
            raw_lines.append(str(body))
    edit.set_text("\n".join(raw_lines))


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
        self.copy_button.setToolTip(self.tr("复制 Cookie"))
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
            show_warning(self.tr("提示"), self.tr("没有可复制的 Cookie"), self.window())
            return

        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        QApplication.clipboard().setText(cookie_str)
        show_success(self.tr("成功"), self.tr("Cookie 已复制到剪贴板"), self.window())


class BodyPane(QStackedWidget):
    """报文体三态：文本/JSON 树双视图 | 表单键值（仅请求）| 空占位。

    改造前 body 为空的流量把 Body 标签也照常摆出来，点进去是一片编辑器空白；
    现在给一张居中的「无任何数据」占位，读起来不用猜。urlencoded 表单是请求体的
    一种格式：有解析结果时用键值表格呈现，不再像改造前那样单开一个 Form 标签。
    """

    def __init__(self, allow_form: bool = False, parent=None):
        super().__init__(parent)
        self.json_panel = JsonDualPanel()
        self.json_panel.set_read_only(True)
        self.addWidget(self.json_panel)

        self.form_panel: ItemDualPanel | None = None
        if allow_form:
            self.form_panel = ItemDualPanel()
            self.form_panel.set_read_only(True)
            self.addWidget(self.form_panel)

        self.empty_label = SubtitleLabel(self.tr("无任何数据"))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.addWidget(self.empty_label)

    def set_data(self, data: dict, prefix: str) -> None:
        """按详情字典切页：表单优先，其次报文体文本，最后空占位。"""
        text = data.get(f"{prefix} Body Pretty")
        if text is None:
            text = data.get(f"{prefix} Body Text") or ""
        if self.form_panel is not None:
            form = data.get(f"{prefix} Form") or {}
            if form:
                self.form_panel.set_items(form)
                self.setCurrentWidget(self.form_panel)
                return
        if text:
            lang = _body_lang(data.get(f"{prefix} Body Syntax", "none"), str(text))
            self.json_panel.set_text(str(text), lang=lang)
            self.setCurrentWidget(self.json_panel)
        else:
            self.setCurrentWidget(self.empty_label)


class CommentPane(QWidget):
    """备注内联编辑页：直接编辑，脏了才亮保存。

    改造前备注只有一个弹窗入口（CommandBar 的 Comment 按钮）；现在这页常驻
    左栏，弹窗入口保留在「更多」菜单里 —— 两处共用一套写回（面板接到
    `commentSaved` 后走同一个 `set_flow_comment`）。
    """

    commentSaved = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._saved_text = ""
        self._syncing = False

        self.edit = ToolPlainTextEdit()
        self.save_button = TransparentToolButton(FluentIcon.SAVE)
        self.save_button.setToolTip(self.tr("保存备注"))
        self.save_button.setEnabled(False)
        self.save_button.installEventFilter(
            ToolTipFilter(self.save_button, 1000, ToolTipPosition.TOP)
        )
        self.edit.tool_layout.addWidget(self.save_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)

        self.edit.changed.connect(self.__on_changed)
        self.save_button.clicked.connect(self.__on_save)

    def set_data(self, data: dict) -> None:
        """灌入当前流量的备注。程序化换文本不算编辑，哨兵拦住。"""
        self.__load(str(data.get("comment") or ""))

    def text(self) -> str:
        return self.edit.text()

    def mark_saved(self, comment: str) -> None:
        """写回成功后由面板回填「已保存」基线，保存按钮随之熄灭。"""
        self.__load(comment)

    def set_read_only(self, read_only: bool) -> None:
        self.edit.set_read_only(read_only)
        if read_only:
            self.save_button.setEnabled(False)

    def __load(self, comment: str) -> None:
        self._syncing = True
        try:
            self.edit.set_text(comment, lang=Language.TEXT)
        finally:
            self._syncing = False
        self._saved_text = comment
        self.__refresh()

    def __on_changed(self) -> None:
        if not self._syncing:
            self.__refresh()

    def __refresh(self) -> None:
        self.save_button.setEnabled(
            self.edit.isEnabled()
            and not self.edit.is_read_only()
            and self.edit.text() != self._saved_text
        )

    def __on_save(self) -> None:
        self.commentSaved.emit(self.edit.text())


class PivotBadgeAnchor(QObject):
    """把 `InfoBadge` 外挂在 Pivot 标签右侧的定位器。

    qfw 自带的 `InfoBadgeManager`（RIGHT 档）公式是 ``x = 标签.right() - 徽标宽//2``，
    徽标有一半永远压在标签文字上。这里改成完全外挂：``x = 标签.right() + GAP``、
    y 垂直居中，并跟住目标的 Resize / Move —— 与 manager 同一套事件面，只是
    位置公式不同。徽标自身变宽（数字 9 → 1024）不触发目标事件，调用方在
    `setText` 之后要自己再 `reposition` 一次。

    徽标必须挂在**比 pivot 更宽的宿主**上（本面板传入 `res_pane`）：Qt 子件永远
    被父件矩形裁剪，而 pivot 的宽度恰好等于内容 —— 挂在它身上、越出右缘的部分
    会被整枚裁掉。坐标一律经 `mapTo` 折算到徽标自己的父件坐标系。
    """

    GAP = 4

    def __init__(self, target: QWidget, badge: InfoBadge, parent=None) -> None:
        super().__init__(parent)
        self.target = target
        self.badge = badge
        self.pivot = target.parentWidget()
        if self.pivot is None:
            return
        target.installEventFilter(self)
        self.pivot.installEventFilter(self)

    def eventFilter(self, obj, e: QEvent) -> bool:
        if obj in (self.target, self.pivot) and e.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Move,
        ):
            self.reposition()
        return super().eventFilter(obj, e)

    def reposition(self) -> None:
        self.badge.move(self.position())

    def position(self) -> QPoint:
        host = self.badge.parentWidget()
        if self.pivot is None or host is None:
            return self.badge.pos()
        anchor = self.pivot.mapTo(host, self.target.geometry().topRight())
        return QPoint(
            anchor.x() + self.GAP,
            anchor.y() + self.target.height() // 2 - self.badge.height() // 2,
        )


class ResponsePane(TabPanel):
    """响应栏：Raw / Headers(N) / Body。

    详情面板的右栏，也被 compose 页整个复用 —— 那边没有 flow 可问，
    controller 为 None，Raw 走详情字典手工拼装的兜底。

    `with_raw=False` 时不给 Raw 标签（compose 的响应结果不需要原始报文兜底，
    那边用 响应头/响应体/性能 三条标签）。宿主仍可用 `addTab` 往里追加自己的
    标签（compose 把「性能」追加在末位）。

    关闭按钮默认藏：compose 那路不需要 ×；详情面板里由 `FlowDataPanel` 按
    分栏方向决定它显不显示。
    """

    PREFIX = "Response"

    def __init__(self, parent=None, controller=None, *, with_raw: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.datas: dict | None = None
        self.setTabFontSize(12)

        self.header_card = ItemDualPanel()
        self.header_card.set_read_only(True)

        self.body_pane = BodyPane(allow_form=False)

        self.raw_edit = ToolPlainTextEdit()
        self.raw_edit.set_read_only(True)

        if with_raw:
            self.addTab("Raw", self.raw_edit, self.tr("原始"))
        self.addTab("Headers", self.header_card, self.tr("响应头"))
        self.addTab("Body", self.body_pane, self.tr("响应体"))

        # contentview 的视图名（JSON / gRPC / Multipart Form …）。改造前是把它
        # 拼进 Body 标签的文字里再 `adjustSize()`，标签宽度跟着每条流量跳；挪成
        # 右侧一枚徽标之后标签栏宽度稳定，视图名也不再和标签文案抢位置。
        # 徽标插在弹簧之后、动作区之前，所以永远贴着右侧动作区。
        self.body_view_badge = InfoBadge(self)
        self.body_view_badge.setLevel(InfoLevel.INFOAMTION)
        self.body_view_badge.hide()
        self.tab_layout.insertWidget(2, self.body_view_badge)

        self.close_button.hide()

    def set_data(self, data: dict) -> None:
        """填充数据（详情字典，mitm 阶段已预解析结构化字段）。"""
        self.datas = data
        headers = data.get("Response Headers", {})
        self.header_card.set_items(headers)
        self.body_pane.set_data(data, self.PREFIX)
        self._set_body_view(data.get("Response Body View", ""))
        _fill_raw(
            self.raw_edit,
            data,
            self.PREFIX,
            _response_start_line(data),
            self.controller,
            "get_raw_response",
        )
        self.setTabText(
            "Headers", self.tr("响应头 ({count})").format(count=len(headers))
        )

    def _set_body_view(self, view: str) -> None:
        """Body 标签行右侧那枚视图名徽标；没有视图名就不显示。"""
        self.body_view_badge.setText(str(view))
        self.body_view_badge.setVisible(bool(view))
        self.body_view_badge.adjustSize()


class FlowDataPanel(QWidget):
    """Flow 详情面板：左请求区 | 右响应区，方向跟随全局布局。"""

    collapseRequested = Signal()  # 请求折叠面板

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
        self._split_normalized = False
        # 哨兵位要先于动作就位，见 `__set_mark_checked`：它分开「代码在同步勾选
        # 态」和「人点了按钮」。
        self.__syncing_mark = False
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def minimumSizeHint(self) -> QSize:
        """Keep the outer 50/50 split usable despite the two panes.

        数值沿用改造前（280x200）：返回一个偏小的 hint，外层分割器收起面板时
        才能压到 0 —— 真实下限由内层布局自己兜着。
        """
        return QSize(280, 200)

    def __init_widget(self):
        """初始化界面组件"""
        # 空
        self.empty_page = QWidget()
        self.empty_label = SubtitleLabel(self.empty_page)
        self.empty_label.setText(self.tr("什么都没有"))
        self.empty_close_button = TransparentToolButton(self.empty_page)  # 空页面的 X
        self.empty_close_button.setIcon(FluentIcon.CLOSE)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # —— 左栏（请求 + flow 级信息）——
        self.req_tabs = TabPanel()
        self.req_tabs.setTabFontSize(12)

        self.overview = OverviewPane()

        self.req_raw = ToolPlainTextEdit()
        self.req_raw.set_read_only(True)

        self.req_headers = ItemDualPanel()
        self.req_headers.set_read_only(True)

        self.req_body = BodyPane(allow_form=True)

        self.query_widget = ItemDualPanel()
        self.query_widget.set_read_only(True)

        self.cookie_widget = CookieWidget()
        self.cookie_card = SimpleCardWidget()
        self.cookie_card.setBorderRadius(0)
        cookie_layout = QVBoxLayout(self.cookie_card)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.addWidget(self.cookie_widget)

        self.comment_pane = CommentPane()

        self.req_tabs.addTab("Overview", self.overview, self.tr("概览"))
        self.req_tabs.addTab("Raw", self.req_raw, self.tr("原始"))
        self.req_tabs.addTab("Headers", self.req_headers, self.tr("请求头"))
        self.req_tabs.addTab("Body", self.req_body, self.tr("请求体"))
        self.req_tabs.addTab("Query", self.query_widget, self.tr("查询参数"))
        self.req_tabs.addTab("Cookies", self.cookie_card, self.tr("Cookie"))
        self.req_tabs.addTab("Comment", self.comment_pane, self.tr("备注"))

        # 请求体视图名徽标 + 「…」动作菜单，插在弹簧之后、动作区之前，
        # 与右栏的徽标/× 同一条边。
        self.req_body_badge = InfoBadge(self.req_tabs)
        self.req_body_badge.setLevel(InfoLevel.INFOAMTION)
        self.req_body_badge.hide()
        self.req_tabs.tab_layout.insertWidget(2, self.req_body_badge)
        self.more_button = TransparentToolButton(FluentIcon.MORE, self.req_tabs)
        self.more_button.setToolTip(self.tr("更多操作"))
        self.req_tabs.tab_layout.insertWidget(3, self.more_button)

        # —— 右栏（响应区）：「消息」由本面板追加 ——
        self.res_pane = ResponsePane(controller=self.controller)
        self.messages = MessagesPane()
        self.res_pane.addTab("Messages", self.messages, self.tr("消息"))
        self.res_pane.setTabVisible("Messages", False)
        # 帧数/事件数挂在「消息」标签右侧。qfw 的 `InfoBadgeManager` 会把徽标
        # 半压在标签文字上，这里换 `PivotBadgeAnchor` 完全外挂；徽标挂 `res_pane`
        # 而不是 pivot —— pivot 宽度恰等于内容，越出右缘的子件会被父件矩形
        # 裁掉。徽标自己变宽（数字从 9 涨到 1024）不触发目标事件，所以
        # `__refresh_message_page` 里 `setText` 之后要自己再 `reposition` 一次。
        self.message_badge = InfoBadge.make(
            "",
            parent=self.res_pane,
            level=InfoLevel.ATTENTION,
        )
        self.message_badge.manager = PivotBadgeAnchor(
            self.res_pane.pivot.items["Messages"], self.message_badge
        )
        # pivot 与徽标同挂 res_pane，创建顺序在徽标之前；浮层要压在其上。
        self.message_badge.raise_()
        self.message_badge.hide()

        # 「…」菜单动作。备注弹窗保留（内联编辑页承担日常编辑，两处共用写回）；
        # 重放/标记都得改**活** flow，会话页那批流量是从 `.flow` 文件回来的死对象
        # （`MitmFacade._mutate` 内核没跑就抛），动作按能力门控。cURL/raw/HAR
        # 那套导出是右键菜单 `FlowExportMenu` 的领地，这里不重复。
        self.copy_url_action = BaseAction(
            icon=FluentIcon.LINK, text=self.tr("复制 URL"), parent=self
        )
        self.replay_action = BaseAction(
            icon=FluentIcon.SYNC, text=self.tr("重发"), parent=self
        )
        self.mark_action = BaseAction(
            icon=BaseIcon.BOOKMARK_ADD, text=self.tr("标记"), parent=self
        )
        self.mark_action.setCheckable(True)
        self.comment_action = BaseAction(
            icon=FluentIcon.EDIT, text=self.tr("备注"), parent=self
        )

        self.detail_page = QWidget()
        # inverted=True：全局布局说的是「表格 vs 详情」的排布，内层分栏与它
        # 相反才放得下 —— 全局横向（表格|详情左右排，详情窄而高）时内层上下排；
        # 全局纵向（表格在上、详情在下，详情宽而矮）时内层左右排。
        self.splitter = OrientationSplitter(inverted=True, parent=self.detail_page)
        self.splitter.addWidget(self.req_tabs)
        self.splitter.addWidget(self.res_pane)
        # 两栏初始 50/50：QSplitter 默认按子控件的 sizeHint / 最小提示分初始位，
        # 左栏七条标签的 Pivot 提示宽度比半栏还宽，直接把等分顶歪。Ignored 策略
        # 让分割条完全按 `setSizes` 分配 —— 首次可见与右栏重现时都从对半开始，
        # 用户拖出的比例此后自然保留。
        for pane in (self.req_tabs, self.res_pane):
            pane.setMinimumSize(0, 0)
            pane.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty_page)  # index 0
        self.stack.addWidget(self.detail_page)  # index 1

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.stack)

        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(8, 4, 8, 8)
        detail_layout.setSpacing(0)
        detail_layout.addWidget(self.splitter)

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
        self.empty_close_button.clicked.connect(self.__collapse_panel)
        self.req_tabs.close_button.clicked.connect(self.__collapse_panel)
        self.res_pane.close_button.clicked.connect(self.__collapse_panel)
        self.more_button.clicked.connect(self.__on_more)
        self.copy_url_action.triggered.connect(self.__on_copy_url)
        self.replay_action.triggered.connect(self.__on_replay)
        self.mark_action.toggled.connect(self.__on_mark_toggled)
        self.comment_action.triggered.connect(self.__on_comment)
        self.comment_pane.commentSaved.connect(self.__on_comment_saved)
        # 分栏方向由 `OrientationSplitter` 自己跟配置走；它先连的槽先跑，
        # 这里读到的是换完之后的方向。
        CONFIG.layout.valueChanged.connect(self._on_layout_changed)
        self.__connect_controller(self.controller)

    def __connect_controller(self, controller, connect: bool = True) -> None:
        """接上/断开 controller 的 WS / SSE 实时信号。

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
            "sse_started": self.__on_sse_started,
            "sse_event": self.__on_sse_event,
            "sse_ended": self.__on_sse_ended,
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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 内层分栏的初始 50/50 不能指望构造期：QSplitter 初始分位只认两侧
        # sizeHint 的比例，而左栏七条标签的 Pivot 宽得离谱。首次可见的下一拍
        # 按真实宽度平分一次（与 compose 页、flow 页 `_apply_equal_sizes`
        # 同一模式），之后用户拖过就不再打扰。
        if not self._split_normalized:
            self._split_normalized = True
            QTimer.singleShot(0, self.splitter.set_equal_sizes)

    # —— 分栏 ——

    @Slot()
    def _on_layout_changed(self) -> None:
        self.__sync_close_host()

    def __sync_close_host(self) -> None:
        """× 跟着分栏方向换宿主：横向挂右栏，纵向挂上栏（请求区）。

        右栏收起（没有响应的流量）时只剩一栏，× 落回请求区，保证随时可点。
        """
        horizontal = self.splitter.orientation() == Qt.Orientation.Horizontal
        on_response = horizontal and not self.res_pane.isHidden()
        self.res_pane.close_button.setVisible(on_response)
        self.req_tabs.close_button.setVisible(not on_response)

    def __sync_response_pane(self, data: dict) -> None:
        """没有响应可看的流量右栏整体收起 —— 不是藏一条标签，是整个半栏。

        用 `setVisible` 而不是 `BaseSplitter.collapse(1)`：collapse 之后分割条
        还能拖着展开，露出来的却是半栏空白；隐藏连分割条一起收掉。重新一起
        出现时按 50/50 平分起步，用户之后拖出的比例自然保留。
        """
        visible = data.get("res_headers_size") is not None
        if visible == (not self.res_pane.isHidden()):
            return
        if visible:
            self.res_pane.setVisible(True)
            self.splitter.set_equal_sizes()
        else:
            self.res_pane.setVisible(False)
        self.__sync_close_host()

    # —— 槽 ——

    @Slot()
    def __collapse_panel(self):
        """折叠面板"""
        self.collapseRequested.emit()

    def _more_actions(self) -> list[BaseAction]:
        """「…」菜单里该出现的动作，按能力门控。独立成方法供测试钉住门控。"""
        actions = [self.copy_url_action]
        if self.capabilities.can_replay:
            actions.append(self.replay_action)
        if self.capabilities.can_mark:
            actions.append(self.mark_action)
        if self.capabilities.can_comment:
            actions.append(self.comment_action)
        return actions

    @Slot()
    def __on_more(self) -> None:
        """「…」动作菜单：复制 / 重放 / 标记 / 备注弹窗。"""
        menu = RoundMenu(parent=self)
        for action in self._more_actions():
            menu.addAction(action)
        menu.exec(self.more_button.mapToGlobal(QPoint(0, self.more_button.height())))

    @Slot()
    def __on_copy_url(self) -> None:
        self.__copy(self.datas.get("URL", ""), "URL")

    def __copy(self, text: str, label: str) -> None:
        """复制到剪贴板；没内容就说清是「还没有」而不是静默无反应。"""
        if not text:
            show_warning(
                self.tr("没有可复制的内容"),
                self.tr("%s 还没有准备好") % label,
                self.window(),
            )
            return
        QApplication.clipboard().setText(str(text))
        show_success(
            self.tr("成功"),
            self.tr("%s 已复制到剪贴板") % label,
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
            show_warning(self.tr("重发失败"), str(exc), self.window())

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
            show_warning(self.tr("标记失败"), str(exc), self.window())
            self.__set_mark_checked(bool(self.datas.get("marked")))
            return
        self.__store("marked", marked)

    @Slot()
    def __on_comment(self) -> None:
        """弹窗编辑备注（「…」菜单入口；内联编辑页是日常入口）。

        复用右键菜单那只 `CommentDialog` 而不是另开一个气泡：同一件事在两处长成
        两个样子本身就是毛病，何况对话框那一份已经在用了。"""
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            return
        dialog = CommentDialog(str(self.datas.get("comment") or ""), self.window())
        if not dialog.exec():
            return
        self.__write_comment(dialog.comment())

    @Slot(str)
    def __on_comment_saved(self, comment: str) -> None:
        """内联编辑页的保存。没有可写的 flow 时把编辑页拉回已存基线。"""
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            self.comment_pane.mark_saved(str(self.datas.get("comment") or ""))
            return
        self.__write_comment(comment)

    def __write_comment(self, comment: str) -> None:
        flow_id = self.datas.get("id", "")
        if not self.controller or not flow_id:
            return
        try:
            self.controller.set_flow_comment(flow_id, comment)
        except (AttributeError, ValueError, RuntimeError) as exc:
            show_warning(self.tr("备注保存失败"), str(exc), self.window())
            return
        show_success(self.tr("成功"), self.tr("备注已保存"), self.window())
        self.__store("comment", comment)
        self.comment_pane.mark_saved(comment)

    def __store(self, key: str, value: str) -> None:
        """写回成功后就地更新缓存的详情并只重画概览卡片。

        刻意不走整个 `set_data`：那会连消息页一起重建，把 WS 帧表的选中行和滚动位置
        清掉 —— 帧还在一秒几十条地进来，改个标记就把人看的位置弄丢说不过去。
        重新问一趟 `flow_detail` 也没必要，改的就是这一个字段。"""
        self.datas[key] = value
        self.overview.set_data(self.datas)

    def __set_mark_checked(self, checked: bool) -> None:
        """摆按钮的勾选态，且不触发写回。

        `toggled` 对 `setChecked` 和真人点击一样会发 —— 不拦一道，光是切换选中的
        流量就会把「上一条的标记」写到刚选中的那条上去。

        用一个哨兵位而不是 `blockSignals`：菜单动作的勾选态还要被 Fluento 的
        action→控件同步链路照搬，掐掉 action 的信号等于让控件一直画着上一条
        流量的样子。"""
        self.__syncing_mark = True
        try:
            self.mark_action.setChecked(checked)
        finally:
            self.__syncing_mark = False

    # —— WebSocket / SSE 实时 ——

    def __is_current(self, flow_id: str) -> bool:
        """这条信号说的是不是面板上正显示的那一条。

        信号是广播的：抓包时几十条 WS 连接同时在推帧，不过滤等于把所有连接的帧混进
        同一张表。"""
        return bool(flow_id) and flow_id == self.datas.get("id")

    @Slot(str)
    def __on_ws_started(self, flow_id: str) -> None:
        """握手成功。选中时还是普通 HTTP 流量（消息栏藏着）的那一条，从这里开始有帧。"""
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

    @Slot(str)
    def __on_sse_started(self, flow_id: str) -> None:
        """检测出事件流。选中时还是普通 HTTP 流量的那一条，从这里开始消息栏有内容。"""
        if not self.__is_current(flow_id):
            return
        self.messages.show_sse_events(self.__events(flow_id))
        self.__refresh_message_page()

    @Slot(str, object)
    def __on_sse_event(self, flow_id: str, event: SseEvent) -> None:
        if not self.__is_current(flow_id):
            return
        self.messages.append_event(event)
        self.__refresh_message_page()

    @Slot(str)
    def __on_sse_ended(self, flow_id: str) -> None:
        """流末：body 已补回，响应体页该从「还在流」换成最终内容 —— 重拉一次详情。"""
        if not self.__is_current(flow_id):
            return
        detail = self.controller.flow_detail(flow_id) if self.controller else {}
        if detail:
            self.set_data(detail)

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

    def __events(self, flow_id: str) -> list[SseEvent]:
        """向 controller 要 SSE 事件存档。

        只读的会话 controller **刻意**不提供这个方法（历史流量由消息页兑底解
        body，见 `protocols.py` 的注释），所以缺方法走 `getattr` 探 —— 静默回
        空表，别把设计如此的情况当天天报的 warning。真出错（内核没在跑等）才
        留一行日志。
        """
        fetch = (
            getattr(self.controller, "sse_events", None) if self.controller else None
        )
        if not flow_id or fetch is None:
            return []
        try:
            return fetch(flow_id)
        except (AttributeError, RuntimeError) as exc:
            log.warning("读取 SSE 事件失败 flow_id=%s: %s", flow_id, exc)
            return []

    def __set_messages(self, data: dict) -> None:
        """填消息栏。

        帧要现取：详情字典是在 mitm 线程上一次性构建的（`core/mitm/detail.py`），
        塞进去上千帧等于让每一条流量都背着一份帧列表过界，而九成流量压根不是 WS。
        SSE 那一路同形：事件在 `FerretSseAddon` 的存档里，问 controller 整取；
        取不到的（从 `.flow` 文件回来的历史流量）由 `MessagesPane` 兑底解 body。"""
        flow_id = str(data.get("id") or "")
        if is_websocket(data):
            self.messages.set_data(data, self.__frames(flow_id), self.__close(flow_id))
        else:
            self.messages.set_data(data, events=self.__events(flow_id))
        self.__refresh_message_page()

    def __refresh_message_page(self) -> None:
        """消息栏的可见性与计数徽标 —— 换流量、新帧到达都要过这里。"""
        self.res_pane.setTabVisible("Messages", self.messages.applicable)
        count = self.messages.count
        if not self.messages.applicable or not count:
            self.message_badge.hide()
            return
        self.message_badge.setText(str(count))
        self.message_badge.adjustSize()
        # 徽标变宽不触发目标 Resize / Move，`PivotBadgeAnchor` 感知不到，得手动补一次。
        self.message_badge.manager.reposition()
        self.message_badge.show()

    # —— 数据 ——

    def set_controller(self, controller) -> None:
        """更新 Flow 查看控制器并同步到响应栏。"""
        if controller is self.controller:
            return
        self.__connect_controller(self.controller, connect=False)
        self.controller = controller
        self.res_pane.controller = controller
        self.__connect_controller(controller)

    def set_data(self, data: dict):
        """有数据时调用，切换到详情页并填充

        Args:
            data: 数据字典
        """
        self.datas = data
        self.overview.set_data(data)

        # 左栏：请求 + flow 级信息
        headers = data.get("Request Headers", {})
        self.req_headers.set_items(headers)
        self.req_body.set_data(data, "Request")
        self.req_body_badge.setText(str(data.get("Request Body View", "")))
        self.req_body_badge.setVisible(bool(data.get("Request Body View", "")))
        self.req_body_badge.adjustSize()
        _fill_raw(
            self.req_raw,
            data,
            "Request",
            _request_start_line(data),
            self.controller,
            "get_raw_request",
        )
        query = data.get("Request Params", {})
        self.query_widget.set_items(query)
        cookies = data.get("Request Cookies", {})
        self.cookie_widget.set_cookies(cookies)
        self.req_tabs.setTabText(
            "Headers", self.tr("请求头 ({count})").format(count=len(headers))
        )
        self.req_tabs.setTabText(
            "Query", self.tr("查询参数 ({count})").format(count=len(query))
        )
        self.req_tabs.setTabText(
            "Cookies", self.tr("Cookie ({count})").format(count=len(cookies))
        )
        self.req_tabs.setTabVisible("Query", bool(query))
        self.req_tabs.setTabVisible("Cookies", bool(cookies))

        # 右栏：响应 + 消息
        self.res_pane.set_data(data)
        self.__set_messages(data)

        # 备注
        self.comment_pane.set_data(data)
        editable = bool(self.controller and data.get("id"))
        self.comment_pane.set_read_only(
            not (self.capabilities.can_comment and editable)
        )

        # 「…」动作的可用性跟着这条流量走。
        self.copy_url_action.setEnabled(bool(data.get("URL")))
        self.replay_action.setEnabled(editable)
        self.mark_action.setEnabled(editable)
        self.comment_action.setEnabled(editable)
        self.__set_mark_checked(bool(data.get("marked")))

        self.__sync_response_pane(data)
        self.__sync_close_host()
        self.stack.setCurrentIndex(1)
