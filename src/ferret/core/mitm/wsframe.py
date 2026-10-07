"""WebSocket frames and close info as plain values, safe to hand to Qt.

`flow.websocket` 一直由 mitmproxy 自动填充（`WebSocketData`），所以「取帧」本身不需要
新 addon。这个模块解决的是**另一件事**：把帧从活 flow 上摘下来，变成不带任何 flow /
master 引用的冻结值对象，界面才拿得到。

为什么非得摘一层（AGENTS.md §3）：

* 三个 websocket 钩子跑在 mitm 的 asyncio 线程上。把 flow 直接 emit 过去，Qt 侧就得在
  自己线程上读活 flow 的 `websocket.messages` —— 正是红线禁止的事。
* `WebSocketMessage.content` 在 `websocket_message` 钩子里**还是用户可改的**（下游
  addon 可以 `drop()` 或改写它）。当场取值才对得上「这一帧当时是什么」。
* `bytes` 不可变，所以取完值之后引用过界是安全的；`WsFrame` 自己是 frozen slots。

与 `intercept.py` 的 `RequestEdit` / `ResponseEdit` 同一种东西，方向相反：那两个是
界面 → flow，这个是 flow → 界面。
"""

from __future__ import annotations

from dataclasses import dataclass

from ferret.core.mitm.bindings import Opcode, WebSocketData, WebSocketMessage

# 界面最多保留多少帧。聊天型 / 行情型 WS 一条连接刷出上万帧是常态，而每帧都要在表格里
# 占一行、内容还留在内存里。理由与 `intercept.py::INTERCEPT_LIMIT` 同构：原生一个上限
# 都没有（mitmproxy 自己的 TUI 逐条翻页），闸门只能由 ferret 加。
#
# 界面按条数和字节预算取尾部预览；内核侧（`view.py::_trim_websocket`）按同一组预算
# 裁剪原生 messages 列表 —— 内核保留 = 界面可见 = 导出内容，三者一致，被裁掉的帧
# 不再存在于任何副本里。调用 `ws_frames` 不传 limit 时仍返回全部保留帧。
WS_FRAME_LIMIT = 2000
WS_PREVIEW_BYTES = 64 * 1024
WS_WINDOW_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class WsFrame:
    """One assembled WebSocket message, detached from its flow.

    `index` 是它在 `WebSocketData.messages` 里的下标，也就是界面上的帧号：原生
    `WebSocketMessage` 自己不带序号，而「第几帧」是排查时最常用的坐标。

    `opcode` 存 int 而不是 `Opcode`：值对象要能原样穿过 Qt 信号、也能塞进 `.flow`
    状态字典，纯 int 最省事；要拿名字用 :func:`opcode_name`。
    """

    index: int
    from_client: bool
    opcode: int
    content: bytes
    timestamp: float
    dropped: bool
    injected: bool
    original_size: int | None = None

    @property
    def is_text(self) -> bool:
        """`True` 表示这帧由 TEXT 帧拼出来，内容按 UTF-8 解读才有意义。"""
        return self.opcode == int(Opcode.TEXT)

    @property
    def size(self) -> int:
        return (
            self.original_size if self.original_size is not None else len(self.content)
        )

    @property
    def truncated(self) -> bool:
        return self.size > len(self.content)

    def text(self, *, errors: str = "replace") -> str:
        """内容按 UTF-8 宽松解码。

        刻意不照搬原生 `WebSocketMessage.text` —— 那个属性对非 TEXT 帧抛
        `AttributeError`、对畸形字节抛 `UnicodeDecodeError`，而这里的调用方是界面：
        抓到什么就得显示什么，一帧坏字节不该让整张表塌掉（和 §2「解码 body 一律
        `strict=False`」同一个道理）。
        """
        return self.content.decode("utf-8", errors=errors)


@dataclass(frozen=True, slots=True)
class WsClose:
    """Why and when a WebSocket connection ended (native ``WebSocketData`` tail).

    四个字段一起走：`close_code` 单独看不出是谁关的，`closed_by_client` 单独看不出
    原因。计划里 `websocket_end` 是发四个参数的信号，这里收成一个值对象 —— 多一个
    `timestamp_end` 就得多改一处信号签名，而界面底部那一行本来就要显示它。
    """

    #: `True` 客户端关的，`False` 服务端关的，`None` 还没关。
    closed_by_client: bool | None = None
    close_code: int | None = None
    close_reason: str = ""
    timestamp_end: float | None = None

    @property
    def is_closed(self) -> bool:
        return self.timestamp_end is not None or self.close_code is not None


def to_frame(
    message: WebSocketMessage, index: int, *, preview: bool = False
) -> WsFrame:
    """Copy one native message into a frozen value. **Call on the mitm thread.**"""
    return WsFrame(
        index=index,
        from_client=message.from_client,
        opcode=int(message.type),
        content=bytes(
            message.content[:WS_PREVIEW_BYTES] if preview else message.content
        ),
        timestamp=message.timestamp,
        dropped=message.dropped,
        injected=message.injected,
        original_size=len(message.content) if preview else None,
    )


def ws_frames(data: WebSocketData | None, *, limit: int | None = None) -> list[WsFrame]:
    """Full history, or a bounded tail preview. **Call on the mitm thread.**

    `limit` 同时启用消息数、单帧和窗口字节预算；未传时保留完整快照语义。
    """
    if data is None:
        return []
    if limit is not None:
        frames = []
        used = 0
        for index in range(
            len(data.messages) - 1, max(-1, len(data.messages) - limit - 1), -1
        ):
            frame = to_frame(data.messages[index], index, preview=True)
            if frames and used + len(frame.content) > WS_WINDOW_BYTES:
                break
            used += len(frame.content)
            frames.append(frame)
        frames.reverse()
        return frames
    return [to_frame(message, index) for index, message in enumerate(data.messages)]


def latest_frame(
    data: WebSocketData | None, *, preview: bool = False
) -> WsFrame | None:
    """The frame that just arrived. **Call on the mitm thread.**

    `websocket_message` 钩子的约定是「最新一帧在 `messages[-1]`」，所以整表不用重取，
    界面那边就能走增量 `appendRow`。空表返回 None —— 钩子理论上不会这么调，但拿
    `[-1]` 去撞 IndexError 不是这一层该出的错。
    """
    if data is None or not data.messages:
        return None
    index = len(data.messages) - 1
    return to_frame(data.messages[index], index, preview=preview)


def ws_close(data: WebSocketData | None) -> WsClose:
    """Pull the close info off a native ``WebSocketData``. **Mitm thread only.**"""
    if data is None:
        return WsClose()
    return WsClose(
        closed_by_client=data.closed_by_client,
        close_code=data.close_code,
        close_reason=data.close_reason or "",
        timestamp_end=data.timestamp_end,
    )


def opcode_name(opcode: int) -> str:
    """``1`` → ``"TEXT"``；不认识的码回 ``"0x9"`` 这样的十六进制。

    刻意不译：TEXT / BINARY / PING / PONG / CLOSE 是 RFC 6455 §5.2 的帧类型名，和
    HTTP 方法名一样属于协议字面量（`fields.py` 里的 `Method` 也不译）。
    """
    try:
        return Opcode(opcode).name
    except ValueError:
        return f"0x{opcode:x}"
