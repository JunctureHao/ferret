"""Server-Sent Events：解析层（纯函数）+ tee 层（`FerretSseAddon`）。

mitmproxy 对 SSE 零支持 —— 原生 `server_side_events.py` 全文只有一条告警
（mitmproxy#4469）：默认缓冲模式下端点不收尾就永远看不到事件；开 `stream` 则
body 不入库（`store_streamed_bodies` 默认 False，且流末才一次性写回，传输中途
addon 拿不到）。两条路都走不通，所以这里自己 tee：

`responseheaders` 检测出事件流后，把 `flow.response.stream` 换成**官方支持的
callable**（`mitmproxy/http.py` 的 `Response.stream` setter：每个 chunk 先过
callable 再转发），tee 出数据自己攒 body、增量解析、逐事件发 Qt 信号。不碰
`stream_large_bodies` / `store_streamed_bodies` 全局选项，非 SSE 流量零影响。
整体是 WebSocket 管道（层内摘值对象 → 信号过界）的平行复制。

模块内部分两层，imports 也按层分：

* **解析层**（`SseEvent` / `is_event_stream` / `SseFeeder` / `parse_sse`）：零
  mitmproxy、零 Qt，输入解码后的 `str` 输出冻结值对象。
* **tee 层**（`FerretSseAddon`）：碰 mitmproxy 的走 `bindings`（红线不变），发
  Qt 信号经构造时注入的 bridge（同 `UiBridgeAddon` 的姿势）。

## 与规范的关系

按 WHATWG HTML「Interpreting an event stream」实现，唯一刻意的偏差是**注释块也产出
事件**：规范里「没有 data 就不派发」，而 SSE 的心跳恰好是纯注释行（`: keep-alive`），
按规范解完一份心跳流会得到空列表 —— 界面上看着就像什么都没抓到。这一层是给人看的，
所以每个非空块都产出一条，靠 `data` / `comment` 两个字段区分是消息还是心跳。
"""

from __future__ import annotations

import codecs
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import HTTPFlow

if TYPE_CHECKING:
    from ferret.core.mitm.runtime import MitmRuntime

log = get_logger("mitmproxy")

#: `Content-Type` 的判定前缀。带参数的 `text/event-stream; charset=utf-8` 也算。
SSE_CONTENT_TYPE = "text/event-stream"

#: 没有显式 `event:` 字段的数据块的事件名（规范定的缺省值）。
DEFAULT_EVENT = "message"

# 单条流 tee 出来的原始字节最多攒多少。SSE 端点设计寿命是几小时起步（维基最近变更
# 流一天几百 MB），全攒着等流末补回 body 就是给内核埋一颗内存炸弹。超了停止攒
# body —— 解析推送继续（事件是增量吐出的，不依赖 buf），流末不补 body，响应体页
# 显示已经缓冲到的前 N 字节。与 `WS_FRAME_LIMIT` 同构：原生一个上限都没有，闸门
# 只能由 ferret 加。
SSE_BODY_LIMIT = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SseEvent:
    """事件流里的一个块（两个空行之间的那一段）。

    `event` 三种取值都有意义：显式 `event:` 字段原样带出；没写但有 `data:` 字段的按
    规范算 :data:`DEFAULT_EVENT`；一个字段都没有的块（纯注释心跳）留空 —— 那种块规范
    根本不派发，硬填一个 ``message`` 是编数据。

    `id` 是**这个块自己**的 `id:` 字段，不是规范里那个跨事件继承的 last event ID：
    表格那一列要回答的是「哪些事件带了 id」，继承来的值填进去反而看不出。
    """

    index: int
    event: str
    data: str
    id: str
    retry: int | None
    comment: str
    raw: str


