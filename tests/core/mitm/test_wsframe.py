"""帧从活 flow 上摘下来这一步的契约（`core/mitm/wsframe.py`）。

这里钉三件事：

一、**取值是当场取的，不是留引用**。`WebSocketMessage.content` 在
`websocket_message` 钩子里还能被下游 addon 改写，之后 Qt 侧再读就已经不是「那一帧」
了；`WsFrame` 必须在钩子里就冻结。

二、**一帧都不许少**。`ws_frames` 恒返回全部帧，`WS_FRAME_LIMIT` 只是界面显示上限
——静默少几行是最难查的那类 bug（`wsframe.py` 模块 docstring）。

三、**界面拿到什么都得能显示**。`text()` 刻意比原生 `WebSocketMessage.text` 宽松：
非 TEXT 帧、畸形字节都不该让整张表塌掉。
"""

import unittest
from dataclasses import FrozenInstanceError

from mitmproxy.test import tflow

from ferret.core.mitm import bindings
from ferret.core.mitm.bindings import Opcode, WebSocketData, WebSocketMessage
from ferret.core.mitm.wsframe import (
    WS_FRAME_LIMIT,
    WsClose,
    WsFrame,
    latest_frame,
    opcode_name,
    to_frame,
    ws_close,
    ws_frames,
)


class BindingsExportTests(unittest.TestCase):
    """`bindings.py` 是唯一合法的 `from mitmproxy import` 处（AGENTS.md §4）。

    这几个符号少一个，`wsframe.py` 就只能自己去 import mitmproxy —— 正是红线。
    """

    def test_websocket_symbols_are_exported(self) -> None:
        for name in ("Opcode", "WebSocketData", "WebSocketMessage"):
            with self.subTest(name=name):
                self.assertIn(name, bindings.__all__)
                self.assertIsNotNone(getattr(bindings, name))

    def test_head_assemblers_are_exported(self) -> None:
        """原生**没有**光秃秃的 `assemble`：只有 request/response 各自的整体与头部。

        计划里写的是「导出 `assemble`」，落地成这两个 `*_head`（原始报文那一页拼的
        就是头部）。写在这里，免得下次又去找一个不存在的名字。
        """
        for name in ("assemble_request_head", "assemble_response_head"):
            with self.subTest(name=name):
                self.assertIn(name, bindings.__all__)
                self.assertTrue(callable(getattr(bindings, name)))


class ToFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.message = WebSocketMessage(Opcode.TEXT, True, b"hello", 946681204.0)

    def test_every_field_is_carried_over(self) -> None:
        frame = to_frame(self.message, 7)
        self.assertEqual(frame.index, 7)
        self.assertTrue(frame.from_client)
        self.assertEqual(frame.opcode, int(Opcode.TEXT))
        self.assertEqual(frame.content, b"hello")
        self.assertEqual(frame.timestamp, 946681204.0)
        self.assertFalse(frame.dropped)
        self.assertFalse(frame.injected)

    def test_dropped_and_injected_survive(self) -> None:
        """两个标志都是界面要标出来的：被丢掉的帧对端根本没收到。"""
        message = WebSocketMessage(
            Opcode.BINARY, False, b"\x00", 1.0, dropped=True, injected=True
        )
        frame = to_frame(message, 0)
        self.assertTrue(frame.dropped)
        self.assertTrue(frame.injected)

    def test_the_frame_does_not_track_later_edits(self) -> None:
        """核心那一条：下游 addon 改写内容，已发出的帧不该跟着变。"""
        frame = to_frame(self.message, 0)
        self.message.content = b"tampered"
        self.message.drop()
        self.assertEqual(frame.content, b"hello")
        self.assertFalse(frame.dropped)

    def test_the_frame_is_frozen(self) -> None:
        """值对象要能安全地穿过 Qt 信号 —— 收到的人不该能改它。"""
        frame = to_frame(self.message, 0)
        with self.assertRaises(FrozenInstanceError):
            frame.content = b"nope"  # type: ignore

    def test_opcode_is_a_plain_int(self) -> None:
        """存 int 而不是 `Opcode`：值对象还要塞进 `.flow` 状态字典。"""
        frame = to_frame(self.message, 0)
        self.assertIs(type(frame.opcode), int)


