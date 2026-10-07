"""详情面板：左右分栏（请求区 | 响应区），各一排扁平标签。

改造前是「一层导航：概览/请求/响应/消息/原始状态」+ 请求/响应页内的次级
Pivot（Headers/Query/Form/…/Raw），两层导航点两次才到一份报文头。现在回到
左右分栏：左栏是请求区（总览/原始/请求头/请求体/查询参数/Cookies/备注），
右栏是响应区（原始/响应头/响应体/消息）。「时序」是总览里的一张卡：瀑布块
（见 `timing.py`）作为 lead 挂在组头之下、时刻行之上，与原「耗时」组同栖。
全局布局设置说的是
「表格 vs 详情」的排布，内层分栏与它相反（`inverted=True`）才放得下：全局横向
（详情窄而高）时内层上下排，全局纵向（详情宽而矮）时内层左右排，比例在切换时
保持（`OrientationSplitter` 自己处理）。

「消息」栏只对 WebSocket 与 SSE 流量出现（见 `messages.py`），其余一律整条
隐藏 —— 普通请求点进去只会看到一张空表。请求体 / 查询参数 / Cookie 同理：
没有内容就整条隐藏（`set_data` 里按摘要字段现算显隐）。

没有响应的流量（只抓到请求）右栏整体隐藏；× 收起整个详情面板，横向时挂在
右栏标签行右端、纵向时挂在上栏（请求区）标签行右端 —— 永远贴着离表格最远的
那条边。

`ResponsePane` 除了做详情面板的右栏，还被 compose 页整个复用（那边的响应
展示复用响应头与响应体，不构造 Raw 页）。

表格、外层分割器、空状态仍留在 `views.py`；`FlowViewerPane` 首次展开详情时
才导入本模块并创建 `FlowDataPanel`。

标签先注册工厂；有 HTTP 摘要时只激活两栏的当前页，隐藏页在换流量后留到
再次打开才填充。Body、Raw、Messages 分别取自己的数据，空页与连接摘要页
不会创建 HTTP 标签内容；读取可选控件属性本身也不会触发构造。
"""

from __future__ import annotations

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
    CaptionLabel,
    FluentIcon,
    InfoBadge,
    InfoLevel,
    SimpleCardWidget,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    TreeWidget,
)

from ferret.apps.common.edit import (
    ItemDualPanel,
    JsonDualPanel,
    Language,
    ToolPlainTextEdit,
)
from ferret.apps.common.flow.fields import OverviewPane, _decoded_size
from ferret.apps.common.flow.messages import (
    MessagesPane,
    is_websocket,
    looks_like_json,
)
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.flow.timing import TimingPane
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import OrientationSplitter
from ferret.core.log import get_logger
from ferret.core.mitm import SseEvent, WsClose, WsFrame, is_event_stream, parse_sse
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