def is_event_stream(content_type: object) -> bool:
    """`Content-Type` 是不是事件流。

    宽松的地方：前后空白、大小写、`; charset=utf-8` 参数都不影响判定。参数类型是
    `object` 而不是 `str` —— 详情字典里这一格缺失时是 ``"-"``，也可能压根没有键。

    不宽松的地方：只比前缀不够。`text/event-streamx` 是**另一种** MIME 类型，
    `startswith` 会把它认成事件流，然后拿 SSE 的规则去解一份不是 SSE 的 body。
    类型名到这里必须结束，后面只允许参数分隔符或空白。
    """
    if not isinstance(content_type, str):
        return False
    value = content_type.strip().lower()
    if not value.startswith(SSE_CONTENT_TYPE):
        return False
    rest = value[len(SSE_CONTENT_TYPE) :]
    return not rest or rest[0] in ";, \t"


def _split_lines(text: str) -> list[str]:
    """按规范切行：`\\r\\n`、`\\r`、`\\n` 三种分隔符都算，且不吃掉空行。

    不能用 `str.splitlines()`：它还会在 `\\x0b` / `\\x0c` / `\\u2028` 这些字符上断行，
    而那些在 SSE 里是**数据的一部分**（JSON 字符串里出现过就会被切坏）。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _parse_field(line: str) -> tuple[str, str]:
    """一行 → ``(字段名, 值)``。

    规范：以 `:` 开头是注释（字段名留空）；`field: value` 里值前面**恰好**去掉一个
    空格（`data:  x` 的值是 `" x"`）；整行没有 `:` 时字段名是整行、值是空串。
    """
    name, sep, value = line.partition(":")
    if not sep:
        return line, ""
    # `removeprefix` 而不是 `lstrip(" ")`：规范只让去掉**一个**空格，
    # `data:  x` 的值是 `" x"`（缩进过的 JSON 全靠这一条不被啃掉）。
    return name, value.removeprefix(" ")


class _Block:
    """一个块的累积缓冲。字段的合并规则各不相同，所以不能拿一个 dict 糊过去。"""

    __slots__ = ("comments", "data", "event", "has_data", "id", "lines", "retry")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.data: list[str] = []
        self.has_data = False
        self.event = ""
        self.id = ""
        self.retry: int | None = None
        self.comments: list[str] = []

    def feed(self, line: str) -> None:
        self.lines.append(line)
        name, value = _parse_field(line)
        if not name:
            # `:` 开头 —— 注释。心跳全靠它，所以留着而不是丢掉。
            self.comments.append(value)
        elif name == "data":
            # 多条 `data:` 按换行拼接；空值那条也算，`data:` 单独一行是合法的空消息。
            self.data.append(value)
            self.has_data = True
        elif name == "event":
            self.event = value
        elif name == "id":
            # 规范：含 NUL 的 id 整条忽略（不是截断）。
            if "\0" not in value:
                self.id = value
        # 只认纯 ASCII 数字。`str.isdigit()` 对全角「１２３」也是真，而那不是规范说的
        # ASCII digits，`int()` 却照样吃 —— 会把乱码当成重连间隔。
        elif name == "retry" and value.isascii() and value.isdigit():
            self.retry = int(value)
        # 其余字段按规范忽略（`retry: abc` 也走到这里）。

    def build(self, index: int) -> SseEvent:
        event = self.event
        if not event and self.has_data:
            event = DEFAULT_EVENT
        return SseEvent(
            index=index,
            event=event,
            data="\n".join(self.data),
            id=self.id,
            retry=self.retry,
            comment="\n".join(self.comments),
            raw="\n".join(self.lines),
        )


class SseFeeder:
    """增量解析：一段段喂文本，凑齐一块（空行收尾）就吐事件。

        tee callable 拿到的 chunk 是 TCP 切分，块边界和 chunk 边界没有任何关系 —
    — 一个
        块可能摊开在三段 chunk 里，一段 chunk 也可能装着三个半块。所以攒行归这里，
        喂入方只负责按到达次序倒进来。事件序号从 0 开始连续编号，与 `parse_sse` 对同一
        份文本的结果逐一相等（它本来就是这里的薄封装）。
    """

    def __init__(self) -> None:
        self._pending = ""
        self._block = _Block()
        self._count = 0

    def feed(self, text: str) -> list[SseEvent]:
        """喂一段解码后的文本，吐出这一段凑齐的所有事件。"""
        events: list[SseEvent] = []
        lines = _split_lines(self._pending + text)
        # 最后一段没有行尾分隔符，可能是个喂到一半的行 —— 留到下一段凑齐再解，
        # 否则 `data: {"a` 会被当成完整字段吐出去一次。
        self._pending = lines.pop()
        for line in lines:
            if line:
                self._block.feed(line)
                continue
            if self._block.lines:
                events.append(self._block.build(self._count))
                self._count += 1
            self._block = _Block()
        return events

    def flush(self) -> list[SseEvent]:
        """连接收尾：把喂到一半的行和未满一块的残余吐出来。

        规范面对的是一条还在流动的连接（半个块要等后续字节），而流末意味着后续
        不会来了 —— 残余不派发就等于凭空少一条。
        """
        events: list[SseEvent] = []
        if self._pending:
            self._block.feed(self._pending)
            self._pending = ""
        if self._block.lines:
            events.append(self._block.build(self._count))
            self._count += 1
        self._block = _Block()
        return events


def parse_sse(text: str) -> list[SseEvent]:
    """事件流文本 → 事件列表。

    末尾没有空行时最后一段也要派发（`flush` 的语义）：这里拿到的是一份已经收完的
    body，最后那段不派发就等于凭空少一条。

    `raw` 里的行分隔符统一成 `\\n` —— 界面上那一格显示的是块的原文，三种分隔符混排
    对读它的人没有信息量。
    """
    feeder = SseFeeder()
    return [*feeder.feed(text), *feeder.flush()]


# ———————————————————————— tee 层 ————————————————————————


class _SseTap:
    """一条流的 tee 状态：增量解码器 + feeder + 攒 body 的缓冲。

    callable 本体是 :meth:`tee`（bound method），装进 `flow.response.stream` 后
    每个 chunk 走一趟：解码 → 喂 feeder → 事件经回调上桥 → 原始字节攒 buf →
    原样还给转发路径。所有字段只在 mitm 线程上碰，不加锁。
    """

    def __init__(
        self,
        charset: str,
        on_events: Callable[[list[SseEvent]], None],
        on_end: Callable[[], None],
    ) -> None:
        try:
            decoder_factory = codecs.getincrementaldecoder(charset)
        except LookupError:
            # charset 是服务器自报的，畸形/生僻名字都见过 —— 退回 UTF-8 宽松解，
            # 比整条流不显示强（和 §2「解码一律 strict=False」同一个道理）。
            decoder_factory = codecs.getincrementaldecoder("utf-8")
        self._decoder = decoder_factory(errors="replace")
        self._feeder = SseFeeder()
        self._on_events = on_events
        self._on_end = on_end
        self.buf = bytearray()
        # 超 :data:`SSE_BODY_LIMIT` 后置真：停止攒 body，解析推送继续。
        self.buf_overflowed = False

    def tee(self, chunk: bytes) -> bytes:
        if not chunk:
            # 流末约定：mitmproxy 在转发路径收尾时以空 chunk 通知 callable。
            self._flush()
            return chunk
        if not self.buf_overflowed:
            if len(self.buf) + len(chunk) > SSE_BODY_LIMIT:
                self.buf_overflowed = True
                log.info(
                    "SSE body 超过 %d 字节，停止攒 body（事件推送继续）", SSE_BODY_LIMIT
                )
            else:
                self.buf += chunk
        events = self._feeder.feed(self._decoder.decode(chunk))
        if events:
            self._on_events(events)
        return chunk

    def _flush(self) -> None:
        events = [
            *self._feeder.feed(self._decoder.decode(b"", final=True)),
            *self._feeder.flush(),
        ]
        if events:
            self._on_events(events)
        self._on_end()


class FerretSseAddon:
    """把事件流的 `flow.response.stream` 换成 callable，边转发边解析边推送。

    挂点必须是 `responseheaders`：mitmproxy 在源码注释里写明 stream 包装要赶在
    `response` 钩子之前设，`response` 里设已晚（转发已经开始，前面的 chunk 就
    漏过去了）。

    存档语义对齐 `ws_frames()`：`_events` 恒存全量（显示上限归界面）；flow 从
    View 移除时清掉对应条目。
    """

    def __init__(self, bridge: MitmRuntime | None = None) -> None:
        # bridge 可后置注入：master 装配时不认识 runtime（master 由 runtime 造），
        # runtime 在挂 UiBridgeAddon 时一并补上。没补就等于推送关掉，存档照常。
        self.bridge = bridge
        self._events: dict[str, list[SseEvent]] = {}
        self._taps: dict[str, _SseTap] = {}
        self._flows: dict[str, HTTPFlow] = {}

    # —— addon 钩子 ——

    def responseheaders(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None or not is_event_stream(
            response.headers.get("content-type")
        ):
            return
        # 断点拦在响应期时 stream 已开始向客户端转发（原生行为如此）——但断点拦的是
        # `response` 钩子，而 `responseheaders` 更早，这里登记不受影响。
        tap = _SseTap(
            # charset 单独拆出来：整串 content-type（带参数）喂给 codecs 会
            # LookupError，而 `_SseTap` 只认编码名。
            _charset_of(response.headers.get("content-type", "")),
            on_events=lambda events, fid=flow.id: self._record_events(fid, events),
            on_end=lambda fid=flow.id: self._finish(fid),
        )
        self._taps[flow.id] = tap
        self._flows[flow.id] = flow
        self._events.setdefault(flow.id, [])
        response.stream = tap.tee
        self._emit_started(flow.id)

    # —— 对内 ——

    def _record_events(self, flow_id: str, events: list[SseEvent]) -> None:
        archive = self._events.get(flow_id)
        if archive is None:
            return
        archive.extend(events)
        if self.bridge is None:
            return
        for event in events:
            self.bridge.sse_event.emit(flow_id, event)

    def _finish(self, flow_id: str) -> None:
        tap = self._taps.pop(flow_id, None)
        flow = self._flows.pop(flow_id, None)
        if tap is None or flow is None:
            return
        # 把 tee 攒下的字节补回 body：响应体页 / 保存 / HAR 导出都读它。超限的流
        # 攒的是前缀，也补回去 —— 有前缀总比空 body 强（界面能显示「前 10 MB」）。
        if flow.response is not None:
            flow.response.data.content = bytes(tap.buf)
        if self.bridge is not None:
            self.bridge.sse_ended.emit(flow_id)

    def _emit_started(self, flow_id: str) -> None:
        if self.bridge is not None:
            self.bridge.sse_started.emit(flow_id)

    def forget(self, flow_id: str) -> None:
        """flow 从 View 移除时清存档。由 facade 的 remove/clear 路径调用。"""
        self._events.pop(flow_id, None)
        self._taps.pop(flow_id, None)
        self._flows.pop(flow_id, None)

    def clear(self) -> None:
        """整表清空（`clear_flows` 那条路）。"""
        self._events.clear()
        self._taps.clear()
        self._flows.clear()

    def events(self, flow_id: str) -> list[SseEvent]:
        """全量事件存档（不在此套显示上限 —— 上限是界面策略）。"""
        return list(self._events.get(flow_id, []))


def _charset_of(content_type: str) -> str:
    """`text/event-stream; charset=utf-8` → ``utf-8``；没有参数回 UTF-8（规范缺省）。"""
    for part in content_type.split(";")[1:]:
        name, sep, value = part.strip().partition("=")
        if sep and name.strip().lower() == "charset":
            return value.strip().strip('"') or "utf-8"
    return "utf-8"
