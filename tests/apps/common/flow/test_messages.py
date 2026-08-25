"""消息页：WS 帧表、SSE 事件表、上限提示。

改造前这两种流量在详情面板里根本看不见，所以这里钉的大多是「有没有真的显示出来」，
而不是像素。四组重点：

* **帧表填对**。方向、`Opcode` 名、时间、大小、预览各占一列，`dropped` / `injected`
  必须在界面上有字眼 —— 被丢掉的帧对端根本没收到，这是结论级信息。
* **上限不许静默**。截断只发生在这一层（`ws_frames` 恒返回全部帧），超过
  `MESSAGE_ROW_LIMIT` 时提示里必须写着真实总数，否则「共 2000 帧」就是假话。
* **模式判定**。WS / SSE / 都不是，三态各自落在正确的堆叠页；SSE 开了 `stream`
  时 body 是空的，那时要说「未缓冲」而不是摆一张空表（后者看着像解析失败）。
* **增量追加**。行情型连接每秒几十帧，整表重建会把选中和滚动位置一起清掉。

`isVisible()` 在这里一律不可信：面板没 `show()` 过，祖先链上就是不可见的。判「藏没藏」
统一用 `isHidden()`（只看自己那一位显式隐藏标志）。翻译器**故意不装**，理由同
`test_fields.py` —— 断言比的是源码里的英文原文。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication, QTableWidget

from ferret.apps.common.flow.messages import (
    HEX_DUMP_LIMIT,
    MESSAGE_ROW_LIMIT,
    MessagesPane,
    frame_preview,
    frame_time,
    hex_dump,
    is_websocket,
    looks_like_json,
    preview_text,
)
from ferret.core.mitm import (
    WS_FRAME_LIMIT,
    WsClose,
    WsFrame,
    build_flow_detail,
    ws_frames,
)

# 帧表列号（和 `messages.py` 里的私有常量对齐；测试不该 import 私有名）。
NO, DIR, TYPE, TIME, SIZE, PREVIEW = range(6)

# 事件表列号。
E_NO, E_EVENT, E_ID, E_RETRY, E_SIZE, E_DATA = range(6)

# 堆叠页序号。
PAGE_PLACEHOLDER, PAGE_WS, PAGE_SSE = range(3)


def frame(
    index: int = 0,
    *,
    from_client: bool = True,
    opcode: int = 1,
    content: bytes = b"hi",
    timestamp: float = 1700000000.0,
    dropped: bool = False,
    injected: bool = False,
) -> WsFrame:
    return WsFrame(index, from_client, opcode, content, timestamp, dropped, injected)


def text_at(table: QTableWidget, row: int, column: int) -> str:
    """一个格子的文本。

    `QTableWidget.item()` 的返回是可空的，而这里**空格子一律是缺陷** —— 少填一列
    的症状是界面上一片空白，断言里先撞上更好。
    """
    item = table.item(row, column)
    assert item is not None, f"row {row} column {column} is empty"
    return item.text()


def tip_at(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    assert item is not None, f"row {row} column {column} is empty"
    return item.toolTip()


def sse_detail(body: str, content_type: str = "text/event-stream") -> dict:
    return {
        "id": "flow-1",
        "raw_state": {"websocket": None},
        "Response Content-Type": content_type,
        "Response Body Text": body,
    }


class PureHelperTests(unittest.TestCase):
    """表里表外都用得上的几个纯函数。"""

    def test_websocket_detection_reads_the_native_subtree(self) -> None:
        """判据是 `flow.websocket is not None`，不是「状态码 101」。

        101 只说明握手**请求**被接受了；`flow.websocket` 是 mitmproxy 真的架起 WS 层
        之后才填的，后者才对得上「有帧可看」。
        """
        self.assertTrue(is_websocket({"raw_state": {"websocket": {"messages": []}}}))
        self.assertFalse(is_websocket({"raw_state": {"websocket": None}}))

    def test_websocket_detection_survives_a_missing_state(self) -> None:
        """表格双击传过来的字典可能只有几个键。"""
        for data in ({}, {"raw_state": None}, {"raw_state": "?"}):
            with self.subTest(data=data):
                self.assertFalse(is_websocket(data))

    def test_a_real_websocket_flow_is_detected(self) -> None:
        detail = build_flow_detail(tflow.twebsocketflow())
        self.assertTrue(is_websocket(detail))

    def test_a_real_http_flow_is_not(self) -> None:
        self.assertFalse(is_websocket(build_flow_detail(tflow.tflow(resp=True))))

    def test_json_sniffing_only_looks_at_the_first_character(self) -> None:
        self.assertTrue(looks_like_json('{"a": 1}'))
        self.assertTrue(looks_like_json("  \n[1, 2]"))
        self.assertFalse(looks_like_json("key: value"))
        self.assertFalse(looks_like_json(""))

    def test_frame_time_keeps_milliseconds_and_drops_the_date(self) -> None:
        """一条连接上的帧彼此只差几毫秒，日期在每行重复一遍是纯噪音。"""
        text = frame_time(1700000000.123)
        self.assertRegex(text, r"^\d\d:\d\d:\d\d\.\d\d\d$")

    def test_frame_time_says_something_when_there_is_no_timestamp(self) -> None:
        self.assertEqual(frame_time(None), "-")
        self.assertEqual(frame_time(0), "-")

    def test_preview_collapses_whitespace_into_one_line(self) -> None:
        """格式化过的 JSON 直接塞进单元格只能看到第一行的 `{`。"""
        self.assertEqual(preview_text('{\n  "a": 1\n}'), '{ "a": 1 }')

    def test_a_long_preview_is_clipped_with_an_ellipsis(self) -> None:
        clipped = preview_text("x" * 5000)
        self.assertLess(len(clipped), 5000)
        self.assertTrue(clipped.endswith("…"))

    def test_a_binary_frame_previews_as_hex(self) -> None:
        text = frame_preview(frame(opcode=2, content=bytes(range(8))))
        self.assertEqual(text, "00 01 02 03 04 05 06 07")

    def test_a_text_frame_previews_as_text(self) -> None:
        self.assertEqual(frame_preview(frame(content=b'{"px": 1}')), '{"px": 1}')

    def test_a_malformed_text_frame_still_previews(self) -> None:
        """抓到什么就得显示什么 —— 一帧坏字节不该让整张表塌掉（AGENTS.md §2）。"""
        self.assertTrue(frame_preview(frame(content=b"\xff\xfe")))

    def test_hex_dump_lays_out_sixteen_bytes_per_line(self) -> None:
        dump = hex_dump(b"hello\x00world!!")

        self.assertEqual(dump.splitlines()[0].split("  ")[0], "00000000")
        self.assertIn("68 65 6c 6c 6f", dump)
        # 非可打印字节一律 "."，和「原始状态」页的 bytes 预览一个规矩。
        self.assertTrue(dump.endswith("|hello.world!!|"))

    def test_hex_dump_stops_at_its_limit(self) -> None:
        self.assertEqual(len(hex_dump(bytes(4096), limit=32).splitlines()), 2)


class WebsocketTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def cell(self, row: int, column: int) -> str:
        return text_at(self.pane.frame_table, row, column)

    def test_a_fresh_pane_is_not_applicable(self) -> None:
        """详情面板据此决定整页露不露面。"""
        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.count, 0)

    def test_every_frame_gets_a_row(self) -> None:
        frames = [frame(i, content=str(i).encode()) for i in range(4)]
        self.pane.show_websocket(frames, WsClose())

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_WS)
        self.assertEqual(self.pane.frame_table.rowCount(), 4)
        self.assertEqual(self.pane.count, 4)
        self.assertEqual([self.cell(r, NO) for r in range(4)], ["0", "1", "2", "3"])

    def test_the_row_reads_the_six_things_worth_a_column(self) -> None:
        self.pane.show_websocket(
            [frame(7, content=b'{"px": 1}', timestamp=1700000000.5)], WsClose()
        )

        self.assertEqual(self.cell(0, NO), "7")
        self.assertEqual(self.cell(0, TYPE), "TEXT")
        self.assertRegex(self.cell(0, TIME), r"^\d\d:\d\d:\d\d\.500$")
        self.assertEqual(self.cell(0, SIZE), "9b")
        self.assertEqual(self.cell(0, PREVIEW), '{"px": 1}')

    def test_direction_is_an_arrow_with_a_spelled_out_tooltip(self) -> None:
        """箭头认不出方向的人靠 tooltip；改造前这里是硬编码色值，主题一换就对不上。"""
        self.pane.show_websocket(
            [frame(0, from_client=True), frame(1, from_client=False)], WsClose()
        )

        self.assertEqual(self.cell(0, DIR), "↑")
        self.assertEqual(self.cell(1, DIR), "↓")
        table = self.pane.frame_table
        self.assertEqual(tip_at(table, 0, DIR), "Client → Server")
        self.assertEqual(tip_at(table, 1, DIR), "Server → Client")

    def test_opcodes_are_named_not_numbered(self) -> None:
        """RFC 6455 §5.2 的帧类型名，和 HTTP 方法名一样属于协议字面量。"""
        cases = {1: "TEXT", 2: "BINARY", 8: "CLOSE", 9: "PING", 10: "PONG"}
        self.pane.show_websocket(
            [frame(i, opcode=code) for i, code in enumerate(cases)], WsClose()
        )

        self.assertEqual(
            [self.cell(r, TYPE) for r in range(len(cases))], list(cases.values())
        )

    def test_an_unknown_opcode_falls_back_to_hex_instead_of_blank(self) -> None:
        self.pane.show_websocket([frame(0, opcode=0x9999)], WsClose())
        self.assertTrue(self.cell(0, TYPE))

    def test_a_dropped_frame_says_so_in_words(self) -> None:
        """被丢掉的帧对端根本没收到 —— 不该让人去猜一个没有图例的色点。"""
        self.pane.show_websocket([frame(0, dropped=True)], WsClose())
        self.assertIn("dropped", self.cell(0, TYPE))

    def test_an_injected_frame_says_so_too(self) -> None:
        self.pane.show_websocket([frame(0, injected=True)], WsClose())
        self.assertIn("injected", self.cell(0, TYPE))

    def test_an_ordinary_frame_carries_no_flag_noise(self) -> None:
        self.pane.show_websocket([frame(0)], WsClose())
        self.assertEqual(self.cell(0, TYPE), "TEXT")

    def test_selecting_a_text_frame_shows_its_whole_content(self) -> None:
        body = '{"a": ' + "1" * 500 + "}"
        self.pane.show_websocket([frame(0, content=body.encode())], WsClose())
        self.pane.frame_table.selectRow(0)

        self.assertEqual(self.pane.frame_detail.text(), body)

    def test_selecting_a_binary_frame_shows_a_hex_dump(self) -> None:
        self.pane.show_websocket(
            [frame(0, opcode=2, content=bytes(range(32)))], WsClose()
        )
        self.pane.frame_table.selectRow(0)

        self.assertEqual(len(self.pane.frame_detail.text().splitlines()), 2)
        self.assertIn("00000010", self.pane.frame_detail.text())

    def test_a_huge_binary_frame_says_how_much_was_clipped(self) -> None:
        size = HEX_DUMP_LIMIT + 1000
        self.pane.show_websocket([frame(0, opcode=2, content=bytes(size))], WsClose())
        self.pane.frame_table.selectRow(0)
        tail = self.pane.frame_detail.text().splitlines()[-1]

        self.assertIn(str(HEX_DUMP_LIMIT), tail)
        self.assertIn(str(size), tail)

    def test_switching_flows_clears_the_previous_frames(self) -> None:
        self.pane.show_websocket([frame(i) for i in range(5)], WsClose())
        self.pane.show_websocket([frame(0)], WsClose())

        self.assertEqual(self.pane.frame_table.rowCount(), 1)
        self.assertEqual(self.pane.count, 1)
        self.assertEqual(self.pane.frame_detail.text(), "")


class FrameLimitTests(unittest.TestCase):
    """截断只发生在这一层，而被截掉多少一定要说出来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_the_row_limit_matches_the_core_display_cap(self) -> None:
        """两处各写一个数迟早只改一边。"""
        self.assertEqual(MESSAGE_ROW_LIMIT, WS_FRAME_LIMIT)

    def test_a_stream_under_the_limit_gets_no_notice(self) -> None:
        self.pane.show_websocket([frame(i) for i in range(10)], WsClose())
        self.assertTrue(self.pane.frame_notice.isHidden())

    def test_an_over_limit_stream_keeps_the_newest_rows(self) -> None:
        """最新的帧才是排查时在看的东西。"""
        extra = 7
        frames = [
            frame(i, content=str(i).encode()) for i in range(MESSAGE_ROW_LIMIT + extra)
        ]
        self.pane.show_websocket(frames, WsClose())

        table = self.pane.frame_table
        self.assertEqual(table.rowCount(), MESSAGE_ROW_LIMIT)
        self.assertEqual(text_at(table, 0, NO), str(extra))
        self.assertEqual(
            text_at(table, MESSAGE_ROW_LIMIT - 1, NO),
            str(MESSAGE_ROW_LIMIT + extra - 1),
        )

    def test_the_notice_names_the_real_total(self) -> None:
        """静默少几行是最难查的那类 bug。"""
        total = MESSAGE_ROW_LIMIT + 7
        self.pane.show_websocket([frame(i) for i in range(total)], WsClose())

        self.assertFalse(self.pane.frame_notice.isHidden())
        self.assertIn(str(total), self.pane.frame_notice.text())
        self.assertIn(str(MESSAGE_ROW_LIMIT), self.pane.frame_notice.text())

    def test_the_count_reports_the_total_not_the_visible_rows(self) -> None:
        """导航项上那枚徽标读它 —— 显示 2000 而实际 2007 是假话。"""
        total = MESSAGE_ROW_LIMIT + 7
        self.pane.show_websocket([frame(i) for i in range(total)], WsClose())

        self.assertEqual(self.pane.count, total)
        self.assertEqual(self.pane.frame_table.rowCount(), MESSAGE_ROW_LIMIT)

    def test_appending_at_the_limit_evicts_the_oldest_row(self) -> None:
        """到上限就不再追加的话，界面看着像卡死了。"""
        self.pane.show_websocket(
            [frame(i) for i in range(MESSAGE_ROW_LIMIT)], WsClose()
        )
        self.pane.append_frame(frame(9999))

        table = self.pane.frame_table
        self.assertEqual(table.rowCount(), MESSAGE_ROW_LIMIT)
        self.assertEqual(text_at(table, 0, NO), "1")
        self.assertEqual(text_at(table, MESSAGE_ROW_LIMIT - 1, NO), "9999")
        self.assertEqual(self.pane.count, MESSAGE_ROW_LIMIT + 1)
        self.assertFalse(self.pane.frame_notice.isHidden())


