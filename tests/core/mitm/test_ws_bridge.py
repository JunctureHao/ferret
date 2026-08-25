"""`UiBridgeAddon` 那三个 websocket 钩子发出去的到底是什么。

`View` 一个 websocket 钩子都没有（只有 `requestheaders` / `error` / `response` /
`tcp_*` / `udp_*` / `update`），所以帧到达这件事没有原生信号可借，只能自己补 emit。
既然是自己补的，就得钉住两件事：

* **过界的只能是值对象**。钩子跑在 mitm 的 asyncio 线程上，把 flow 发过去等于让 Qt
  侧读活 flow（AGENTS.md §3 红线）。这里逐个断言载荷类型 —— 类型退化成 flow 的那天，
  症状会是偶发的空白面板，不是异常。
* **`done()` 之后不许再发**。内核换代时旧 addon 还挂在旧 master 上，多发一帧就会被
  记到新一代的界面上。

这里刻意不起代理：三个钩子都只读 `flow.websocket`，直接调用即可。
"""

import unittest

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.bindings import Opcode, WebSocketData, WebSocketMessage
from ferret.core.mitm.runtime import UiBridgeAddon
from ferret.core.mitm.wsframe import WsClose, WsFrame


class _FakeMaster:
    """三个钩子一个都不碰 master —— 只有 `running()` 要它，这里不走那条路。"""


class WebsocketBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = MitmRuntime()
        self.addon = UiBridgeAddon(
            self.runtime.view,
            self.runtime,
            _FakeMaster(),  # type: ignore
            generation=1,
        )
        self.addCleanup(self.addon.disconnect)

        self.started: list[tuple] = []
        self.frames: list[tuple] = []
        self.closed: list[tuple] = []
        self.runtime.websocket_started.connect(lambda *a: self.started.append(a))
        self.runtime.websocket_frame.connect(lambda *a: self.frames.append(a))
        self.runtime.websocket_closed.connect(lambda *a: self.closed.append(a))

    def test_start_carries_only_the_flow_id(self) -> None:
        flow = tflow.twebsocketflow()
        self.addon.websocket_start(flow)
        self.assertEqual(self.started, [(flow.id,)])

    def test_message_emits_the_newest_frame_as_a_value(self) -> None:
        flow = tflow.twebsocketflow()
        self.addon.websocket_message(flow)

        self.assertEqual(len(self.frames), 1)
        flow_id, frame = self.frames[0]
        self.assertEqual(flow_id, flow.id)
        self.assertIsInstance(frame, WsFrame)
        self.assertEqual(frame.index, 2)
        self.assertEqual(frame.content, b"it's me")
        self.assertFalse(frame.from_client)

    def test_no_flow_ever_crosses_the_boundary(self) -> None:
        """红线体检：载荷里不该出现任何带 `request` / `websocket` 的东西。"""
        flow = tflow.twebsocketflow()
        self.addon.websocket_start(flow)
        self.addon.websocket_message(flow)
        self.addon.websocket_end(flow)

        payloads = [
            arg for args in (*self.started, *self.frames, *self.closed) for arg in args
        ]
        self.assertTrue(payloads)
        for payload in payloads:
            with self.subTest(payload=type(payload).__name__):
                self.assertIsInstance(payload, str | WsFrame | WsClose)
                self.assertFalse(hasattr(payload, "websocket"))
                self.assertFalse(hasattr(payload, "request"))

    def test_each_message_hook_reports_the_frame_that_just_arrived(self) -> None:
        """增量刷新的前提：钩子调一次只发一帧，帧号跟着往后走。"""
        flow = tflow.twebsocketflow(messages=False)
        flow.websocket = WebSocketData()
        for i in range(3):
            flow.websocket.messages.append(
                WebSocketMessage(Opcode.TEXT, True, str(i).encode(), float(i))
            )
            self.addon.websocket_message(flow)

        self.assertEqual([frame.index for _, frame in self.frames], [0, 1, 2])
        self.assertEqual(
            [frame.content for _, frame in self.frames], [b"0", b"1", b"2"]
        )

    def test_a_frameless_message_hook_stays_quiet(self) -> None:
        """理论上不会这么调，但拿 `messages[-1]` 撞 IndexError 不是桥该出的错。"""
        flow = tflow.twebsocketflow(messages=False)
        flow.websocket = WebSocketData()
        self.addon.websocket_message(flow)
        self.assertEqual(self.frames, [])

    def test_a_plain_http_flow_does_not_break_the_hooks(self) -> None:
        """`~http` 底座下 WS 与普通流量同列，钩子拿到 `websocket is None` 也得活着。"""
        flow = tflow.tflow(resp=True)
        self.addon.websocket_message(flow)
        self.addon.websocket_end(flow)

        self.assertEqual(self.frames, [])
        self.assertEqual(len(self.closed), 1)
        self.assertEqual(self.closed[0][1], WsClose())

    def test_end_carries_the_whole_close_story(self) -> None:
        flow = tflow.twebsocketflow(close_code=1006, close_reason="gone")
        self.addon.websocket_end(flow)

        self.assertEqual(len(self.closed), 1)
        flow_id, info = self.closed[0]
        self.assertEqual(flow_id, flow.id)
        self.assertIsInstance(info, WsClose)
        self.assertEqual(info.close_code, 1006)
        self.assertEqual(info.close_reason, "gone")
        self.assertFalse(info.closed_by_client)
        self.assertTrue(info.is_closed)

    def test_nothing_is_emitted_after_done(self) -> None:
        """换代时旧 addon 还挂在旧 master 上，多发一帧就记到新一代的界面上了。"""
        flow = tflow.twebsocketflow()
        self.addon.done()

        self.addon.websocket_start(flow)
        self.addon.websocket_message(flow)
        self.addon.websocket_end(flow)

        self.assertEqual(self.started, [])
        self.assertEqual(self.frames, [])
        self.assertEqual(self.closed, [])

    def test_the_frame_is_detached_from_the_live_message(self) -> None:
        """发出去之后下游 addon 还能 `drop()`；已发的帧不该跟着变。"""
        flow = tflow.twebsocketflow()
        self.addon.websocket_message(flow)
        _, frame = self.frames[0]

        assert flow.websocket is not None
        flow.websocket.messages[-1].content = b"tampered"
        flow.websocket.messages[-1].drop()

        self.assertEqual(frame.content, b"it's me")
        self.assertFalse(frame.dropped)


if __name__ == "__main__":
    unittest.main()
