"""消息页：WebSocket 帧表与 SSE 事件表。

改造前这两种流量在详情面板里**根本看不见**：WS 流量只有 101 那一次握手的头，之后所有
帧都在 `flow.websocket` 里躺着没人读；SSE 响应则整份挤在 Body 那一页里，一堆
``data: {...}`` 连成一块文本，想看第三个事件只能自己数空行。

两种协议共用这一页而不是各开一页：它们回答的是同一个问题 ——「这条连接上来回传了些
什么」，而任何一条流量最多只可能是其中一种。所以内部一个 `QStackedWidget` 三态切换
（帧表 / 事件表 / 占位），外面由详情面板决定整页是否露面。

两侧的数据来路完全不同，这一点决定了 API 形状：

* **WS 帧是活的**。`flow.websocket` 由 mitmproxy 持续填充，帧要边到边追加，所以
  :meth:`MessagesPane.append_frame` 是增量接口 —— 整表重建会把用户的选中和滚动位置
  一起清掉，而行情型连接每秒几十帧，等于面板没法用。
* **SSE 事件是静的**。mitmproxy 默认不开 streaming，流量走完时整份 body 已经缓冲好，
  所以直接解析 `Response Body Text` 就是全部。开了 `stream` 时 body 为空，这时显示
  「响应体以流式转发，未缓冲」而不是一张空表 —— 后者看着像解析失败。

表格行数上限见 :data:`MESSAGE_ROW_LIMIT`：**截断只发生在这一层**，`ws_frames` 恒返回
全部帧，被截掉多少一定在界面上说出来。
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    InfoBadge,
    InfoLevel,
    TableWidget,
)

from ferret.apps.common.edit import Language, ToolPlainTextEdit
from ferret.apps.common.splitter import BaseSplitter
from ferret.core.mitm import WS_FRAME_LIMIT, WsClose, WsFrame, human, opcode_name
from ferret.utils.sse import SseEvent, is_event_stream, parse_sse

# 两张表各自最多摆多少行。和 :data:`ferret.core.mitm.WS_FRAME_LIMIT` 钉在同一个数
# ——它本来就是「界面侧显示上限」的那个值，事件表没有理由另立一个。
MESSAGE_ROW_LIMIT = WS_FRAME_LIMIT

# 预览列的字符上限。够看出这帧是什么（JSON 的头几个键、订阅指令的动作名），又不会
# 让一帧几十 KB 的推流内容把整行拖成一条线。
PREVIEW_LIMIT = 160

# 二进制帧预览列取多少字节转十六进制（每字节占三个字符）。
PREVIEW_HEX_BYTES = 32

# 详情区 hex dump 最多铺多少字节。上限本身要在界面上说出来，同 `MESSAGE_ROW_LIMIT`。
HEX_DUMP_LIMIT = 4096

# 帧表列号。
_COL_NO, _COL_DIR, _COL_TYPE, _COL_TIME, _COL_SIZE, _COL_PREVIEW = range(6)

# 事件表列号。
_SSE_NO, _SSE_EVENT, _SSE_ID, _SSE_RETRY, _SSE_SIZE, _SSE_DATA = range(6)

# 堆叠页序号。
_PAGE_PLACEHOLDER, _PAGE_WS, _PAGE_SSE = range(3)


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
    只是「该用哪套词法器」，猜错的代价是高亮不准，不是显示错。
    """
    return text.lstrip()[:1] in ("{", "[")


def frame_time(timestamp: float | None) -> str:
    """帧时间戳 → ``HH:MM:SS.mmm``。

    刻意不带日期（`human.format_timestamp` 那套）：一条连接上的帧动辄成百上千、彼此
    只差几毫秒，日期在每一行重复一遍是纯噪音，而毫秒才是这张表要回答的东西。整条流量
    的起止时间在「概览」的时序卡里。

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

    折叠空白是必须的：JSON 帧常常是格式化过的，直接塞进单元格只能看到第一行的 ``{``。
    """
    line = " ".join(text.split())
    if len(line) > PREVIEW_LIMIT:
        return line[:PREVIEW_LIMIT] + "…"
    return line


def frame_preview(frame: WsFrame) -> str:
    """帧内容的预览列文案：TEXT 帧按文本，其余按十六进制。"""
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


def _item(text: str, tooltip: str = "") -> QTableWidgetItem:
    """一个单元格。长文本自动挂 tooltip —— 预览列截断之后全文只能靠它。"""
    cell = QTableWidgetItem(text)
    if tooltip:
        cell.setToolTip(tooltip)
    elif len(text) > 30:
        cell.setToolTip(text)
    return cell


