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
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import Flow, HTTPFlow
from ferret.core.mitm.rewrite import REWRITE_ANSWERED_KEY

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
SSE_EVENT_LIMIT = 10_000
SSE_ARCHIVE_LIMIT = 10 * 1024 * 1024
SSE_BLOCK_LIMIT = 1024 * 1024
SSE_BODY_TRUNCATED_KEY = "ferret.sse.body_truncated"
SSE_EVENTS_TRUNCATED_KEY = "ferret.sse.events_truncated"
SSE_EVENT_COUNT_KEY = "ferret.sse.event_count"


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
            try:
                self.retry = int(value)
            except ValueError:
                # Python limits decimal conversion length; a malformed retry
                # must not prevent subsequent data from being forwarded.
                pass
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
        self._block_size = 0

    def feed(self, text: str) -> list[SseEvent]:
        """喂一段解码后的文本，吐出这一段凑齐的所有事件。"""
        events: list[SseEvent] = []
        data = self._pending + text
        if len(data) + self._block_size > SSE_BLOCK_LIMIT and not any(
            sep in data for sep in ("\n\n", "\r\r", "\r\n\r\n")
        ):
            raise ValueError("SSE event exceeds parser buffer limit")
        # 段尾悬着一个 `\r` 时先扣下不切：它可能自己就是行分隔符，也可能是 `\r\n`
        # 的前半 —— TCP 把一个 `\r\n` 劈在两段之间（`\r` 收上一段尾、`\n` 开下一段
        # 头）是常态。此时当行尾吃掉，下一段开头的 `\n` 就成了一条凭空空行，把攒到
        # 一半的块当场派发（`event:` 与 `data:` 分裂成两条事件）。扣下的 `\r` 并进
        # pending，下一段凑齐再判定；真是裸 `\r` 分隔符的只晚一段到货，不会卡死。
        held_cr = data.endswith("\r")
        if held_cr:
            data = data[:-1]
        lines = _split_lines(data)
        # 最后一段没有行尾分隔符，可能是个喂到一半的行 —— 留到下一段凑齐再解，
        # 否则 `data: {"a` 会被当成完整字段吐出去一次。pending 里没有任何行分隔符，
        # 至多悬着上面扣下的那个 `\r`。
        self._pending = lines.pop()
        if held_cr:
            self._pending += "\r"
        if len(self._pending) > SSE_BLOCK_LIMIT:
            raise ValueError("SSE line exceeds parser buffer limit")
        for line in lines:
            if line:
                self._block_size += len(line) + 1
                if self._block_size > SSE_BLOCK_LIMIT:
                    raise ValueError("SSE event exceeds parser buffer limit")
                self._block.feed(line)
                continue
            if self._block.lines:
                events.append(self._block.build(self._count))
                self._count += 1
            self._block = _Block()
            self._block_size = 0
        return events

    def flush(self) -> list[SseEvent]:
        """连接收尾：把喂到一半的行和未满一块的残余吐出来。

        规范面对的是一条还在流动的连接（半个块要等后续字节），而流末意味着后续
        不会来了 —— 残余不派发就等于凭空少一条。
        """
        events: list[SseEvent] = []
        if self._pending:
            # 悬置的 `\r` 到流末落定：`\r\n` 的后半不会再来，它就是个行尾，
            # 不能漏进最后一个字段的值（`retry: 100\r` 的 `"100\r"` 不是数字）。
            line = self._pending.removesuffix("\r")
            self._pending = ""
            if line:
                self._block.feed(line)
        if self._block.lines:
            events.append(self._block.build(self._count))
            self._count += 1
        self._block = _Block()
        self._block_size = 0
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


# wbits=32+15 让 zlib 自识别 gzip / zlib 包头；试错失败换裸 deflate（-15）重放。
_AUTO_WBITS = 32 + zlib.MAX_WBITS
_RAW_WBITS = -zlib.MAX_WBITS
_PLAIN_ENCODINGS = frozenset({"", "identity"})
_KNOWN_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate"}) | _PLAIN_ENCODINGS