class AppendTests(unittest.TestCase):
    """增量追加：整表重建会把选中和滚动位置一起清掉。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_a_new_frame_lands_at_the_bottom(self) -> None:
        self.pane.show_websocket([frame(0), frame(1)], WsClose())
        self.pane.append_frame(frame(2, content=b"newest"))

        self.assertEqual(self.pane.frame_table.rowCount(), 3)
        self.assertEqual(text_at(self.pane.frame_table, 2, NO), "2")
        self.assertEqual(text_at(self.pane.frame_table, 2, PREVIEW), "newest")
        self.assertEqual(self.pane.count, 3)

    def test_appending_keeps_the_current_selection(self) -> None:
        """正翻看前面某一帧的人不该被每秒几十帧的推流拽走。"""
        self.pane.show_websocket([frame(i) for i in range(3)], WsClose())
        self.pane.frame_table.selectRow(0)
        self.pane.append_frame(frame(3))

        self.assertEqual(self.pane.frame_table.currentRow(), 0)

    def test_the_first_frame_switches_a_pending_flow_into_frame_mode(self) -> None:
        """选中时握手还没完成，帧却已经到了 —— 这一帧本身就是「它是 WS」的证据。"""
        self.pane.set_data({"raw_state": {"websocket": None}})
        self.assertFalse(self.pane.applicable)

        self.pane.append_frame(frame(0, content=b"early"))

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_WS)
        self.assertEqual(self.pane.frame_table.rowCount(), 1)
        self.assertEqual(self.pane.count, 1)


class CloseInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_an_open_connection_says_so_instead_of_going_blank(self) -> None:
        self.pane.show_websocket([frame(0)], WsClose())
        self.assertEqual(self.pane.close_label.text(), "Connection open")

    def test_the_close_line_carries_who_why_and_when(self) -> None:
        self.pane.show_websocket(
            [frame(0)],
            WsClose(
                closed_by_client=True,
                close_code=1000,
                close_reason="done",
                timestamp_end=1700000009.0,
            ),
        )
        text = self.pane.close_label.text()

        self.assertIn("Closed by client", text)
        self.assertIn("1000", text)
        self.assertIn("done", text)

    def test_a_server_side_close_is_attributed_to_the_server(self) -> None:
        self.pane.show_websocket(
            [frame(0)], WsClose(closed_by_client=False, close_code=1006)
        )
        self.assertIn("Closed by server", self.pane.close_label.text())

    def test_a_close_arriving_later_updates_the_line_in_place(self) -> None:
        self.pane.show_websocket([frame(0)], WsClose())
        self.pane.set_close(WsClose(closed_by_client=True, close_code=1001))

        self.assertIn("1001", self.pane.close_label.text())
        # 关闭信息不该动帧表。
        self.assertEqual(self.pane.frame_table.rowCount(), 1)


class NativeFlowTests(unittest.TestCase):
    """跟原生 flow 对一遍：一帧都不许少，内容也不许错。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_a_native_websocket_flow_fills_the_table(self) -> None:
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        frames = ws_frames(flow.websocket)

        pane = MessagesPane()
        self.addCleanup(pane.deleteLater)
        pane.set_data(build_flow_detail(flow), frames, WsClose())

        self.assertEqual(pane.frame_table.rowCount(), len(frames))
        self.assertEqual(pane.count, len(frames))
        table = pane.frame_table
        self.assertEqual(
            [text_at(table, r, PREVIEW) for r in range(len(frames))],
            [frame_preview(f) for f in frames],
        )
        # `tflow` 的第一帧是 BINARY，所以那一格是十六进制而不是文本 —— 顺手钉住
        # 两种帧在同一张表里各按各的方式预览。
        self.assertEqual(text_at(table, 0, TYPE), "BINARY")
        self.assertEqual(text_at(table, 1, TYPE), "TEXT")
        self.assertEqual(text_at(table, 1, PREVIEW), "hello text")


class SseTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def cell(self, row: int, column: int) -> str:
        return text_at(self.pane.event_table, row, column)

    def test_every_event_gets_a_row(self) -> None:
        self.pane.show_sse("data: a\n\ndata: b\n\ndata: c\n\n")

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_SSE)
        self.assertEqual(self.pane.event_table.rowCount(), 3)
        self.assertEqual(self.pane.count, 3)

    def test_the_row_reads_the_six_things_worth_a_column(self) -> None:
        self.pane.show_sse('event: price\ndata: {"px": 1}\nid: 7\nretry: 3000\n\n')

        self.assertEqual(self.cell(0, E_NO), "0")
        self.assertEqual(self.cell(0, E_EVENT), "price")
        self.assertEqual(self.cell(0, E_ID), "7")
        self.assertEqual(self.cell(0, E_RETRY), "3000")
        self.assertEqual(self.cell(0, E_SIZE), "9b")
        self.assertEqual(self.cell(0, E_DATA), '{"px": 1}')

    def test_a_default_event_name_shows_the_spec_value(self) -> None:
        self.pane.show_sse("data: hi\n\n")
        self.assertEqual(self.cell(0, E_EVENT), "message")

    def test_an_absent_retry_leaves_the_cell_empty_not_zero(self) -> None:
        """`0` 会被读成「立刻重连」，那是另一回事。"""
        self.pane.show_sse("data: hi\n\n")
        self.assertEqual(self.cell(0, E_RETRY), "")

    def test_a_multi_line_data_event_previews_on_one_line(self) -> None:
        self.pane.show_sse("data: one\ndata: two\n\n")
        self.assertEqual(self.cell(0, E_DATA), "one two")

    def test_a_heartbeat_row_shows_the_comment_as_it_looked_on_the_wire(self) -> None:
        """看着就知道这一行不是一条消息。"""
        self.pane.show_sse(": keep-alive\n\n")

        self.assertEqual(self.cell(0, E_EVENT), "")
        self.assertEqual(self.cell(0, E_DATA), ": keep-alive")

    def test_selecting_an_event_shows_its_data(self) -> None:
        self.pane.show_sse('data: {"px": 1}\ndata: tail\n\n')
        self.pane.event_table.selectRow(0)

        self.assertEqual(self.pane.event_detail.text(), '{"px": 1}\ntail')

    def test_selecting_a_heartbeat_shows_the_raw_block(self) -> None:
        """心跳块的信息全在那一行注释里。"""
        self.pane.show_sse(": served by node-3\n\n")
        self.pane.event_table.selectRow(0)

        self.assertEqual(self.pane.event_detail.text(), ": served by node-3")

    def test_an_over_limit_stream_is_clipped_with_a_notice(self) -> None:
        total = MESSAGE_ROW_LIMIT + 5
        self.pane.show_sse("".join(f"data: {i}\n\n" for i in range(total)))

        self.assertEqual(self.pane.event_table.rowCount(), MESSAGE_ROW_LIMIT)
        self.assertEqual(self.pane.count, total)
        self.assertFalse(self.pane.event_notice.isHidden())
        self.assertIn(str(total), self.pane.event_notice.text())

    def test_a_streamed_body_says_why_the_table_is_missing(self) -> None:
        """空表看着像解析失败，而这条流量其实是被转发走了。"""
        self.pane.show_sse("")

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_PLACEHOLDER)
        self.assertIn("streamed", self.pane.placeholder.text())


