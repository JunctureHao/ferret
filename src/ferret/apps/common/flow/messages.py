"""消息页：WebSocket 帧与 SSE 事件的聊天式气泡流。

改造前这两种流量在详情面板里**根本看不见**：WS 流量只有 101 那一次握手的头，之后所有
帧都在 `flow.websocket` 里躺着没人读；SSE 响应则整份挤在 Body 那一页里，一堆
``data: {...}`` 连成一块文本，想看第三个事件只能自己数空行。

现在是「聊天气泡」而不是表格：每枚气泡就是一张只摆**消息内容**的 qfw 卡片
（`chat.Bubble`），方向不占字 —— 客户端→服务端靠右、服务端→客户端靠左；
SSE 事件一律靠左，纯注释块（心跳）收成居中的系统条；连接关闭也以系统条收尾。
长内容默认限高截断，点击气泡原地展开。

两种协议共用一条 :class:`~ferret.apps.common.flow.chat.ChatStream` 而不是各开一条：
它们回答的是同一个问题 ——「这条连接上来回传了些 什么」，而任何一条流量最多只可能
是其中一种。三态（气泡流 / 占位）由 `QStackedWidget` 切换，外面由详情面板决定整页
是否露面。

两侧的数据来路完全不同，这一点决定了 API 形状：

* **WS 帧是活的**。`flow.websocket` 由 mitmproxy 持续填充，帧要边到边追加，所以
  :meth:`MessagesPane.append_frame` 是增量接口 —— 整流重建会把用户的选中和滚动位置
  一起清掉，而行情型连接每秒几十帧，等于面板没法用。
* **SSE 事件也是活的**。`FerretSseAddon` 在 `responseheaders` 把
  `flow.response.stream` 换成 callable，边转发边增量解析，逐事件经信号推过来，走
  :meth:`MessagesPane.append_event` 追加；整取（选中时）与兑底（从 `.flow` 文件
  回来的历史流量，走 `parse_sse(body)`）两条路汇合到同一个 `show_sse_events`。

顶栏就三件事（整行靠右）：过滤框（大小写不敏感子串，WS 匹配帧文本 / 方向文字 /
BINARY 帧 hex，SSE 匹配 data 与事件名——方向词与事件元信息只进搜索串不进界面）、
正逆序切换（逆序时新气泡插顶）、清空显示（只清界面，内核帧数据不动，后续帧照常
追加）。

显示上限见 :data:`MESSAGE_ROW_LIMIT`：**截断只发生在这一层**，`ws_frames` 恒返回
全部帧；超限时静默从最旧一端逐出，`count`（标签徽标）恒指内核总数 —— 显示与计数
语义分开，各自有测试钉着。
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    TransparentToolButton,
)

from ferret.apps.common.flow.chat import Bubble, ChatStream, SystemNote
from ferret.core.mitm import (
    WS_FRAME_LIMIT,
    SseEvent,
    WsClose,
    WsFrame,
    is_event_stream,
    parse_sse,
)

# 气泡流最多摆多少条。和 :data:`ferret.core.mitm.WS_FRAME_LIMIT` 钉在同一个数
# ——它本来就是「界面侧显示上限」的那个值，SSE 事件没有理由另立一个。
MESSAGE_ROW_LIMIT = WS_FRAME_LIMIT

# 预览的字符上限（SSE data 单行预览用）。够看出这条事件是什么，又不会让几十 KB
# 的推流内容把搜索串撑爆。
PREVIEW_LIMIT = 160

# 二进制帧内容 / 搜索串取多少字节转十六进制（每字节占三个字符）。
PREVIEW_HEX_BYTES = 32

# 二进制气泡展开态 hex dump 最多铺多少字节。
HEX_DUMP_LIMIT = 4096

# 堆叠页序号。
_PAGE_PLACEHOLDER, _PAGE_STREAM = range(2)


def is_websocket(data: dict) -> bool:
    """详情字典描述的是不是一条 WebSocket 流量。

    判据是 `flow.websocket is not None`（原生状态树里的 ``websocket`` 子树）而不是
    「状态码是 101」：101 只说明握手**请求**被接受了，而 `flow.websocket` 是 mitmproxy
    真的架起 WS 层之后才填的，后者才对得上「有帧可看」。

    调用方是详情面板：它得先知道该不该向 controller 要帧，再决定这一页露不露面。
    """
    raw_state = data.get("raw_state")
    if not isinstance(raw_state, dict):
        return False
    return raw_state.get("websocket") is not None


def looks_like_json(text: str) -> bool:
    """`{` / `[` 开头就当 JSON。

    嗅探而不是试解析：一帧几百 KB 的推流内容 `json.loads` 一遍纯属浪费，而这里的问题
    只是「内容是什么形态」，猜错的代价是预览折行难看一点，不是显示错。
    """
    return text.lstrip()[:1] in ("{", "[")


def frame_time(timestamp: float | None) -> str:
    """帧时间戳 → ``HH:MM:SS.mmm``。

    气泡流里**没有时间**（顺序即时间），这个函数留给系统条上的关闭时刻用。

    时区走 `UTC` → `astimezone()` 这一趟而不是裸 `fromtimestamp`（同
    `models.py::_time_tooltip`）：mitmproxy 的时间戳是 Unix epoch，显式声明它是 UTC
    再换到本地，读起来才和抓包时钟对得上。
    """
    if not timestamp:
        return "-"
    local = datetime.fromtimestamp(timestamp, tz=UTC).astimezone()
    return local.strftime("%H:%M:%S.%f")[:-3]


def preview_text(text: str) -> str:
    """一行预览：连续空白折叠成单空格，超长截断。

    折叠空白是必须的：JSON 帧常常是格式化过的，直接塞进搜索串只能匹配到第一行。
    """
    line = " ".join(text.split())
    if len(line) > PREVIEW_LIMIT:
        return line[:PREVIEW_LIMIT] + "…"
    return line


def frame_preview(frame: WsFrame) -> str:
    """帧内容的预览文案：TEXT 帧按文本，其余按十六进制。"""
    if frame.is_text:
        return preview_text(frame.text())
    return preview_text(frame.content[:PREVIEW_HEX_BYTES].hex(" "))


def hex_dump(content: bytes, limit: int = HEX_DUMP_LIMIT) -> str:
    """二进制帧的 hex dump：``偏移  十六进制  |可打印字符|``，每行 16 字节。

    只铺前 `limit` 字节；被截掉这件事由调用方在界面上说出来（这里是纯函数，不该自己
    造一句待翻译的文案）。
    """
    lines = []
    clipped = content[:limit]
    for offset in range(0, len(clipped), 16):
        chunk = clipped[offset : offset + 16]
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {chunk.hex(' '):<47}  |{printable}|")
    return "\n".join(lines)


class MessagesPane(QWidget):
    """WS 帧 / SSE 事件共用的聊天气泡页。

    对外只有四件事：`set_data` 换一条流量、`append_frame` 追一帧、`set_close` 更新
    关闭信息、`applicable` / `count` 告诉详情面板这一页该不该露面、标签上挂几。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 显示中的条数，和流里的气泡 1:1 对齐（截断之后就不等于全部了，所以另存
        # `_total` —— 标签徽标恒指内核总数，与显示条数语义分开）。
        self._total = 0
        self._applicable = False
        # 三态分派要读它，而 append_* 可能先于任何 show_* 到达（选中时握手还没
        # 完成、第一帧/第一个事件却已经到了），构造期就得有值。
        self._mode = ""
        self._close = WsClose()
        self._close_note: SystemNote | None = None

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # —— 组件 ——

    def __init_widget(self) -> None:
        self.placeholder = BodyLabel(self)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)

        self.filter_input = LineEdit(self)
        self.filter_input.setFixedWidth(200)
        self.filter_input.setPlaceholderText(self.tr("过滤消息…"))
        self.filter_input.setClearButtonEnabled(True)

        self.sort_btn = TransparentToolButton(FluentIcon.DOWN, self)
        self.sort_btn.setCheckable(True)
        self.sort_btn.setFixedSize(28, 28)
        self.sort_btn.setIconSize(QSize(16, 16))
        self.sort_btn.setToolTip(self.tr("最新在上"))
        self.sort_btn.setAccessibleName(self.tr("切换消息顺序"))

        self.clear_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.clear_btn.setFixedSize(28, 28)
        self.clear_btn.setIconSize(QSize(16, 16))
        self.clear_btn.setToolTip(self.tr("清空显示的消息"))
        self.clear_btn.setAccessibleName(self.tr("清空显示的消息"))

        self.stream = ChatStream(self)

        # 占位页也要有一条顶栏是浪费，所以顶栏跟着气泡页走。
        self.stream_page = QWidget(self)
        self.pages = QStackedWidget(self)

    def __init_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        stream_layout = QVBoxLayout(self.stream_page)
        stream_layout.setContentsMargins(2, 2, 2, 0)
        stream_layout.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        toolbar.addStretch(1)
        toolbar.addWidget(self.filter_input)
        toolbar.addWidget(self.sort_btn)
        toolbar.addWidget(self.clear_btn)
        stream_layout.addLayout(toolbar)
        stream_layout.addWidget(self.stream, 1)

        self.pages.addWidget(self.placeholder)  # _PAGE_PLACEHOLDER
        self.pages.addWidget(self.stream_page)  # _PAGE_STREAM
        layout.addWidget(self.pages)

    def __connect_signal_to_slot(self) -> None:
        self.filter_input.textChanged.connect(self.stream.set_filter)
        self.sort_btn.toggled.connect(self.__on_order_toggled)
        self.clear_btn.clicked.connect(self.stream.clear)

    # —— 对外 ——

    @property
    def applicable(self) -> bool:
        """这一页有没有内容可看 —— 详情面板据此决定整页是否露面。

        SSE 开了 `stream` 时事件数是 0 但仍然 `True`：那条流量确实是事件流，只是 body
        没缓冲，「未缓冲」这句话本身就是要给的信息。
        """
        return self._applicable

    @property
    def count(self) -> int:
        """帧数 / 事件数的**总数**（不是显示条数）—— 标签上那枚徽标读它。"""
        return self._total

    def set_data(
        self,
        data: dict,
        frames: list[WsFrame] | None = None,
        close: WsClose | None = None,
        events: list[SseEvent] | None = None,
    ) -> None:
        """换一条流量。

        Args:
            data: 详情字典。判定走它（``Response Content-Type``）。
            frames: 已抓到的帧，由详情面板从 controller 取来（跨线程那一步归它）。
            close: 关闭信息，同上。
            events: addon 存档里的 SSE 事件，同上。为 None（历史流量）或空表时兑底
                解详情字典里的 body —— 从 `.flow` 文件加载的流量没有 addon 存档。
        """
        if is_websocket(data) or frames:
            self.show_websocket(frames or [], close or WsClose())
            return
        if is_event_stream(data.get("Response Content-Type")):
            if events:
                self.show_sse_events(events)
            else:
                self.show_sse_events(
                    parse_sse(str(data.get("Response Body Text") or ""))
                )
            return
        self.__reset()
        self._applicable = False
        self.placeholder.setText(self.tr("这条流量没有消息"))
        self.pages.setCurrentIndex(_PAGE_PLACEHOLDER)

    def show_websocket(self, frames: list[WsFrame], close: WsClose) -> None:
        """整流重建。选中期间新到的帧走 `append_frame`，不再走这里。"""
        self.__reset()
        self._applicable = True
        self._mode = "ws"
        self._total = len(frames)
        for frame in frames[-MESSAGE_ROW_LIMIT:]:
            self.stream.add(self.__frame_bubble(frame))
        self.__trim()
        self.set_close(close)
        self.pages.setCurrentIndex(_PAGE_STREAM)
        self.stream.scroll_to_newest()

    def show_sse_events(self, events: list[SseEvent]) -> None:
        """整取重建。选中期间新到的事件走 `append_event`，不再走这里。

        空列表不是「未缓冲」：tee 在流式转发也照样解析，空就是还没有事件到 ——
        摆一条空流，事件到了自然长出来。
        """
        self.__reset()
        self._applicable = True
        self._mode = "sse"
        self._total = len(events)
        for event in events[-MESSAGE_ROW_LIMIT:]:
            self.__add_event(event)
        self.pages.setCurrentIndex(_PAGE_STREAM)
        self.stream.scroll_to_newest()

    def append_event(self, event: SseEvent) -> None:
        """新到一个事件。语义对齐 `append_frame`：计数 +1、贴边才滚、超限从最旧端逐出。"""
        if self._mode != "sse":
            self.show_sse_events([])
        self._total += 1
        at_edge = self.stream.at_newest_edge()
        self.__add_event(event)
        self.__trim()
        if at_edge:
            self.stream.scroll_to_newest()

    def append_frame(self, frame: WsFrame) -> None:
        """新到一帧。

        只有已经贴着「最新」一端时才跟着滚：正翻看旧帧的人不该被每秒几十帧的推流
        拽走。超过上限时从最旧一端挤掉一条 —— 反过来（到上限就不再追加）看着像面板
        卡死了。
        """
        if self._mode != "ws":
            # 选中流量时握手还没完成（`raw_state` 里 `websocket` 仍是 None），
            # 帧却已经到了：这一帧本身就是「它是 WS」的证据。
            self.show_websocket([], self._close)
        self._total += 1
        at_edge = self.stream.at_newest_edge()
        self.stream.add(self.__frame_bubble(frame))
        self.__trim()
        if at_edge:
            self.stream.scroll_to_newest()

    def set_close(self, close: WsClose) -> None:
        """关闭信息：开着时什么都不摆，关了在流末尾（逆序时在顶部）落一条居中系统条。

        重复调用原地更新 —— `websocket_end` 信号可能晚于 `set_data` 到达。
        """
        self._close = close
        if self._close_note is not None:
            self._close_note.setParent(None)
            self._close_note.deleteLater()
            self._close_note = None
        if not close.is_closed:
            return
        self._close_note = SystemNote(self.__close_text(close), self.stream)
        self.stream.add_note(self._close_note)

    # —— 造气泡 ——

    def __frame_bubble(self, frame: WsFrame) -> Bubble:
        direction = (
            self.tr("客户端 → 服务端")
            if frame.from_client
            else self.tr("服务端 → 客户端")
        )
        if frame.is_text:
            content = frame.text()
        else:
            content = frame.content[:PREVIEW_HEX_BYTES].hex(" ")
        # 方向文字也进搜索串：输 "client" 只看上行帧，这是排查订阅流时最快的切法。
        # 界面上方向不占字 —— 靠左（发过来）右（发过去）对齐表达。
        bubble = Bubble(
            content,
            key=frame.index,
            search_text=f"{direction}\n{content}",
            align_right=frame.from_client,
        )
        # 二进制帧的展开态才是 hex dump：收起态那一行 hex 预览是给人认类型的，
        # 真要看内容必须铺成带偏移的 dump。
        if not frame.is_text:
            self.__bind_hex_expand(bubble, frame)
        return bubble

    def __bind_hex_expand(self, bubble: Bubble, frame: WsFrame) -> None:
        collapsed = frame.content[:PREVIEW_HEX_BYTES].hex(" ")

        def swap(_key: object) -> None:
            if bubble.is_expanded:
                dump = hex_dump(frame.content)
                if frame.size > HEX_DUMP_LIMIT:
                    note = self.tr("仅显示前 {} 字节，共 {} 字节")
                    dump = f"{dump}\n{note.format(HEX_DUMP_LIMIT, frame.size)}"
                bubble.set_content(dump)
            else:
                bubble.set_content(collapsed)

        bubble.activated.connect(swap)

    def __add_event(self, event: SseEvent) -> None:
        if not event.data and not event.event:
            # 纯注释块（心跳）不是任何一方说的话，收成居中系统条。
            self.stream.add_note(
                SystemNote(preview_text(f": {event.comment}"), self.stream)
            )
            return
        # 事件名/id/retry 进搜索串不进界面：气泡只摆数据本体。
        search = f"{event.event}\n{event.data}"
        if event.id:
            search += f"\nid: {event.id}"
        if event.retry is not None:
            search += f"\nretry: {event.retry}"
        bubble = Bubble(
            event.data,
            key=event.index,
            search_text=search,
            align_right=False,
        )
        self.stream.add(bubble)

    # —— 内部 ——

    def __reset(self) -> None:
        self._total = 0
        self._close = WsClose()
        self._close_note = None
        self._mode = ""
        self.stream.clear()
        self.filter_input.clear()

    def __trim(self) -> None:
        """超上限从最旧一端逐出。显示条数是界面策略，`count` 恒指内核总数。"""
        while len(self.stream.bubbles()) > MESSAGE_ROW_LIMIT:
            self.stream.evict_oldest()

    def __close_text(self, close: WsClose) -> str:
        if close.closed_by_client is None:
            parts = [self.tr("已关闭")]
        elif close.closed_by_client:
            parts = [self.tr("客户端关闭")]
        else:
            parts = [self.tr("服务端关闭")]
        if close.close_code is not None:
            parts.append(str(close.close_code))
        if close.close_reason:
            parts.append(close.close_reason)
        if close.timestamp_end:
            parts.append(frame_time(close.timestamp_end))
        return " · ".join(parts)

    # —— 槽 ——

    @Slot(bool)
    def __on_order_toggled(self, checked: bool) -> None:
        self.stream.set_descending(checked)
        # 图标跟着方向走：逆序（最新在上）朝上，正序朝下 —— 只换 tooltip 的话
        # 按钮看起来像没反应。
        self.sort_btn.setIcon(FluentIcon.UP if checked else FluentIcon.DOWN)
        tip = self.tr("最早在上") if checked else self.tr("最新在上")
        self.sort_btn.setToolTip(tip)