class _ChunkDecoder:
    """「线上 chunk → 解析层输入」的 Content-Encoding 剥离层（issues #12）。

    mitmproxy 的 stream callable 拿到的是**未解压**的转发 chunk，事件解析前要
    自己剥 Content-Encoding；转发侧不受影响（tee 恒原样返回 chunk）。gzip /
    deflate 增量解压：wbits 自动识别 gzip 与 zlib 包头（"deflate" 该是 zlib
    包，但不少服务器发裸 deflate —— 试错一次换 -15 并从攒下的前缀重放），
    gzip 多成员连着解。br / zstd 标准库没有解压器 —— 解不出来还硬喂只会产出
    乱码事件，诚实降级成「不解析」（`decode` 恒回 ``None``）。
    """

    __slots__ = ("_dead", "_decompressor", "_prefix", "_wbits")

    def __init__(self, content_encoding: str) -> None:
        self._dead = False
        # 首次成功解压前攒下的原始前缀：deflate 换形态时从这里重放（SSE 流开头
        # 才判得出形态，前缀很短），成功之后只喂增量。
        self._prefix: bytearray | None = bytearray()
        name = content_encoding.strip().lower()
        if name in _PLAIN_ENCODINGS:
            self._wbits = 0
            self._decompressor = None
        elif name in _KNOWN_ENCODINGS:
            self._wbits = _AUTO_WBITS
            self._decompressor = zlib.decompressobj(_AUTO_WBITS)
        else:
            self._wbits = 0
            self._decompressor = None
            self._dead = True

    def decode(self, chunk: bytes) -> bytes | None:
        """一段线上字节 → 明文；``None`` 表示这段放弃解析（照常转发）。"""
        if self._dead:
            return None
        if self._decompressor is None:
            return chunk
        if self._prefix is not None:
            self._prefix += chunk
            # zlib cannot distinguish a wrapped stream from raw deflate after
            # only one byte. Do not feed or discard the prefix prematurely.
            if len(self._prefix) < 2:
                return b""
            data = bytes(self._prefix)
        else:
            data = chunk
        out = bytearray()
        try:
            while True:
                out += self._decompressor.decompress(data, SSE_BODY_LIMIT + 1)
                if len(out) > SSE_BODY_LIMIT or self._decompressor.unconsumed_tail:
                    self._dead = True
                    return None
                if not self._decompressor.eof:
                    break
                # gzip 多成员：本成员收尾，剩余字节（可能为空）换新解压器接着解。
                data = self._decompressor.unused_data
                self._decompressor = zlib.decompressobj(self._wbits)
                if not data:
                    break
        except zlib.error:
            if self._prefix is not None and self._wbits == _AUTO_WBITS:
                # deflate 双形态：zlib 包解不动，按裸 deflate 从前缀重来一遍。
                self._wbits = _RAW_WBITS
                self._decompressor = zlib.decompressobj(_RAW_WBITS)
                try:
                    out += self._decompressor.decompress(
                        bytes(self._prefix), SSE_BODY_LIMIT + 1
                    )
                    if len(out) > SSE_BODY_LIMIT or self._decompressor.unconsumed_tail:
                        self._dead = True
                        return None
                except zlib.error:
                    self._dead = True
                    return None
            else:
                # 中途的坏字节救不回来：余流放弃解析，转发不受影响。
                self._dead = True
                return None
        self._prefix = None
        return bytes(out)

    def flush(self) -> bytes | None:
        """流末：吐出解压器里剩余的字节。``None`` 语义同 `decode`。"""
        if self._dead:
            return None
        if self._decompressor is None:
            return b""
        try:
            return self._decompressor.flush()
        except zlib.error:
            return None