class ModeSelectionTests(unittest.TestCase):
    """`set_data` 的三态分派。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_a_websocket_flow_lands_on_the_frame_table(self) -> None:
        self.pane.set_data(
            {"raw_state": {"websocket": {}}}, [frame(0), frame(1)], WsClose()
        )

        self.assertEqual(self.pane.pages.currentIndex(), PAGE_WS)
        self.assertEqual(self.pane.frame_table.rowCount(), 2)

    def test_an_event_stream_lands_on_the_event_table(self) -> None:
        self.pane.set_data(sse_detail("data: hi\n\n"))

        self.assertEqual(self.pane.pages.currentIndex(), PAGE_SSE)
        self.assertEqual(self.pane.event_table.rowCount(), 1)

    def test_a_parameterised_content_type_still_counts(self) -> None:
        self.pane.set_data(
            sse_detail("data: hi\n\n", "text/event-stream; charset=utf-8")
        )
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_SSE)

    def test_a_plain_flow_is_not_applicable(self) -> None:
        """普通请求点进去只会看到一张空表，所以整页该藏。"""
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_PLACEHOLDER)
        self.assertEqual(self.pane.count, 0)

    def test_a_websocket_flow_with_no_frames_yet_is_still_applicable(self) -> None:
        """握手成功就该有这一页 —— 帧还没来是另一回事。"""
        self.pane.set_data({"raw_state": {"websocket": {"messages": []}}}, [])

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_WS)

    def test_going_from_websocket_to_plain_clears_the_frames(self) -> None:
        self.pane.set_data({"raw_state": {"websocket": {}}}, [frame(0)], WsClose())
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertEqual(self.pane.frame_table.rowCount(), 0)
        self.assertEqual(self.pane.count, 0)

    def test_going_from_sse_to_websocket_clears_the_events(self) -> None:
        self.pane.set_data(sse_detail("data: a\n\ndata: b\n\n"))
        self.pane.set_data({"raw_state": {"websocket": {}}}, [frame(0)], WsClose())

        self.assertEqual(self.pane.event_table.rowCount(), 0)
        self.assertEqual(self.pane.frame_table.rowCount(), 1)

    def test_a_four_key_dict_does_not_blow_up(self) -> None:
        """表格双击传过来的可能只有几个键。"""
        self.pane.set_data({"Method": "GET", "URL": "https://x/y"})
        self.assertFalse(self.pane.applicable)


if __name__ == "__main__":
    unittest.main()
