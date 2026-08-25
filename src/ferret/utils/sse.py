"""Server-Sent Events 事件流解析：一份 `text/event-stream` body → 事件列表。

**纯函数、零依赖**（输入解码后的 `str`，输出冻结值对象），满足 AGENTS.md §4「新增
utils 不再加依赖」，也不去沾 `http_parser.py` 误引 `core.mitm.bindings` 的问题。

mitmproxy 默认不开 streaming，会把响应体整份缓冲下来，所以流量走完时整个事件流都在
`Response.content` 里 —— 界面上一张静态表就够了，不需要边收边追加。若将来给 SSE 开了
`stream`，body 会是空的，那时消息页显示「响应体以流式转发，未缓冲」而不是空表。

## 与规范的关系

按 WHATWG HTML「Interpreting an event stream」实现，唯一刻意的偏差是**注释块也产出
事件**：规范里「没有 data 就不派发」，而 SSE 的心跳恰好是纯注释行（`: keep-alive`），
按规范解完一份心跳流会得到空列表 —— 界面上看着就像什么都没抓到。这一层是给人看的，
所以每个非空块都产出一条，靠 `data` / `comment` 两个字段区分是消息还是心跳。
"""

from __future__ import annotations

from dataclasses import dataclass

#: `Content-Type` 的判定前缀。带参数的 `text/event-stream; charset=utf-8` 也算。
SSE_CONTENT_TYPE = "text/event-stream"

#: 没有显式 `event:` 字段的数据块的事件名（规范定的缺省值）。
DEFAULT_EVENT = "message"


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
    return not rest or rest[0] in ";, 	"


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


def parse_sse(text: str) -> list[SseEvent]:
    """事件流文本 → 事件列表。

    末尾没有空行时最后一段也要派发：规范面对的是一条还在流动的连接（半个块要等后续
    字节），而这里拿到的是一份已经收完的 body，最后那段不派发就等于凭空少一条。

    `raw` 里的行分隔符统一成 `\\n` —— 界面上那一格显示的是块的原文，三种分隔符混排
    对读它的人没有信息量。
    """
    events: list[SseEvent] = []
    block = _Block()
    for line in _split_lines(text):
        if line:
            block.feed(line)
            continue
        if block.lines:
            events.append(block.build(len(events)))
        block = _Block()
    if block.lines:
        events.append(block.build(len(events)))
    return events