def _numeric_item(text: str) -> QTableWidgetItem:
    cell = QTableWidgetItem(text)
    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    return cell


class MessagesPane(QWidget):
    """WS 帧 / SSE 事件两张表 + 选中项全文。

    对外只有四件事：`set_data` 换一条流量、`append_frame` 追一帧、`set_close` 更新
    关闭信息、`applicable` / `count` 告诉详情面板这一页该不该露面、标签上挂几。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # 显示中的行，和表格行号 1:1 对齐（截断之后就不等于全部了，所以另存 `_total`）。
        self._frames: list[WsFrame] = []
        self._events: list[SseEvent] = []
        self._total = 0
        self._applicable = False
        self._close = WsClose()

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # —— 组件 ——

    def __init_widget(self) -> None:
        self.placeholder = BodyLabel(self)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)

        self.frame_table = self.__make_table(
            (
                self.tr("#"),
                self.tr("Direction"),
                self.tr("Type"),
                self.tr("Time"),
                self.tr("Size"),
                self.tr("Preview"),
            ),
            (44, 52, 76, 96, 72),
        )
        self.frame_detail = ToolPlainTextEdit()
        self.frame_detail.set_read_only(True)
        self.frame_notice = InfoBadge(self)
        self.frame_notice.setLevel(InfoLevel.WARNING)
        self.frame_notice.hide()
        self.close_label = CaptionLabel(self)

        self.event_table = self.__make_table(
            (
                self.tr("#"),
                self.tr("Event"),
                self.tr("ID"),
                self.tr("Retry"),
                self.tr("Size"),
                self.tr("Data"),
            ),
            (44, 120, 90, 60, 72),
        )
        self.event_detail = ToolPlainTextEdit()
        self.event_detail.set_read_only(True)
        self.event_notice = InfoBadge(self)
        self.event_notice.setLevel(InfoLevel.WARNING)
        self.event_notice.hide()

        # 上表下详情，比例可拖：帧短的时候（订阅指令那类）表格该占大头，一帧几十 KB
        # 的时候详情该占大头，而这两种连接看的是同一个面板。
        self.frame_splitter = BaseSplitter(Qt.Orientation.Vertical, self)
        self.frame_splitter.addWidget(self.frame_table)
        self.frame_splitter.addWidget(self.frame_detail)
        self.frame_splitter.setStretchFactor(0, 1)
        self.frame_splitter.setStretchFactor(1, 1)

        self.event_splitter = BaseSplitter(Qt.Orientation.Vertical, self)
        self.event_splitter.addWidget(self.event_table)
        self.event_splitter.addWidget(self.event_detail)
        self.event_splitter.setStretchFactor(0, 1)
        self.event_splitter.setStretchFactor(1, 1)

    def __make_table(
        self, headers: tuple[str, ...], widths: tuple[int, ...]
    ) -> TableWidget:
        """一张只读、整行选中、末列拉伸的表。

        刻意**不**开排序：帧和事件本来就是按到达顺序编号的，按大小排一遍之后
        `#` 列和表格行号就不再对应，增量追加也没地方插。
        """
        table = TableWidget(self)
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(28)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        header = table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(40)
        header.setFixedHeight(32)
        for column, width in enumerate(widths):
            table.setColumnWidth(column, width)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        return table

    def __init_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        ws_page = QWidget(self)
        ws_layout = QVBoxLayout(ws_page)
        ws_layout.setContentsMargins(0, 0, 0, 0)
        ws_layout.setSpacing(4)
        ws_layout.addWidget(self.frame_splitter, 1)
        ws_footer = QHBoxLayout()
        ws_footer.setContentsMargins(2, 0, 2, 2)
        ws_footer.setSpacing(8)
        ws_footer.addWidget(self.close_label, 1)
        ws_footer.addWidget(self.frame_notice, 0)
        ws_layout.addLayout(ws_footer)

        sse_page = QWidget(self)
        sse_layout = QVBoxLayout(sse_page)
        sse_layout.setContentsMargins(0, 0, 0, 0)
        sse_layout.setSpacing(4)
        sse_layout.addWidget(self.event_splitter, 1)
        sse_footer = QHBoxLayout()
        sse_footer.setContentsMargins(2, 0, 2, 2)
        sse_footer.setSpacing(8)
        sse_footer.addStretch(1)
        sse_footer.addWidget(self.event_notice, 0)
        sse_layout.addLayout(sse_footer)

        # 三态共用一个 `QStackedWidget`：任何一条流量最多只是其中一种。
        self.pages = QStackedWidget(self)
        self.pages.addWidget(self.placeholder)  # _PAGE_PLACEHOLDER
        self.pages.addWidget(ws_page)  # _PAGE_WS
        self.pages.addWidget(sse_page)  # _PAGE_SSE
        layout.addWidget(self.pages)

    def __connect_signal_to_slot(self) -> None:
        self.frame_table.itemSelectionChanged.connect(self.__on_frame_selected)
        self.event_table.itemSelectionChanged.connect(self.__on_event_selected)

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
        """帧数 / 事件数的**总数**（不是显示行数）—— 标签上那枚徽标读它。"""
        return self._total

    def set_data(
        self,
        data: dict,
        frames: list[WsFrame] | None = None,
        close: WsClose | None = None,
    ) -> None:
        """换一条流量。

        Args:
            data: 详情字典。SSE 那一路只靠它（``Response Content-Type`` +
                ``Response Body Text``），不需要 controller。
            frames: 已抓到的帧，由详情面板从 controller 取来（跨线程那一步归它）。
            close: 关闭信息，同上。
        """
        if is_websocket(data) or frames:
            self.show_websocket(frames or [], close or WsClose())
            return
        if is_event_stream(data.get("Response Content-Type")):
            self.show_sse(str(data.get("Response Body Text") or ""))
            return
        self.__reset()
        self._applicable = False
        self.placeholder.setText(self.tr("This flow carries no messages"))
        self.pages.setCurrentIndex(_PAGE_PLACEHOLDER)

    def show_websocket(self, frames: list[WsFrame], close: WsClose) -> None:
        """整表重建。选中期间新到的帧走 `append_frame`，不再走这里。"""
        self.__reset()
        self._applicable = True
        self._total = len(frames)
        self._frames = list(frames[-MESSAGE_ROW_LIMIT:])
        self.frame_table.setRowCount(len(self._frames))
        for row, frame in enumerate(self._frames):
            self.__fill_frame_row(row, frame)
        self.__update_frame_notice()
        self.set_close(close)
        self.pages.setCurrentIndex(_PAGE_WS)

    def show_sse(self, body: str) -> None:
        """一份 `text/event-stream` body → 事件表。空 body 走「未缓冲」占位。"""
        self.__reset()
        self._applicable = True
        if not body:
            self.placeholder.setText(
                self.tr("The response body was streamed, not buffered")
            )
            self.pages.setCurrentIndex(_PAGE_PLACEHOLDER)
            return
        events = parse_sse(body)
        self._total = len(events)
        self._events = list(events[-MESSAGE_ROW_LIMIT:])
        self.event_table.setRowCount(len(self._events))
        for row, event in enumerate(self._events):
            self.__fill_event_row(row, event)
        self.__update_event_notice()
        self.pages.setCurrentIndex(_PAGE_SSE)

    def append_frame(self, frame: WsFrame) -> None:
        """新到一帧。

        只有已经贴着底部时才跟着滚：正翻看前面某一帧的人不该被每秒几十帧的推流拽走。
        超过上限时从头部挤掉一行 —— 反过来（到上限就不再追加）看着像面板卡死了。
        """
        if self.pages.currentIndex() != _PAGE_WS:
            # 选中流量时握手还没完成（`raw_state` 里 `websocket` 仍是 None），
            # 帧却已经到了：这一帧本身就是「它是 WS」的证据。
            self.show_websocket([], self._close)
        self._total += 1
        bar = self.frame_table.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        if len(self._frames) >= MESSAGE_ROW_LIMIT:
            self._frames.pop(0)
            self.frame_table.removeRow(0)
        self._frames.append(frame)
        row = self.frame_table.rowCount()
        self.frame_table.insertRow(row)
        self.__fill_frame_row(row, frame)
        self.__update_frame_notice()
        if at_bottom:
            self.frame_table.scrollToBottom()

    def set_close(self, close: WsClose) -> None:
        """底部那一行关闭信息。连接还开着就说「保持中」，别留空。"""
        self._close = close
        self.close_label.setText(self.__close_text(close))

    # —— 填表 ——

    def __reset(self) -> None:
        self._frames = []
        self._events = []
        self._total = 0
        self._close = WsClose()
        self.frame_table.clearContents()
        self.frame_table.setRowCount(0)
        self.event_table.clearContents()
        self.event_table.setRowCount(0)
        self.frame_detail.set_text("")
        self.event_detail.set_text("")
        self.frame_notice.hide()
        self.event_notice.hide()
        self.close_label.clear()

    def __fill_frame_row(self, row: int, frame: WsFrame) -> None:
        arrow, direction = (
            ("↑", self.tr("Client → Server"))
            if frame.from_client
            else ("↓", self.tr("Server → Client"))
        )
        # `dropped` / `injected` 写成字眼而不是一枚小圆点：被丢掉的帧对端**根本没收到**，
        # 这是排查时的结论级信息，读者不该先去猜一个没有图例的色点是什么意思。
        kind = opcode_name(frame.opcode)
        flags = [
            label
            for flag, label in (
                (frame.dropped, self.tr("dropped")),
                (frame.injected, self.tr("injected")),
            )
            if flag
        ]
        if flags:
            kind = f"{kind} ({', '.join(flags)})"
        self.frame_table.setItem(row, _COL_NO, _numeric_item(str(frame.index)))
        self.frame_table.setItem(row, _COL_DIR, _item(arrow, direction))
        self.frame_table.setItem(row, _COL_TYPE, _item(kind))
        self.frame_table.setItem(row, _COL_TIME, _item(frame_time(frame.timestamp)))
        self.frame_table.setItem(
            row, _COL_SIZE, _numeric_item(human.pretty_size(frame.size))
        )
        self.frame_table.setItem(row, _COL_PREVIEW, _item(frame_preview(frame)))

    def __fill_event_row(self, row: int, event: SseEvent) -> None:
        # 纯注释块（心跳）没有事件类型，预览列退回注释原文并带上 `:` —— 那正是它在线上
        # 的样子，看着就知道这一行不是一条消息。
        preview = (
            preview_text(event.data)
            if event.data
            else preview_text(f": {event.comment}")
        )
        self.event_table.setItem(row, _SSE_NO, _numeric_item(str(event.index)))
        self.event_table.setItem(row, _SSE_EVENT, _item(event.event))
        self.event_table.setItem(row, _SSE_ID, _item(event.id))
        self.event_table.setItem(
            row,
            _SSE_RETRY,
            _numeric_item("" if event.retry is None else str(event.retry)),
        )
        self.event_table.setItem(
            row,
            _SSE_SIZE,
            _numeric_item(human.pretty_size(len(event.data.encode()))),
        )
        self.event_table.setItem(row, _SSE_DATA, _item(preview))

    def __update_frame_notice(self) -> None:
        self.__update_notice(
            self.frame_notice,
            len(self._frames),
            self.tr("Showing the latest {} of {} frames"),
        )

    def __update_event_notice(self) -> None:
        self.__update_notice(
            self.event_notice,
            len(self._events),
            self.tr("Showing the latest {} of {} events"),
        )

    def __update_notice(self, badge: InfoBadge, shown: int, template: str) -> None:
        """截断提示。没截断就不出现，截了就必须写清总数。"""
        if self._total <= shown:
            badge.hide()
            return
        badge.setText(template.format(shown, self._total))
        badge.adjustSize()
        badge.show()

    def __close_text(self, close: WsClose) -> str:
        if not close.is_closed:
            return self.tr("Connection open")
        if close.closed_by_client is None:
            parts = [self.tr("Closed")]
        elif close.closed_by_client:
            parts = [self.tr("Closed by client")]
        else:
            parts = [self.tr("Closed by server")]
        if close.close_code is not None:
            parts.append(str(close.close_code))
        if close.close_reason:
            parts.append(close.close_reason)
        if close.timestamp_end:
            parts.append(frame_time(close.timestamp_end))
        return " · ".join(parts)

    # —— 槽 ——

    @Slot()
    def __on_frame_selected(self) -> None:
        row = self.frame_table.currentRow()
        if not 0 <= row < len(self._frames):
            self.frame_detail.set_text("")
            return
        frame = self._frames[row]
        if frame.is_text:
            text = frame.text()
            lang = Language.JSON if looks_like_json(text) else Language.TEXT
            self.frame_detail.set_text(text, lang=lang)
            return
        dump = hex_dump(frame.content)
        if frame.size > HEX_DUMP_LIMIT:
            note = self.tr("Showing the first {} of {} bytes")
            dump = f"{dump}\n{note.format(HEX_DUMP_LIMIT, frame.size)}"
        self.frame_detail.set_text(dump, lang=Language.TEXT)

    @Slot()
    def __on_event_selected(self) -> None:
        row = self.event_table.currentRow()
        if not 0 <= row < len(self._events):
            self.event_detail.set_text("")
            return
        event = self._events[row]
        # 有 data 就只看 data（JSON 自动高亮），否则退回块原文 —— 心跳块的信息全在
        # 那一行注释里。
        if event.data:
            lang = Language.JSON if looks_like_json(event.data) else Language.TEXT
            self.event_detail.set_text(event.data, lang=lang)
            return
        self.event_detail.set_text(event.raw, lang=Language.TEXT)