class WsFramePropertyTests(unittest.TestCase):
    @staticmethod
    def frame(opcode: int, content: bytes) -> WsFrame:
        return WsFrame(
            index=0,
            from_client=True,
            opcode=opcode,
            content=content,
            timestamp=1.0,
            dropped=False,
            injected=False,
        )

    def test_is_text_only_for_text_frames(self) -> None:
        self.assertTrue(self.frame(int(Opcode.TEXT), b"hi").is_text)
        self.assertFalse(self.frame(int(Opcode.BINARY), b"hi").is_text)

    def test_size_counts_bytes_not_characters(self) -> None:
        # 三个汉字九个字节：界面上那一列写的是线上的字节数。
        self.assertEqual(self.frame(int(Opcode.TEXT), "行情推".encode()).size, 9)

    def test_text_decodes_utf8(self) -> None:
        self.assertEqual(self.frame(int(Opcode.TEXT), "行情".encode()).text(), "行情")

    def test_text_survives_malformed_bytes(self) -> None:
        """原生 `.text` 这里会抛 `UnicodeDecodeError`，界面不能因此空一格。"""
        self.assertEqual(self.frame(int(Opcode.TEXT), b"ok\xff").text(), "ok�")

    def test_text_works_on_binary_frames_too(self) -> None:
        """原生 `.text` 对非 TEXT 帧抛 `AttributeError`；这里得给出能看的东西。"""
        self.assertEqual(self.frame(int(Opcode.BINARY), b"\x41\x42").text(), "AB")

    def test_text_can_be_made_strict(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            self.frame(int(Opcode.TEXT), b"\xff").text(errors="strict")


class WsFramesTests(unittest.TestCase):
    def test_no_websocket_data_means_no_frames(self) -> None:
        """普通 HTTP 流量的 `flow.websocket` 是 None —— 消息页得优雅地空着。"""
        self.assertEqual(ws_frames(None), [])

    def test_frames_are_indexed_in_order(self) -> None:
        frames = ws_frames(tflow.twebsocket())
        self.assertEqual([f.index for f in frames], [0, 1, 2])
        self.assertEqual(
            [f.content for f in frames],
            [b"hello binary", b"hello text", b"it's me"],
        )
        self.assertEqual([f.from_client for f in frames], [True, True, False])

    def test_every_frame_comes_back_even_past_the_display_limit(self) -> None:
        """上限属于界面，不属于这一层：这里丢一帧，用户永远查不出来。"""
        data = WebSocketData()
        total = WS_FRAME_LIMIT + 5
        data.messages = [
            WebSocketMessage(Opcode.TEXT, True, str(i).encode(), float(i))
            for i in range(total)
        ]
        frames = ws_frames(data)
        self.assertEqual(len(frames), total)
        self.assertEqual(frames[-1].index, total - 1)

    def test_empty_message_list_is_not_an_error(self) -> None:
        """101 刚握上手、还没发帧的那一瞬间。"""
        self.assertEqual(ws_frames(WebSocketData()), [])


class LatestFrameTests(unittest.TestCase):
    def test_the_last_message_is_the_new_one(self) -> None:
        """`websocket_message` 钩子的约定：新到的那帧在 `messages[-1]`。"""
        frame = latest_frame(tflow.twebsocket())
        assert frame is not None
        self.assertEqual(frame.index, 2)
        self.assertEqual(frame.content, b"it's me")

    def test_index_matches_the_position_in_the_full_list(self) -> None:
        """增量 `appendRow` 靠这个下标对齐，错一位帧号就整列错。"""
        data = WebSocketData()
        for i in range(4):
            data.messages.append(
                WebSocketMessage(Opcode.TEXT, True, str(i).encode(), float(i))
            )
            frame = latest_frame(data)
            assert frame is not None
            self.assertEqual(frame.index, i)

    def test_no_data_and_no_messages_both_give_none(self) -> None:
        self.assertIsNone(latest_frame(None))
        self.assertIsNone(latest_frame(WebSocketData()))


class WsCloseTests(unittest.TestCase):
    def test_no_data_means_every_field_is_empty(self) -> None:
        info = ws_close(None)
        self.assertIsNone(info.closed_by_client)
        self.assertIsNone(info.close_code)
        self.assertEqual(info.close_reason, "")
        self.assertIsNone(info.timestamp_end)
        self.assertFalse(info.is_closed)

    def test_an_open_connection_is_not_closed(self) -> None:
        self.assertFalse(ws_close(WebSocketData()).is_closed)

    def test_close_info_is_carried_over(self) -> None:
        info = ws_close(tflow.twebsocket())
        self.assertFalse(info.closed_by_client)
        self.assertEqual(info.close_code, 1000)
        self.assertEqual(info.close_reason, "Close Reason")
        self.assertEqual(info.timestamp_end, 946681205)
        self.assertTrue(info.is_closed)

    def test_native_none_reason_becomes_an_empty_string(self) -> None:
        """原生 `close_reason` 可以是 None，界面上不该冒出一个 "None"。"""
        data = WebSocketData()
        data.close_code = 1006
        self.assertEqual(ws_close(data).close_reason, "")

    def test_a_code_alone_already_counts_as_closed(self) -> None:
        """异常断开时 `timestamp_end` 可能还没写上，但连接确实已经没了。"""
        data = WebSocketData()
        data.close_code = 1006
        self.assertTrue(ws_close(data).is_closed)

    def test_the_close_info_is_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            ws_close(None).close_code = 1000  # type: ignore

    def test_default_construction_is_the_open_state(self) -> None:
        self.assertEqual(WsClose(), ws_close(None))


class OpcodeNameTests(unittest.TestCase):
    def test_known_opcodes_get_their_rfc_name(self) -> None:
        self.assertEqual(opcode_name(int(Opcode.TEXT)), "TEXT")
        self.assertEqual(opcode_name(int(Opcode.BINARY)), "BINARY")
        self.assertEqual(opcode_name(int(Opcode.CLOSE)), "CLOSE")

    def test_unknown_opcodes_fall_back_to_hex(self) -> None:
        """RFC 6455 §5.2 留了保留段。显示 `0x3` 也比整张表报错好。"""
        self.assertEqual(opcode_name(3), "0x3")
        self.assertEqual(opcode_name(11), "0xb")


if __name__ == "__main__":
    unittest.main()