class _SseTap:
    """一条流的 tee 状态：增量解码器 + feeder + 攒 body 的缓冲。

    callable 本体是 :meth:`tee`（bound method），装进 `flow.response.stream` 后
    每个 chunk 走一趟：解码 → 喂 feeder → 事件经回调上桥 → 原始字节攒 buf →
    原样还给转发路径。所有字段只在 mitm 线程上碰，不加锁。
    """

    def __init__(
        self,
        charset: str,
        content_encoding: str,
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
        self._chunk_decoder = _ChunkDecoder(content_encoding)
        if self._chunk_decoder is not None and content_encoding.strip().lower() not in (
            _KNOWN_ENCODINGS
        ):
            # br / zstd 等：标准库解不了，事件解析降级关闭（转发照常，见
            # `_ChunkDecoder`）。不吭声的话用户只会看到「事件页空的」，没法查。
            log.info(
                "SSE 流带 Content-Encoding: %s，无法增量解压，事件解析关闭"
                "（转发不受影响）",
                content_encoding,
            )
        self._feeder = SseFeeder()
        self._on_events = on_events
        self._on_end = on_end
        self.buf = bytearray()
        # 超 :data:`SSE_BODY_LIMIT` 后置真：停止攒 body，解析推送继续。
        self.buf_overflowed = False
        self._ended = False
        self._parsing = True
        self.received = False
        self.streamed = True

    def tee(self, chunk: bytes) -> bytes:
        if self._ended:
            return chunk
        if not chunk:
            # 流末约定：mitmproxy 在转发路径收尾时以空 chunk 通知 callable。
            self._flush()
            return chunk
        self.received = True
        if not self.buf_overflowed:
            if len(self.buf) + len(chunk) > SSE_BODY_LIMIT:
                self.buf += chunk[: max(0, SSE_BODY_LIMIT - len(self.buf))]
                self.buf_overflowed = True
                log.info(
                    "SSE body 超过 %d 字节，停止攒 body（事件推送继续）", SSE_BODY_LIMIT
                )
            else:
                self.buf += chunk
        # 先剥 Content-Encoding 再文本解码：stream callable 拿到的是未解压的
        # 转发 chunk（issues #12）。``None`` = 解不出来，这段放弃解析。
        if self._parsing:
            try:
                plain = self._chunk_decoder.decode(chunk)
                if plain is None:
                    self._parsing = False
                else:
                    events = self._feeder.feed(self._decoder.decode(plain))
                    if events:
                        self._on_events(events)
            except Exception:
                # This is an observer, never part of the forwarding contract.
                self._parsing = False
                self._feeder = SseFeeder()
                log.warning(
                    "SSE parsing disabled; wire bytes are still forwarded",
                    exc_info=True,
                )
        return chunk

    def _flush(self) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if self._parsing:
                tail = self._chunk_decoder.flush()
                if tail is not None:
                    events = [
                        *self._feeder.feed(self._decoder.decode(tail)),
                        *self._feeder.feed(self._decoder.decode(b"", final=True)),
                        *self._feeder.flush(),
                    ]
                    if events:
                        self._on_events(events)
        except Exception:
            log.warning("SSE final parsing failed", exc_info=True)
        finally:
            self._feeder = SseFeeder()
            try:
                self._on_end()
            except Exception:
                log.warning("SSE end notification failed", exc_info=True)


class FerretSseAddon:
    """把事件流的 `flow.response.stream` 换成 callable，边转发边解析边推送。

    挂点必须是 `responseheaders`：mitmproxy 在源码注释里写明 stream 包装要赶在
    `response` 钩子之前设，`response` 里设已晚（转发已经开始，前面的 chunk 就
    漏过去了）。

    存档保留有界尾部；body 保留有界前缀，截断标记随 flow 保存。flow 从
    View 移除时清掉对应条目。
    """

    def __init__(self, bridge: MitmRuntime | None = None) -> None:
        # bridge 可后置注入：master 装配时不认识 runtime（master 由 runtime 造），
        # runtime 在挂 UiBridgeAddon 时一并补上。没补就等于推送关掉，存档照常。
        self.bridge = bridge
        self.on_flow_updated: Callable[[HTTPFlow], None] | None = None
        self._events: dict[str, deque[SseEvent]] = {}
        self._event_counts: dict[str, int] = {}
        self._archive_sizes: dict[str, int] = {}
        self._taps: dict[str, _SseTap] = {}
        self._flows: dict[str, HTTPFlow] = {}

    # —— addon 钩子 ——

    def responseheaders(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None or not is_event_stream(
            response.headers.get("content-type")
        ):
            return
        # 已被重写引擎（文件映射 / 替换响应）就地作答的流量：mitmproxy 对预作答
        # 也会 emulate 一次 `responseheaders`，而静态替换体哪怕是 text/event-stream
        # 也不是流式语义，tee 没有意义还会把整条响应钉在流式管道上（§13 风险一）。
        if flow.metadata.get(REWRITE_ANSWERED_KEY):
            return
        # 断点拦在响应期时 stream 已开始向客户端转发（原生行为如此）——但断点拦的是
        # `response` 钩子，而 `responseheaders` 更早，这里登记不受影响。
        tap = _SseTap(
            # charset 单独拆出来：整串 content-type（带参数）喂给 codecs 会
            # LookupError，而 `_SseTap` 只认编码名。
            _charset_of(response.headers.get("content-type", "")),
            # Content-Encoding 也得单独给：stream callable 收到的是未解压的
            # 转发 chunk（issues #12），tee 侧自己剥。
            response.headers.get("content-encoding", "identity"),
            on_events=lambda events, fid=flow.id: self._record_events(fid, events),
            on_end=lambda fid=flow.id: self._finish(fid),
        )
        self._taps[flow.id] = tap
        self._flows[flow.id] = flow
        self._events.setdefault(flow.id, deque())
        self._event_counts[flow.id] = 0
        self._archive_sizes.setdefault(flow.id, 0)
        flow.metadata[SSE_EVENT_COUNT_KEY] = 0
        tap.streamed = response.raw_content is None
        if tap.streamed:
            response.stream = tap.tee
        self._emit_started(flow.id)

    def response(self, flow: HTTPFlow) -> None:
        tap = self._taps.get(flow.id)
        if tap is None:
            return
        # Static Mock/script answers never enter the streaming callable. Read
        # their already available wire body once before the same finalizer.
        if not tap.received and flow.response is not None:
            content = flow.response.raw_content
            if content:
                tap.tee(content)
        tap.tee(b"")

    def error(self, flow: HTTPFlow) -> None:
        tap = self._taps.get(flow.id)
        if tap is not None:
            tap.tee(b"")

    def done(self) -> None:
        for tap in list(self._taps.values()):
            tap.tee(b"")

    # —— 对内 ——

    def _record_events(self, flow_id: str, events: list[SseEvent]) -> None:
        archive = self._events.get(flow_id)
        if archive is None:
            return
        size = self._archive_sizes[flow_id]
        for event in events:
            archive.append(event)
            size += _event_size(event)
        truncated = False
        while archive and (len(archive) > SSE_EVENT_LIMIT or size > SSE_ARCHIVE_LIMIT):
            size -= _event_size(archive.popleft())
            truncated = True
        self._archive_sizes[flow_id] = size
        if events:
            self._event_counts[flow_id] = events[-1].index + 1
        flow = self._flows.get(flow_id)
        if events and flow is not None:
            flow.metadata[SSE_EVENT_COUNT_KEY] = events[-1].index + 1
        if truncated and flow is not None:
            flow.metadata[SSE_EVENTS_TRUNCATED_KEY] = True
        if self.bridge is None:
            return
        for event in events:
            self._emit("sse_event", flow_id, event)

    def _finish(self, flow_id: str) -> None:
        tap = self._taps.pop(flow_id, None)
        flow = self._flows.pop(flow_id, None)
        if tap is None or flow is None:
            return
        # 把 tee 攒下的字节补回 body：响应体页 / 保存 / HAR 导出都读它。超限的流
        # 攒的是前缀，也补回去 —— 有前缀总比空 body 强（界面能显示「前 10 MB」）。
        if flow.response is not None:
            flow.response.data.content = bytes(tap.buf)
            # Keep the streamed marker for body-rewrite policy without keeping
            # the bound method (and its decoder/body buffers) alive forever.
            flow.response.stream = tap.streamed
        if tap.buf_overflowed:
            flow.metadata[SSE_BODY_TRUNCATED_KEY] = True
        tap.buf.clear()
        # Static SSE replies are parsed after View.response. Account for the
        # resulting archive and republish the final body through View.update.
        if self.on_flow_updated is not None:
            self.on_flow_updated(flow)
        if self.bridge is not None:
            self._emit("sse_ended", flow_id)

    def _emit_started(self, flow_id: str) -> None:
        if self.bridge is not None:
            self._emit("sse_started", flow_id)

    def _emit(self, name: str, *args) -> None:
        if self.bridge is None:
            return
        post = getattr(self.bridge, "post_ui_event", None)
        if post is not None:
            post(name, *args)
        else:
            getattr(self.bridge, name).emit(*args)

    def memory_size(self, flow: Flow) -> int:
        return self._archive_sizes.get(flow.id, 0)

    def forget(self, flow_id: str) -> None:
        """flow 从 View 移除时清存档。由 facade 的 remove/clear 路径调用。"""
        self._events.pop(flow_id, None)
        self._event_counts.pop(flow_id, None)
        self._archive_sizes.pop(flow_id, None)
        tap = self._taps.get(flow_id)
        if tap is not None:
            tap.tee(b"")

    def flow_removed(self, flow: Flow) -> None:
        self.forget(flow.id)

    def clear(self) -> None:
        """整表清空（`clear_flows` 那条路）。"""
        self.done()
        self._events.clear()
        self._event_counts.clear()
        self._archive_sizes.clear()

    def events(self, flow_id: str) -> list[SseEvent]:
        """容量以内的最近事件；绝对 index 保留，可辨别被驱逐的前缀。"""
        return list(self._events.get(flow_id, []))

    def event_count(self, flow_id: str) -> int | None:
        """已有存档的事件总数；没有存档返回 None，调用方才可解历史 body。"""
        return self._event_counts.get(flow_id)


def _event_size(event: SseEvent) -> int:
    # Include duplicated raw/data strings, accounting for worst-case Unicode.
    return 4 * sum(
        len(value)
        for value in (event.raw, event.data, event.comment, event.id, event.event)
    )


def _charset_of(content_type: str) -> str:
    """`text/event-stream; charset=utf-8` → ``utf-8``；没有参数回 UTF-8（规范缺省）。"""
    for part in content_type.split(";")[1:]:
        name, sep, value = part.strip().partition("=")
        if sep and name.strip().lower() == "charset":
            return value.strip().strip('"') or "utf-8"
    return "utf-8"
