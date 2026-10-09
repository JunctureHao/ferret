"""WebSocket / SSE 消息列表与按需详情，只消费跨线程的不可变快照。

追加不重建列表，点击后在同一区域打开正文，返回保留列表位置。显示窗口与内核总数分开，过滤和清空显示
不修改抓包数据；SSE 心跳与元信息块同样占一行，连接关闭信息单独展示。
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    SimpleCardWidget,
    TransparentToolButton,
)

from ferret.apps.common.button import TransparentTooltipButton
from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.flow.message_list import MessageList, MessageRow
from ferret.apps.common.icon import BaseIcon
from ferret.core.mitm import (
    WS_FRAME_LIMIT,
    SseEvent,
    WsClose,
    WsFrame,
    human,
    is_event_stream,
    opcode_name,
    parse_sse,
)

MESSAGE_ROW_LIMIT = WS_FRAME_LIMIT
PREVIEW_LIMIT = 160
PREVIEW_HEX_BYTES = 32
HEX_DUMP_LIMIT = 4096
_PAGE_PLACEHOLDER, _PAGE_STREAM = range(2)


def is_websocket(data: dict) -> bool:
    """详情字典描述的是不是一条 WebSocket 流量。

    摘要的 ``is_websocket`` 直接记录 `flow.websocket is not None`；完整详情还可
    从原生状态树里的 ``websocket`` 子树兼容读取。判据不是
    「状态码是 101」：101 只说明握手**请求**被接受了，而 `flow.websocket` 是 mitmproxy
    真的架起 WS 层之后才填的，后者才对得上「有帧可看」。

    调用方是详情面板：它得先知道该不该向 controller 要帧，再决定这一页露不露面。
    """
    if "is_websocket" in data:
        return bool(data["is_websocket"])
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

    折叠多行 JSON 与控制字符，保持固定行高；详情与搜索仍使用原始文本。
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


class MessagesPane(SimpleCardWidget):
    """消息列表与详情；内核总数独立于过滤、清空和显示上限。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._total = 0
        self._applicable = False
        self._mode = ""
        self._close = WsClose()
        self._selected: MessageRow | None = None
        self.__init_widget()
        self.__init_layout()
        self.filter_input.textChanged.connect(self.__on_filter_changed)
        self.sort_btn.toggled.connect(self.__on_order_toggled)
        self.clear_btn.clicked.connect(self.__clear_display)
        self.message_list.messageSelected.connect(self.__on_selected)
        self.message_list.messageActivated.connect(self.__open_message)
        self.back_btn.clicked.connect(self.__back_to_list)
        self.wrap_btn.clicked.connect(self.detail_edit.handle_btn_wrap_clicked)
        self.__update_empty()
        self.__on_selected(None)

    def __init_widget(self) -> None:
        self.notice_label = BodyLabel(self)
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_label.hide()
        self.placeholder = BodyLabel(self)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)

        self.filter_input = LineEdit(self)
        self.filter_input.setMinimumWidth(100)
        self.filter_input.setMaximumWidth(220)
        self.filter_input.setPlaceholderText(self.tr("过滤消息…"))
        self.filter_input.setToolTip(self.tr("按内容、发送、接收或事件信息过滤"))
        self.filter_input.setClearButtonEnabled(True)
        self.sort_btn = TransparentToolButton(FluentIcon.DOWN, self)
        self.sort_btn.setCheckable(True)
        self.sort_btn.setToolTip(self.tr("最新在上"))
        self.sort_btn.setAccessibleName(self.tr("切换消息顺序"))
        self.clear_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.clear_btn.setToolTip(self.tr("清空显示的消息"))
        self.clear_btn.setAccessibleName(self.tr("清空显示的消息"))
        for button in (self.sort_btn, self.clear_btn):
            button.setFixedSize(28, 28)
            button.setIconSize(QSize(16, 16))

        self.message_list = MessageList(self)
        self.message_list.setAccessibleName(self.tr("消息列表"))
        self.empty_label = BodyLabel(self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.list_pages = QStackedWidget(self)
        self.list_pages.addWidget(self.message_list)
        self.list_pages.addWidget(self.empty_label)
        self.close_label = CaptionLabel(self)
        self.close_label.setWordWrap(True)
        self.close_label.setTextFormat(Qt.TextFormat.PlainText)
        self.close_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.close_label.hide()

        self.back_btn = TransparentTooltipButton(FluentIcon.RETURN, self)
        self.back_btn.setToolTip(self.tr("返回"))
        self.back_btn.setAccessibleName(self.tr("返回"))
        self.wrap_btn = TransparentTooltipButton(BaseIcon.LINE_BREAK, self)
        self.wrap_btn.setToolTip(self.tr("换行"))
        self.wrap_btn.setAccessibleName(self.tr("换行"))
        self.wrap_btn.setCheckable(True)
        for button in (self.back_btn, self.wrap_btn):
            button.setFixedSize(28, 28)
            button.setIconSize(QSize(16, 16))
        self.detail_edit = ToolPlainTextEdit(self)
        self.detail_edit.set_read_only(True)
        self.detail_edit.code_widget.setAccessibleName(self.tr("消息内容"))
        # 复用只读编辑器与换行行为，详情顶栏只提供返回和换行操作。
        self.detail_edit.tool_widget.hide()
        self.detail_page = QWidget(self)
        self.stream_page = QWidget(self)
        self.content_pages = QStackedWidget(self)
        self.pages = QStackedWidget(self)

    def __init_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.notice_label)
        layout.addWidget(self.pages)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(8, 4, 4, 0)
        toolbar.setSpacing(6)
        toolbar.addStretch(1)
        toolbar.addWidget(self.filter_input, 1)
        toolbar.addWidget(self.sort_btn)
        toolbar.addWidget(self.clear_btn)

        detail_layout = QVBoxLayout(self.detail_page)
        detail_layout.setContentsMargins(4, 4, 4, 4)
        detail_layout.setSpacing(4)
        detail_toolbar = QHBoxLayout()
        detail_toolbar.addWidget(self.back_btn)
        detail_toolbar.addStretch(1)
        detail_toolbar.addWidget(self.wrap_btn)
        detail_layout.addLayout(detail_toolbar)
        detail_layout.addWidget(self.detail_edit, 1)

        stream_layout = QVBoxLayout(self.stream_page)
        stream_layout.setContentsMargins(2, 2, 2, 0)
        stream_layout.setSpacing(4)
        stream_layout.addLayout(toolbar)
        stream_layout.addWidget(self.list_pages, 1)
        stream_layout.addWidget(self.close_label)
        self.content_pages.addWidget(self.stream_page)
        self.content_pages.addWidget(self.detail_page)
        self.pages.addWidget(self.placeholder)
        self.pages.addWidget(self.content_pages)

    @property
    def applicable(self) -> bool:
        return self._applicable

    @property
    def count(self) -> int:
        return self._total

    def set_count(self, count: int) -> None:
        self._total = max(0, count)
        self.__update_empty()

    def set_notice(self, text: str) -> None:
        self.notice_label.setText(text)
        self.notice_label.setVisible(bool(text))

    def discard_before(self, index: int) -> None:
        """同步内核的保留窗口，不重置过滤、清空显示水位或仍有效的选择。"""
        count = 0
        for row in self.message_list.rows():
            if not isinstance(row.key, int) or row.key >= index:
                break
            count += 1
        self.message_list.evict_oldest(count)
        self.__update_empty()

    def set_data(
        self,
        data: dict,
        frames: list[WsFrame] | None = None,
        close: WsClose | None = None,
        events: list[SseEvent] | None = None,
    ) -> None:
        if is_websocket(data) or frames:
            self.show_websocket(frames or [], close or WsClose())
        elif is_event_stream(data.get("Response Content-Type")):
            self.show_sse_events(
                events or parse_sse(str(data.get("Response Body Text") or ""))
            )
        else:
            self.__reset()
            self._applicable = False
            self.placeholder.setText(self.tr("这条流量没有消息"))
            self.pages.setCurrentIndex(_PAGE_PLACEHOLDER)

    def show_websocket(self, frames: list[WsFrame], close: WsClose) -> None:
        self.__reset()
        self._applicable = True
        self._mode = "ws"
        self._total = frames[-1].index + 1 if frames else 0
        self.message_list.set_rows(
            [self.__frame_row(f) for f in frames[-MESSAGE_ROW_LIMIT:]]
        )
        self.set_close(close)
        self.pages.setCurrentIndex(_PAGE_STREAM)
        self.__update_empty()
        self.message_list.scroll_to_newest()

    def show_sse_events(self, events: list[SseEvent]) -> None:
        self.__reset()
        self._applicable = True
        self._mode = "sse"
        self._total = len(events)
        self.message_list.set_rows(
            [self.__event_row(e) for e in events[-MESSAGE_ROW_LIMIT:]]
        )
        self.pages.setCurrentIndex(_PAGE_STREAM)
        self.__update_empty()
        self.message_list.scroll_to_newest()

    def append_frame(self, frame: WsFrame) -> None:
        if self._mode != "ws":
            self.show_websocket([], self._close)
        self._total += 1
        self.__append(self.__frame_row(frame))

    def append_event(self, event: SseEvent) -> None:
        if self._mode != "sse":
            self.show_sse_events([])
        self._total += 1
        self.__append(self.__event_row(event))

    def __append(self, row: MessageRow) -> None:
        self.message_list.add(row)
        while self.message_list.message_count() > MESSAGE_ROW_LIMIT:
            self.message_list.evict_oldest()
        self.__update_empty()

    def set_close(self, close: WsClose) -> None:
        self._close = close
        text = self.__close_text(close) if close.is_closed else ""
        # 关闭原因允许换行；不能让一条状态信息撑高整个详情面板。
        self.close_label.setText(preview_text(text))
        self.close_label.setToolTip(
            f"<qt>{escape(text).replace(chr(10), '<br>')}</qt>" if text else ""
        )
        self.close_label.setVisible(close.is_closed)

    def __frame_row(self, frame: WsFrame) -> MessageRow:
        direction = self.tr("发送") if frame.from_client else self.tr("接收")
        route = (
            self.tr("客户端 → 服务端")
            if frame.from_client
            else self.tr("服务端 → 客户端")
        )
        parts = [
            f"#{frame.index + 1}",
            direction,
            opcode_name(frame.opcode),
            human.pretty_size(frame.size),
            frame_time(frame.timestamp),
        ]
        if frame.dropped:
            parts.append(self.tr("已丢弃"))
        if frame.injected:
            parts.append(self.tr("已注入"))
        if frame.truncated:
            parts.append(self.tr("内容已截断"))
        metadata = " · ".join(parts)
        content = frame.text() if frame.is_text else frame.content.hex(" ")
        return MessageRow(
            key=frame.index,
            icon=FluentIcon.UP if frame.from_client else FluentIcon.DOWN,
            metadata=metadata,
            preview=frame_preview(frame) or self.tr("（空消息）"),
            search_text=f"{metadata}\n{route}\n{content}",
            payload=frame,
        )

    def __event_row(self, event: SseEvent) -> MessageRow:
        if event.event:
            kind = event.event
        elif event.comment and not event.id and event.retry is None:
            kind = self.tr("心跳")
        else:
            kind = self.tr("事件信息")
        metadata = " · ".join(
            [
                f"#{event.index + 1}",
                self.tr("接收"),
                "SSE",
                preview_text(kind),
                human.pretty_size(len(event.raw.encode("utf-8"))),
            ]
        )
        return MessageRow(
            key=event.index,
            icon=FluentIcon.DOWN,
            metadata=metadata,
            preview=preview_text(event.data or event.comment or event.raw)
            or self.tr("（空消息）"),
            search_text=f"{metadata}\n{event.raw}\n{event.event}\n{event.data}\n{event.id}\n{event.retry}\n{event.comment}",
            payload=event,
        )

    @Slot(object)
    def __on_selected(self, row: MessageRow | None) -> None:
        self._selected = row
        if row is None:
            self.detail_edit.set_text("", lang=Language.TEXT)
            self.content_pages.setCurrentWidget(self.stream_page)

    @Slot(object)
    def __open_message(self, row: MessageRow) -> None:
        self._selected = row
        self.__show_content()
        self.content_pages.setCurrentWidget(self.detail_page)
        self.back_btn.setFocus()

    @Slot()
    def __back_to_list(self) -> None:
        # 不清空选择或重建列表，再点同一行也能通过 activated 重新进入详情。
        self.content_pages.setCurrentWidget(self.stream_page)
        self.message_list.setFocus()

    def __show_content(self) -> None:
        if self._selected is None:
            return
        payload = self._selected.payload
        notice = ""
        lang = Language.TEXT
        if isinstance(payload, WsFrame):
            content = payload.text() if payload.is_text else hex_dump(payload.content)
            shown = (
                len(payload.content)
                if payload.is_text
                else min(len(payload.content), HEX_DUMP_LIMIT)
            )
            if shown < payload.size:
                notice = self.tr(
                    "仅显示前 {} 字节，共 {} 字节；可导出流量查看完整消息。"
                ).format(shown, payload.size)
            if payload.is_text and looks_like_json(content):
                lang = Language.JSON
        elif isinstance(payload, SseEvent):
            # 同一份原文包含 data、事件名、id、重连间隔及注释，无需额外切换按钮。
            content = payload.raw
        else:
            return
        self.detail_edit.set_text(content, lang=lang, notice=notice)

    def __reset(self) -> None:
        self.set_notice("")
        self._total = 0
        self._close = WsClose()
        self._mode = ""
        self.close_label.clear()
        self.close_label.hide()
        self.message_list.clear()
        self.filter_input.clear()
        self.__update_empty()

    def __clear_display(self) -> None:
        self.message_list.clear()
        self.__update_empty()

    def __update_empty(self) -> None:
        count = self.message_list.visible_count()
        self.empty_label.setText(
            self.tr("没有匹配的消息")
            if self.message_list.filter_text
            else self.tr("暂无消息")
        )
        self.list_pages.setCurrentWidget(
            self.message_list if count else self.empty_label
        )

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

    @Slot(str)
    def __on_filter_changed(self, text: str) -> None:
        self.message_list.set_filter(text)
        self.__update_empty()

    @Slot(bool)
    def __on_order_toggled(self, checked: bool) -> None:
        self.message_list.set_descending(checked)
        self.sort_btn.setIcon(FluentIcon.UP if checked else FluentIcon.DOWN)
        self.sort_btn.setToolTip(
            self.tr("最早在上") if checked else self.tr("最新在上")
        )