_OVERVIEW_DECODED_SIZES = (
    _decoded_size("req_wire_size", "req_decoded_size"),
    _decoded_size("res_wire_size", "res_decoded_size"),
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


def _load_body(data: dict, prefix: str, controller) -> None:
    """完整详情可直接消费；摘要缺正文时才跨线程取当前一侧。"""
    if f"{prefix} Body Text" in data:
        return
    flow_id = str(data.get("id") or data.get("Flow ID") or "")
    fetch = getattr(controller, "flow_body", None)
    if not flow_id or fetch is None:
        return
    try:
        data.update(fetch(flow_id, prefix))
    except (AttributeError, ValueError, TypeError, RuntimeError) as exc:
        log.warning("failed to read %s body flow_id=%s: %s", prefix, flow_id, exc)


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
    flow_id = str((datas or {}).get("Flow ID") or (datas or {}).get("id") or "")
    if controller and flow_id:
        preview = getattr(controller, f"{getter}_preview", None)
        if preview is not None:
            try:
                result = preview(flow_id)
                edit.set_text(
                    str(result.get("text") or ""),
                    notice=str(result.get("notice") or ""),
                )
            except (AttributeError, ValueError, TypeError, RuntimeError) as exc:
                log.warning("failed to read the raw HTTP preview: %s", exc)
                edit.set_text("")
            return
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
        edit.set_text("")
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
    edit.set_text(
        "\n".join(raw_lines), notice=str(datas.get(f"{prefix} Body Notice") or "")
    )


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
        self.notice = CaptionLabel(self.json_panel)
        self.notice.setWordWrap(True)
        self.notice.hide()
        self.json_panel.main_layout.insertWidget(0, self.notice)
        self.addWidget(self.json_panel)

        self.form_panel: ItemDualPanel | None = None
        if allow_form:
            self.form_panel = ItemDualPanel()
            self.form_panel.set_read_only(True)
            self.addWidget(self.form_panel)

        self.empty_label = SubtitleLabel(self.tr("无任何数据"))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.addWidget(self.empty_label)

    def clear(self) -> None:
        """保留懒创建的控件，释放已失效的正文、树、表单和查找游标。"""
        self.json_panel.set_text("")
        if self.form_panel is not None:
            self.form_panel.set_items({})
        self.notice.clear()
        self.notice.hide()
        self.empty_label.setText(self.tr("无任何数据"))
        self.setCurrentWidget(self.empty_label)

    def set_data(self, data: dict, prefix: str) -> None:
        """按详情字典切页：表单优先，其次报文体文本，最后空占位。"""
        text = data.get(f"{prefix} Body Pretty")
        if text is None:
            text = data.get(f"{prefix} Body Text") or ""
        notice = str(data.get(f"{prefix} Body Notice") or "")
        self.notice.setText(notice)
        self.notice.setVisible(bool(notice))
        if self.form_panel is not None:
            form = data.get(f"{prefix} Form") or {}
            if form:
                self.json_panel.set_text("")
                self.form_panel.set_items(form)
                self.setCurrentWidget(self.form_panel)
                return
            self.form_panel.set_items({})
        if text:
            lang = _body_lang(data.get(f"{prefix} Body Syntax", "none"), str(text))
            self.json_panel.set_text(str(text), lang=lang)
            self.setCurrentWidget(self.json_panel)
        else:
            self.json_panel.set_text("")
            self.empty_label.setText(notice or self.tr("无任何数据"))
            self.setCurrentWidget(self.empty_label)


class CommentPane(QWidget):
    """备注内联编辑页：直接编辑，脏了才亮保存。

    改造前备注只有一个弹窗入口（CommandBar 的 Comment 按钮）；现在这页常驻
    左栏，弹窗入口在表格右键菜单 —— 两处共用一套写回（面板接到
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

    详情面板的右栏，也被 compose 页复用。启用 Raw 但没有 controller 时，
    原始报文走详情字典手工拼装的兜底。

    `with_raw=False` 时不创建或填充 Raw 编辑器（compose 不需要原始报文兜底，
    那边用 响应头/响应体/性能 三条标签）。宿主仍可用 `addTab` 往里追加自己的
    标签（compose 把「性能」追加在末位）。

    关闭按钮默认藏：compose 那路不需要 ×；详情面板里由 `FlowDataPanel` 按
    分栏方向决定它显不显示。
    """

    PREFIX = "Response"
    bodyLoaded = Signal()

    def __init__(self, parent=None, controller=None, *, with_raw: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.datas: dict | None = None
        self._active = False
        self._loading_data = False
        self._dirty: set[str] = set()
        self.setTabFontSize(12)
        self.header_card: ItemDualPanel | None = None
        self.body_pane: BodyPane | None = None
        self.raw_edit: ToolPlainTextEdit | None = None
        if with_raw:
            self.addLazyTab("Raw", self.__create_raw, self.tr("原始"))
        self.addLazyTab("Headers", self.__create_headers, self.tr("响应头"))
        self.addLazyTab("Body", self.__create_body, self.tr("响应体"))
        # body 视图名（JSON / gRPC…）不再展示成标签行徽标（用户反馈摘除）：
        # 视图类型看 body 语法高亮即可，标签栏保持只有标签与动作区。

        self.close_button.hide()
        self.currentChanged.connect(self.__populate_current)

    def __create_raw(self) -> ToolPlainTextEdit:
        self.raw_edit = ToolPlainTextEdit()
        self.raw_edit.set_read_only(True)
        return self.raw_edit

    def __create_headers(self) -> ItemDualPanel:
        self.header_card = ItemDualPanel()
        self.header_card.set_read_only(True)
        return self.header_card

    def __create_body(self) -> BodyPane:
        self.body_pane = BodyPane(allow_form=False)
        return self.body_pane

    def set_data(self, data: dict, *, active: bool = True) -> None:
        """换流量先释放旧页载荷，当前响应页可见时才填入新内容。"""
        previous = self.datas or {}
        if (
            not data
            or data.get("id") != previous.get("id")
            or data.get("Flow ID") != previous.get("Flow ID")
            or (not data.get("id") and data is not self.datas)
        ):
            if self.raw_edit is not None:
                self.raw_edit.set_text("")
            if self.header_card is not None:
                self.header_card.set_items({})
            if self.body_pane is not None:
                self.body_pane.clear()
        elif data.get("res_wire_size") == 0 and self.body_pane is not None:
            self.body_pane.clear()
        self.datas = data
        self._active = active
        self._dirty = {"Raw", "Headers", "Body"}
        headers = data.get("Response Headers", {})
        self.setTabText(
            "Headers", self.tr("响应头 ({count})").format(count=len(headers))
        )
        if active:
            self._loading_data = True
            try:
                self.activateCurrentTab()
            finally:
                self._loading_data = False
            self.__populate_current()

    def __populate_current(self, _index: int = -1) -> None:
        key = self.pivot.currentRouteKey()
        if self._loading_data or not self._active or key not in self._dirty:
            return
        data = self.datas
        if data is None:
            return
        self._dirty.discard(key)
        if key == "Headers" and self.header_card is not None:
            self.header_card.set_items(data.get("Response Headers", {}))
        elif key == "Body" and self.body_pane is not None:
            _load_body(data, self.PREFIX, self.controller)
            self.body_pane.set_data(data, self.PREFIX)
            self.bodyLoaded.emit()
        elif key == "Raw" and self.raw_edit is not None:
            _fill_raw(
                self.raw_edit,
                data,
                self.PREFIX,
                _response_start_line(data),
                self.controller,
                "get_raw_response",
            )


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
        self._setting_data = False
        self._req_dirty: set[str] = set()
        self._overview_decoded_sizes: tuple[str | None, ...] | None = None
        self._messages_dirty = True
        self._message_kind = ""
        self._message_count: int | None = 0
        self._message_count_exact = True
        self._message_last_index = -1
        self._messages_flow_id: str | None = None
        self._rendered_message_kind = ""
        self._rendered_message_index = -1
        self._rendered_close = WsClose()
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

        self.timing_pane: TimingPane | None = None
        self.overview: OverviewPane | None = None
        self.req_raw: ToolPlainTextEdit | None = None
        self.req_headers: ItemDualPanel | None = None
        self.req_body: BodyPane | None = None
        self.query_widget: ItemDualPanel | None = None
        self.cookie_widget: CookieWidget | None = None
        self.cookie_card: SimpleCardWidget | None = None
        self.comment_pane: CommentPane | None = None

        self.req_tabs.addLazyTab("Overview", self.__create_overview, self.tr("概览"))
        self.req_tabs.addLazyTab("Raw", self.__create_req_raw, self.tr("原始"))
        self.req_tabs.addLazyTab(
            "Headers", self.__create_req_headers, self.tr("请求头")
        )
        self.req_tabs.addLazyTab("Body", self.__create_req_body, self.tr("请求体"))
        self.req_tabs.addLazyTab("Query", self.__create_query, self.tr("查询参数"))
        self.req_tabs.addLazyTab("Cookies", self.__create_cookies, self.tr("Cookie"))
        self.req_tabs.addLazyTab("Comment", self.__create_comment, self.tr("备注"))

        # —— 右栏（响应区）：「消息」由本面板追加 ——
        self.res_pane = ResponsePane(controller=self.controller)
        self.messages: MessagesPane | None = None
        self.res_pane.addLazyTab("Messages", self.__create_messages, self.tr("消息"))
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

        # 「…」动作菜单已撤（复制 URL / 重发 / 备注）：三者的常驻入口都在表格
        # 右键菜单（URL 查看、重放、备注弹窗），面板内备注另有内联编辑页承担
        # 日常编辑，走同一条写回通道。标记同理只在表格右键（docs/design.md#ui）。

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

        # —— 连接摘要页（连接树父节点选中时显示，只读字段表）——
        self.conn_page = QWidget()
        self.conn_title = SubtitleLabel(self.conn_page)
        self.conn_title.setText(self.tr("连接摘要"))
        self.conn_close_button = TransparentToolButton(self.conn_page)
        self.conn_close_button.setIcon(FluentIcon.CLOSE)
        self.conn_fields = ItemDualPanel()
        self.conn_fields.set_read_only(True)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty_page)  # index 0
        self.stack.addWidget(self.detail_page)  # index 1
        self.stack.addWidget(self.conn_page)  # index 2：连接摘要

    def __create_overview(self) -> OverviewPane:
        self.timing_pane = TimingPane()
        self.overview = OverviewPane(lead_after="时序", lead=self.timing_pane)
        return self.overview

    def __create_req_raw(self) -> ToolPlainTextEdit:
        self.req_raw = ToolPlainTextEdit()
        self.req_raw.set_read_only(True)
        return self.req_raw

    def __create_req_headers(self) -> ItemDualPanel:
        self.req_headers = ItemDualPanel()
        self.req_headers.set_read_only(True)
        return self.req_headers

    def __create_req_body(self) -> BodyPane:
        self.req_body = BodyPane(allow_form=True)
        return self.req_body

    def __create_query(self) -> ItemDualPanel:
        self.query_widget = ItemDualPanel()
        self.query_widget.set_read_only(True)
        return self.query_widget

    def __create_cookies(self) -> SimpleCardWidget:
        self.cookie_widget = CookieWidget()
        self.cookie_card = SimpleCardWidget()
        self.cookie_card.setBorderRadius(0)
        layout = QVBoxLayout(self.cookie_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.cookie_widget)
        return self.cookie_card

    def __create_comment(self) -> CommentPane:
        self.comment_pane = CommentPane()
        self.comment_pane.commentSaved.connect(self.__on_comment_saved)
        return self.comment_pane

    def __create_messages(self) -> MessagesPane:
        self.messages = MessagesPane()
        return self.messages

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

        # 连接摘要页布局：顶部（标题 + 弹簧 + X）+ 字段表
        conn_layout = QVBoxLayout(self.conn_page)
        conn_layout.setContentsMargins(8, 4, 8, 8)
        conn_layout.setSpacing(6)
        conn_top = QHBoxLayout()
        conn_top.addWidget(self.conn_title)
        conn_top.addStretch(1)
        conn_top.addWidget(self.conn_close_button)
        conn_layout.addLayout(conn_top)
        conn_layout.addWidget(self.conn_fields, 1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.empty_close_button.clicked.connect(self.__collapse_panel)
        self.conn_close_button.clicked.connect(self.__collapse_panel)
        self.req_tabs.close_button.clicked.connect(self.__collapse_panel)
        self.res_pane.close_button.clicked.connect(self.__collapse_panel)
        self.req_tabs.currentChanged.connect(self.__populate_request)
        self.res_pane.currentChanged.connect(self.__populate_messages)
        self.res_pane.bodyLoaded.connect(self.__body_loaded)
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
            "messages_changed": self.__on_messages_changed,
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
            QTimer.singleShot(0, self.splitter, self.splitter.set_equal_sizes)
        self.__populate_messages()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 外层先设数据，再在下一拍展开 splitter；消息页等尺寸恢复后补读快照。
        self.__populate_messages()

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

    # —— 备注 ——

    @Slot(str)
    def __on_comment_saved(self, comment: str) -> None:
        """内联编辑页的保存。没有可写的 flow 时把编辑页拉回已存基线。"""
        if self.comment_pane is None:
            return
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
        if self.comment_pane is not None:
            self.comment_pane.mark_saved(comment)

    def __store(self, key: str, value: str) -> None:
        """写回成功后就地更新详情缓存，概览可见时才重画卡片。

        刻意不走整个 `set_data`：那会连消息页一起重建，把 WS 帧表的选中行和滚动位置
        清掉 —— 帧还在一秒几十条地进来，改个标记就把人看的位置弄丢说不过去。
        重新问一趟 `flow_detail` 也没必要，改的就是这一个字段。"""
        self.datas[key] = value
        self._req_dirty.add("Overview")
        self.__populate_request()

    # —— WebSocket / SSE 实时 ——

    def __is_current(self, flow_id: str) -> bool:
        """这条信号说的是不是面板上正显示的那一条。

        信号是广播的：抓包时几十条 WS 连接同时在推帧，不过滤等于把所有连接的帧混进
        同一张表。"""
        return bool(flow_id) and flow_id == self.datas.get("id")

    @Slot(str, str, int)
    def __on_messages_changed(self, flow_id: str, kind: str, count: int) -> None:
        if not self.__is_current(flow_id):
            return
        self._message_kind = kind
        self._message_count = max(self._message_count or 0, count)
        self._message_count_exact = True
        self._message_last_index = max(self._message_last_index, count - 1)
        self._messages_dirty = True
        self.__refresh_message_page()
        self.__populate_messages()

    @Slot(str)
    def __on_ws_started(self, flow_id: str) -> None:
        """握手成功。选中时还是普通 HTTP 流量（消息栏藏着）的那一条，从这里开始有帧。"""
        if not self.__is_current(flow_id):
            return
        self._message_kind = "websocket"
        self._messages_dirty = True
        self.__refresh_message_page()
        self.__populate_messages()

    @Slot(str, object)
    def __on_ws_frame(self, flow_id: str, frame: WsFrame) -> None:
        if not self.__is_current(flow_id):
            return
        self.__receive_message("websocket", frame)

    @Slot(str, object)
    def __on_ws_closed(self, flow_id: str, close: WsClose) -> None:
        if not self.__is_current(flow_id):
            return
        if (
            self.__messages_visible()
            and not self._messages_dirty
            and self.messages is not None
        ):
            if close != self._rendered_close:
                self.messages.set_close(close)
                self._rendered_close = close
        else:
            self._messages_dirty = True

    @Slot(str)
    def __on_sse_started(self, flow_id: str) -> None:
        """检测出事件流。选中时还是普通 HTTP 流量的那一条，从这里开始消息栏有内容。"""
        if not self.__is_current(flow_id):
            return
        self._message_kind = "sse"
        self._messages_dirty = True
        self.__refresh_message_page()
        self.__populate_messages()

    @Slot(str, object)
    def __on_sse_event(self, flow_id: str, event: SseEvent) -> None:
        if not self.__is_current(flow_id):
            return
        self.__receive_message("sse", event)

    @Slot(str)
    def __on_sse_ended(self, flow_id: str) -> None:
        """流末只刷新摘要；可见正文页按需取最终内容。"""
        if not self.__is_current(flow_id):
            return
        fetch = getattr(self.controller, "flow_summary", None)
        detail = fetch(flow_id) if fetch is not None else {}
        if detail:
            self.set_data(detail)

    def __messages_visible(self) -> bool:
        return (
            self.isVisible()
            and not self.size().isEmpty()
            and self.stack.currentWidget() is self.detail_page
            and not self.res_pane.isHidden()
            and self.res_pane.pivot.currentRouteKey() == "Messages"
        )

    def __receive_message(self, kind: str, item: WsFrame | SseEvent) -> None:
        # 快照可能已经含有尚在 Qt 队列中的信号，序号水位防止重复追加/计数。
        if item.index <= self._message_last_index:
            return
        self._message_last_index = item.index
        self._message_kind = kind
        self._message_count = max(self._message_count or 0, item.index + 1)
        self._message_count_exact = True
        if (
            self.__messages_visible()
            and not self._messages_dirty
            and self.messages is not None
        ):
            # 合并桥接通知可能跳过中间序号；从有界存档补齐，不能当成连续追加。
            if item.index > self._rendered_message_index + 1:
                self._messages_dirty = True
                self.__populate_messages()
                self.__refresh_message_page()
                return
            if isinstance(item, WsFrame):
                self.messages.append_frame(item)
            else:
                self.messages.append_event(item)
            self.messages.set_count(self._message_count)
            self._rendered_message_index = item.index
        else:
            self._messages_dirty = True
        self.__refresh_message_page()

    def __message_snapshot(self, flow_id: str) -> dict:
        fetch = getattr(self.controller, "flow_messages", None)
        if fetch is not None:
            try:
                return fetch(flow_id)
            except (AttributeError, ValueError, TypeError, RuntimeError) as exc:
                log.warning("failed to read messages flow_id=%s: %s", flow_id, exc)
                return {}
        # 独立使用面板且传入完整详情的宿主沿用原接口；也仅在消息页打开时取。
        if self._message_kind == "websocket":
            frames = self.__frames(flow_id)
            return {
                "kind": "websocket",
                "frames": frames,
                "close": self.__close(flow_id),
                "count": len(frames),
            }
        events = self.__events(flow_id)
        if not events:
            events = parse_sse(str(self.datas.get("Response Body Text") or ""))
        return {"kind": "sse", "events": events, "count": len(events)}

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

    def __populate_messages(self, _index: int = -1) -> None:
        if (
            self._setting_data
            or not self._messages_dirty
            or not self.__messages_visible()
        ):
            return
        self.res_pane.activateCurrentTab()
        if self.messages is None or not self._messages_dirty:
            return
        self._messages_dirty = False
        flow_id = str(self.datas.get("id") or "")
        snapshot = self.__message_snapshot(flow_id)
        if not snapshot:
            self._messages_dirty = True
            return
        self._message_kind = snapshot.get("kind") or self._message_kind
        websocket = self._message_kind == "websocket"
        items = snapshot.get("frames" if websocket else "events", [])
        close = snapshot.get("close", WsClose())
        same_flow = (
            self._messages_flow_id == flow_id
            and self._rendered_message_kind == self._message_kind
        )
        if same_flow:
            # 隐藏期间只更新了收件水位；补齐尚未渲染的条目，保留过滤、清空和滚动。
            # 已清空的旧条目仍在快照里，但不能重新加入显示。
            for item in items:
                if item.index > self._rendered_message_index:
                    if websocket:
                        self.messages.append_frame(item)
                    else:
                        self.messages.append_event(item)
            if websocket and close != self._rendered_close:
                self.messages.set_close(close)
        else:
            if websocket:
                self.messages.show_websocket(items, close)
            else:
                self.messages.show_sse_events(items)
            self._messages_flow_id = flow_id
            self._rendered_message_kind = self._message_kind
            self._rendered_message_index = -1
        self._rendered_close = close
        self._rendered_message_index = max(
            self._rendered_message_index,
            max((item.index for item in items), default=-1),
        )
        self._message_count = snapshot.get("count", len(items))
        self._message_count_exact = bool(snapshot.get("count_exact", True))
        self.messages.set_count(self._message_count)
        self.messages.set_notice(str(snapshot.get("notice") or ""))
        self._message_last_index = max(
            self._message_last_index, self._rendered_message_index
        )
        self.__refresh_message_page()

    def __refresh_message_page(self) -> None:
        """消息栏的可见性与计数徽标 —— 换流量、新帧到达都要过这里。"""
        applicable = bool(self._message_kind)
        self.res_pane.setTabVisible("Messages", applicable)
        count = self._message_count
        if not applicable or not count:
            self.message_badge.hide()
            return
        self.message_badge.setText(
            str(count) if self._message_count_exact else f"≥{count}"
        )
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
        self.set_data({})

    def set_data(self, data: dict):
        """摘要驱动导航与计数；只填充两栏当前页，其余页留到首次打开。"""
        if (
            not data
            or data.get("id") != self.datas.get("id")
            or data.get("kind") != self.datas.get("kind")
        ):
            self.__release_payloads()
            self._messages_flow_id = None
        elif data.get("req_wire_size") == 0 and self.req_body is not None:
            self.req_body.clear()
        self.datas = data
        self._req_dirty = set(self.req_tabs.pivot.items)
        self._messages_dirty = True
        self._message_last_index = -1
        if not data:
            self.res_pane.set_data(data, active=False)
            self.stack.setCurrentWidget(self.empty_page)
            return
        # 连接树父节点：走连接摘要页（只读字段表），不进逐流详情。
        if data.get("kind") == "connection":
            self.res_pane.set_data(data, active=False)
            self.__set_connection_data(data)
            return
        self._setting_data = True
        # 隐藏 Messages 会自动跳到 Raw，先撤下旧数据的填充闸门，避免读取上一条流。
        self.res_pane.set_data(data, active=False)
        headers = data.get("Request Headers", {})
        query = data.get("Request Params", {})
        cookies = data.get("Request Cookies", {})
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
        # 无请求体的流量（GET 最常见）整条藏掉「请求体」标签。有无 body 的口径
        # 与表格 Size 列一致：线上字节数（压缩后）为 0 即无 body（detail.py
        # 的 ``wire_size``）；拦截中的流量 body 到达后随 flow_updated 重进。
        self.req_tabs.setTabVisible("Body", bool(data.get("req_wire_size")))
        self._message_kind = str(data.get("message_kind") or "")
        if not self._message_kind:
            if is_websocket(data):
                self._message_kind = "websocket"
            elif is_event_stream(data.get("Response Content-Type")):
                self._message_kind = "sse"
        self._message_count = data.get("message_count")
        self._message_count_exact = True
        if self._message_count is None and self._message_kind == "websocket":
            raw_ws = (data.get("raw_state") or {}).get("websocket") or {}
            self._message_count = len(raw_ws.get("messages", []))
        if self._message_count is not None:
            self._message_last_index = self._message_count - 1
        self.__refresh_message_page()
        self.__sync_response_pane(data)
        self.__sync_close_host()
        self.stack.setCurrentWidget(self.detail_page)
        self.res_pane.set_data(data, active=not self.res_pane.isHidden())
        self.req_tabs.activateCurrentTab()
        self._setting_data = False
        self.__populate_request()
        self.__populate_messages()

    def __release_payloads(self) -> None:
        """切换流量/空页时清空已创建页面，避免隐藏页保留上一条大正文。"""
        if self.req_raw is not None:
            self.req_raw.set_text("")
        if self.req_body is not None:
            self.req_body.clear()
        for panel in (self.req_headers, self.query_widget, self.conn_fields):
            if panel is not None:
                panel.set_items({})
        if self.cookie_widget is not None:
            self.cookie_widget.set_cookies({})
        if self.comment_pane is not None:
            self.comment_pane.set_data({})
        if self.overview is not None:
            self.overview.set_data({})
        if self.timing_pane is not None:
            self.timing_pane.set_data({})
        if self.messages is not None:
            self.messages.set_data({})
        self._rendered_close = WsClose()
        self._rendered_message_index = -1
        self._rendered_message_kind = ""

    def __populate_request(self, _index: int = -1) -> None:
        key = self.req_tabs.pivot.currentRouteKey()
        if (
            self._setting_data
            or key not in self._req_dirty
            or self.stack.currentWidget() is not self.detail_page
        ):
            return
        self._req_dirty.discard(key)
        data = self.datas
        if key == "Overview" and self.overview is not None:
            if data.pop("overview_pending", False):
                fetch = getattr(self.controller, "flow_overview_metadata", None)
                if fetch is not None:
                    try:
                        data.update(fetch(str(data.get("id") or "")))
                    except (AttributeError, ValueError, TypeError, RuntimeError) as exc:
                        log.warning("failed to read overview metadata: %s", exc)
            self.overview.set_data(data)
            self._overview_decoded_sizes = tuple(
                read(data) for read in _OVERVIEW_DECODED_SIZES
            )
            if self.timing_pane is not None:
                self.timing_pane.set_data(data)
        elif key == "Raw" and self.req_raw is not None:
            _fill_raw(
                self.req_raw,
                data,
                "Request",
                _request_start_line(data),
                self.controller,
                "get_raw_request",
            )
        elif key == "Headers" and self.req_headers is not None:
            self.req_headers.set_items(data.get("Request Headers", {}))
        elif key == "Body" and self.req_body is not None:
            _load_body(data, "Request", self.controller)
            self.req_body.set_data(data, "Request")
            self.__body_loaded()
        elif key == "Query" and self.query_widget is not None:
            self.query_widget.set_items(data.get("Request Params", {}))
        elif key == "Cookies" and self.cookie_widget is not None:
            self.cookie_widget.set_cookies(data.get("Request Cookies", {}))
        elif key == "Comment" and self.comment_pane is not None:
            self.comment_pane.set_data(data)
            editable = bool(self.controller and data.get("id"))
            self.comment_pane.set_read_only(
                not (self.capabilities.can_comment and editable)
            )

    def __body_loaded(self) -> None:
        # 无压缩正文的「解压后」行仍隐藏，补出字节数不代表概览需要重新构建。
        # 只增加脏标记，不能抹掉换流量/备注更新等路径已经留下的标记。
        decoded_sizes = tuple(read(self.datas) for read in _OVERVIEW_DECODED_SIZES)
        if decoded_sizes != self._overview_decoded_sizes:
            self._req_dirty.add("Overview")
        self.__populate_request()

    def __set_connection_data(self, data: dict) -> None:
        """连接节点摘要：客户端 / 目标 / TLS / 流数 / 字节 / 起止，只读渲染。"""
        self.datas = data
        targets = data.get("targets") or []
        if len(targets) <= 1:
            target_text = targets[0] if targets else "—"
        else:
            target_text = self.tr("{} 个目标").format(len(targets))
        fields = {
            self.tr("客户端"): data.get("client") or "—",
            self.tr("目标"): target_text,
            self.tr("传输协议"): data.get("transport") or "—",
            self.tr("TLS 版本"): data.get("tls_version") or "—",
            self.tr("ALPN"): data.get("alpn") or "—",
            self.tr("SNI"): data.get("sni") or "—",
            self.tr("加密套件"): data.get("cipher") or "—",
            self.tr("流数"): str(data.get("flow_count", 0)),
            self.tr("传输字节"): data.get("size") or "—",
            self.tr("持续时间"): data.get("duration") or "—",
            self.tr("开始时间"): data.get("start") or "—",
            self.tr("结束时间"): data.get("end") or "—",
            self.tr("连接 ID"): data.get("conn_id") or "—",
        }
        self.conn_fields.set_items(fields)
        self.stack.setCurrentIndex(2)
