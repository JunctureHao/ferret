"""消息页：聊天气泡流、上限逐出、过滤 / 排序 / 清空。

改造前这两种流量在详情面板里根本看不见，所以这里钉的大多是「有没有真的显示出来」，
而不是像素。现在 UI 是气泡流（`chat.py::ChatStream`），断言跟着换成气泡语义。重点：

* **气泡内容**。方向文字、大小、`dropped` / `injected` 字眼必须落在气泡上 ——
  被丢掉的帧对端根本没收到，这是结论级信息。二进制帧展开态是 hex dump。
* **上限与计数语义分开**。截断只发生在这一层（`ws_frames` 恒返回全部帧），超
  `MESSAGE_ROW_LIMIT` 静默从最旧一端逐出，而 `count` 恒指内核总数。
* **模式判定**。WS / SSE / 都不是，三态各自落在正确的堆叠页；SSE 开了 `stream`
  时 body 是空的，那时要说「未缓冲」而不是摆一条空流（后者看着像解析失败）。
* **顶栏三件事**。过滤隐藏不销毁、正逆序切换按键恢复选中、清空只清显示不动总数，
  清完后续帧照常追加；逆序时新气泡插在顶部。

`isVisible()` 在这里一律不可信：面板没 `show()` 过，祖先链上就是不可见的。判「藏没藏」
统一用 `isHidden()`（只看自己那一位显式隐藏标志）。翻译器**故意不装**，理由同
`test_fields.py` —— 断言比的是源码里的英文原文。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.chat import MAX_COLLAPSED_HEIGHT, Bubble, SystemNote
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
    parse_sse,
    ws_frames,
)

# 堆叠页序号（和 `messages.py` 里的私有常量对齐；测试不该 import 私有名）。
PAGE_PLACEHOLDER, PAGE_STREAM = range(2)


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


def sse_detail(body: str, content_type: str = "text/event-stream") -> dict:
    return {
        "id": "flow-1",
        "raw_state": {"websocket": None},
        "Response Content-Type": content_type,
        "Response Body Text": body,
    }


def bubbles_of(pane: MessagesPane) -> list[Bubble]:
    return pane.stream.bubbles()


def notes_of(pane: MessagesPane) -> list[SystemNote]:
    """流里的系统条（关闭信息、SSE 心跳）。布局里只有这两类控件。"""
    layout = pane.stream._host.layout()
    assert layout is not None
    notes = []
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget() if item is not None else None
        if isinstance(widget, SystemNote):
            notes.append(widget)
    return notes


class PureHelperTests(unittest.TestCase):
    """不碰 Qt 的那一半。"""

    def test_websocket_detection_reads_the_native_subtree(self) -> None:
        """`websocket` 子树在就是 WS，哪怕还是空的；`None` 就是普通流量。"""
        self.assertTrue(is_websocket({"raw_state": {"websocket": {"messages": []}}}))
        self.assertFalse(is_websocket({"raw_state": {"websocket": None}}))

    def test_websocket_detection_survives_a_missing_state(self) -> None:
        """详情字典可能只有几个键（双击表格那一路），不许抛。"""
        self.assertFalse(is_websocket({}))
        self.assertFalse(is_websocket({"raw_state": "broken"}))

    def test_a_real_websocket_flow_is_detected(self) -> None:
        data = build_flow_detail(tflow.twebsocketflow())
        self.assertTrue(is_websocket(data))

    def test_a_real_http_flow_is_not(self) -> None:
        data = build_flow_detail(tflow.tflow(resp=True))
        self.assertFalse(is_websocket(data))

    def test_json_sniffing_only_looks_at_the_first_character(self) -> None:
        self.assertTrue(looks_like_json('  {"a": 1}'))
        self.assertTrue(looks_like_json("[1, 2]"))
        self.assertFalse(looks_like_json("plain text"))
        self.assertFalse(looks_like_json(""))

    def test_frame_time_keeps_milliseconds_and_drops_the_date(self) -> None:
        stamp = frame_time(1700000000.5)
        self.assertRegex(stamp, r"^\d\d:\d\d:\d\d\.500$")

    def test_frame_time_says_something_when_there_is_no_timestamp(self) -> None:
        self.assertEqual(frame_time(None), "-")
        self.assertEqual(frame_time(0), "-")

    def test_preview_collapses_whitespace_into_one_line(self) -> None:
        self.assertEqual(preview_text('{\n  "a":\t1\n}'), '{ "a": 1 }')

    def test_a_long_preview_is_clipped_with_an_ellipsis(self) -> None:
        text = preview_text("x" * 500)
        self.assertTrue(text.endswith("…"))
        self.assertEqual(len(text), 161)

    def test_a_binary_frame_previews_as_hex(self) -> None:
        preview = frame_preview(frame(0, opcode=2, content=b"\x01\x02"))
        self.assertEqual(preview, "01 02")

    def test_a_text_frame_previews_as_text(self) -> None:
        self.assertEqual(frame_preview(frame(0, content=b"hi")), "hi")

    def test_a_malformed_text_frame_still_previews(self) -> None:
        """坏字节不许让气泡塌掉 —— 抓到什么显示什么。"""
        self.assertTrue(frame_preview(frame(0, content=b"\xff\xfe")))

    def test_hex_dump_lays_out_sixteen_bytes_per_line(self) -> None:
        dump = hex_dump(bytes(range(32)))
        lines = dump.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("00000000"))
        self.assertTrue(lines[1].startswith("00000010"))
        self.assertIn("|", lines[0])

    def test_hex_dump_stops_at_its_limit(self) -> None:
        self.assertEqual(len(hex_dump(bytes(4096), limit=32).splitlines()), 2)


class WebsocketBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_a_fresh_pane_is_not_applicable(self) -> None:
        """详情面板据此决定整页露不露面。"""
        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.count, 0)

    def test_every_frame_gets_a_bubble(self) -> None:
        frames = [frame(i, content=str(i).encode()) for i in range(4)]
        self.pane.show_websocket(frames, WsClose())

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 4)
        self.assertEqual(self.pane.count, 4)

    def test_a_bubble_shows_only_its_content(self) -> None:
        """气泡只摆消息本体：方向靠左右对齐表达，不写进文字。"""
        self.pane.show_websocket(
            [frame(7, content=b'{"px": 1}', timestamp=1700000000.5)], WsClose()
        )
        bubble = bubbles_of(self.pane)[0]

        self.assertEqual(bubble.content_label.text(), '{"px": 1}')
        # 无方向头、无大小尾 —— 卡片里只有内容。
        self.assertFalse(hasattr(bubble, "header_label"))
        self.assertFalse(hasattr(bubble, "footer_label"))
        self.assertNotIn("Client", bubble.content_label.text())

    def test_upstream_aligns_right_and_downstream_left(self) -> None:
        self.pane.show_websocket(
            [frame(0, from_client=True), frame(1, from_client=False)], WsClose()
        )
        up, down = bubbles_of(self.pane)

        self.assertTrue(up.align_right)
        self.assertFalse(down.align_right)

    def test_direction_words_stay_searchable(self) -> None:
        """方向词从界面撤下后仍进搜索串 —— 输 "client →" 只看上行。"""
        self.pane.show_websocket(
            [frame(0, from_client=True), frame(1, from_client=False)], WsClose()
        )
        self.pane.filter_input.setText("client →")

        up, down = bubbles_of(self.pane)
        self.assertFalse(up.isHidden())
        self.assertTrue(down.isHidden())

    def test_a_text_bubble_shows_its_whole_content(self) -> None:
        body = '{"a": ' + "1" * 500 + "}"
        self.pane.show_websocket([frame(0, content=body.encode())], WsClose())

        self.assertEqual(bubbles_of(self.pane)[0].content_label.text(), body)

    def test_a_binary_bubble_expands_into_a_hex_dump(self) -> None:
        self.pane.show_websocket(
            [frame(0, opcode=2, content=bytes(range(32)))], WsClose()
        )
        bubble = bubbles_of(self.pane)[0]
        self.assertNotIn("00000010", bubble.content_label.text())

        bubble.toggle_expanded()
        bubble.activated.emit(bubble.key)

        text = bubble.content_label.text()
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn("00000010", text)

    def test_a_huge_binary_frame_says_how_much_was_clipped(self) -> None:
        size = HEX_DUMP_LIMIT + 1000
        self.pane.show_websocket([frame(0, opcode=2, content=bytes(size))], WsClose())
        bubble = bubbles_of(self.pane)[0]
        bubble.toggle_expanded()
        bubble.activated.emit(bubble.key)
        tail = bubble.content_label.text().splitlines()[-1]

        self.assertIn(str(HEX_DUMP_LIMIT), tail)
        self.assertIn(str(size), tail)

    def test_clicking_a_bubble_expands_and_selects_it(self) -> None:
        self.pane.show_websocket([frame(0), frame(1)], WsClose())
        bubble = bubbles_of(self.pane)[1]

        bubble.activated.emit(bubble.key)
        self.pane.stream._on_bubble_clicked(bubble.key)

        self.assertEqual(self.pane.stream.selected_key, 1)

    def test_switching_flows_clears_the_previous_frames(self) -> None:
        self.pane.show_websocket([frame(i) for i in range(5)], WsClose())
        self.pane.show_websocket([frame(0)], WsClose())

        self.assertEqual(len(bubbles_of(self.pane)), 1)
        self.assertEqual(self.pane.count, 1)


class ViewAndThemeTests(unittest.TestCase):
    """视口与主题：白底视口会吃掉深色主题的白字，这两条钉住回归。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_the_stream_viewport_is_transparent(self) -> None:
        """qfw `enableTransparentBackground`：视口透出宿主主题底色，不自己画白底。"""
        self.pane.show_websocket([frame(0)], WsClose())
        self.pane.resize(600, 400)
        self.pane.show()
        self.app.processEvents()

        # 原生 QScrollArea 的视口画调色板白底；透明化之后样式表里必须是 transparent。
        self.assertIn("transparent", self.pane.stream.styleSheet())
        host = self.pane.stream.widget()
        assert host is not None
        self.assertIn("transparent", host.styleSheet())

    def test_notes_repaint_on_a_theme_switch(self) -> None:
        """主题热切换：系统条要重算样式（气泡走 CardWidget 的内置跟随）。"""
        from qfluentwidgets import Theme, setTheme

        self.pane.show_websocket(
            [frame(0)],
            WsClose(closed_by_client=True, close_code=1000),
        )
        note = notes_of(self.pane)[0]
        start = note.styleSheet()
        self.addCleanup(setTheme, Theme.LIGHT, save=False)

        setTheme(Theme.DARK, save=False)
        dark = note.styleSheet()
        setTheme(Theme.LIGHT, save=False)

        self.assertNotEqual(dark, start)
        self.assertEqual(note.styleSheet(), start)


class BubbleGeometryTests(unittest.TestCase):
    """长文本气泡的折行与展开：布局数学不把 `heightForWidth` 喂进布局链的话，
    气泡永远只有一行高，长 JSON 被剪成一条缝。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)
        self.pane.resize(600, 400)
        self.pane.show()
        self.app.processEvents()

    def __long_json(self) -> WsFrame:
        body = (
            '{"ok":true,"data":{"list":[{"id":1,"name":"alpha","price":10.5},'
            '{"id":2,"name":"beta","price":22.0},{"id":3,"name":"gamma","price":33.3}]}}'
        )
        return frame(0, content=body.encode())

    def test_a_long_text_bubble_is_taller_than_one_line(self) -> None:
        self.pane.show_websocket([self.__long_json()], WsClose())
        self.app.processEvents()

        bubble = bubbles_of(self.pane)[0]
        line = bubble.content_label.fontMetrics().height()
        self.assertGreater(bubble.height(), line * 3)

    def test_expanding_a_text_bubble_grows_it(self) -> None:
        """点击展开必须肉眼可见地变高 —— 不然「原地展开」是句空话。"""
        # 内容要高过收起态的 120px 上限，展开才谈得上「变高」。
        body = '{"list":[' + ",".join(f'{{"id":{i}}}' for i in range(40)) + "]}"
        self.pane.show_websocket([frame(0, content=body.encode())], WsClose())
        bubble = bubbles_of(self.pane)[0]
        self.app.processEvents()
        collapsed_height = bubble.height()

        bubble.toggle_expanded()
        bubble.activated.emit(bubble.key)
        self.app.processEvents()

        self.assertGreater(bubble.height(), collapsed_height)

    def test_the_collapsed_bubble_is_capped(self) -> None:
        """限高截断仍然生效：一屏推流不许把整页撑成不可读的长墙。"""
        self.pane.show_websocket([self.__long_json()], WsClose())
        bubble = bubbles_of(self.pane)[0]
        self.app.processEvents()

        self.assertLessEqual(bubble.content_label.height(), MAX_COLLAPSED_HEIGHT)


class SortButtonTests(unittest.TestCase):
    """排序按钮：图标随状态切换，只换 tooltip 看起来像没反应。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from qfluentwidgets import FluentIcon

        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)
        self.pane.show_websocket([frame(i) for i in range(3)], WsClose())
        self._down = FluentIcon.DOWN
        self._up = FluentIcon.UP

    def test_the_icon_follows_the_order(self) -> None:
        self.assertEqual(self.pane.sort_btn._icon, self._down)
        self.pane.sort_btn.setChecked(True)
        self.assertEqual(self.pane.sort_btn._icon, self._up)
        self.pane.sort_btn.setChecked(False)
        self.assertEqual(self.pane.sort_btn._icon, self._down)

    def test_the_tooltip_follows_the_order(self) -> None:
        self.pane.sort_btn.setChecked(True)
        self.assertEqual(self.pane.sort_btn.toolTip(), "Oldest first")
        self.pane.sort_btn.setChecked(False)
        self.assertEqual(self.pane.sort_btn.toolTip(), "Newest first")


class FrameLimitTests(unittest.TestCase):
    """截断只发生在这一层；显示条数与 `count` 语义分开。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_the_row_limit_matches_the_core_display_cap(self) -> None:
        """两处各写一个数迟早只改一边。"""
        self.assertEqual(MESSAGE_ROW_LIMIT, WS_FRAME_LIMIT)

    def test_an_over_limit_stream_keeps_the_newest_bubbles(self) -> None:
        """最新的帧才是排查时在看的东西。"""
        extra = 7
        frames = [
            frame(i, content=str(i).encode()) for i in range(MESSAGE_ROW_LIMIT + extra)
        ]
        self.pane.show_websocket(frames, WsClose())

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), MESSAGE_ROW_LIMIT)
        self.assertEqual(bubbles[0].key, extra)
        self.assertEqual(bubbles[-1].key, MESSAGE_ROW_LIMIT + extra - 1)

    def test_the_count_reports_the_total_not_the_visible_bubbles(self) -> None:
        """导航项上那枚徽标读它 —— 显示 2000 而实际 2007 是假话。"""
        total = MESSAGE_ROW_LIMIT + 7
        self.pane.show_websocket([frame(i) for i in range(total)], WsClose())

        self.assertEqual(self.pane.count, total)
        self.assertEqual(len(bubbles_of(self.pane)), MESSAGE_ROW_LIMIT)

    def test_appending_at_the_limit_evicts_the_oldest_bubble(self) -> None:
        """到上限就不再追加的话，界面看着像卡死了。"""
        self.pane.show_websocket(
            [frame(i) for i in range(MESSAGE_ROW_LIMIT)], WsClose()
        )
        self.pane.append_frame(frame(9999))

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), MESSAGE_ROW_LIMIT)
        self.assertEqual(bubbles[0].key, 1)
        self.assertEqual(bubbles[-1].key, 9999)
        self.assertEqual(self.pane.count, MESSAGE_ROW_LIMIT + 1)


class AppendTests(unittest.TestCase):
    """增量追加：整流重建会把选中和滚动位置一起清掉。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_a_new_frame_lands_at_the_bottom(self) -> None:
        self.pane.show_websocket([frame(0), frame(1)], WsClose())
        self.pane.append_frame(frame(2, content=b"newest"))

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), 3)
        self.assertEqual(bubbles[-1].key, 2)
        self.assertEqual(bubbles[-1].content_label.text(), "newest")
        self.assertEqual(self.pane.count, 3)

    def test_appending_keeps_the_current_selection(self) -> None:
        """正翻看前面某一帧的人不该被每秒几十帧的推流拽走。"""
        self.pane.show_websocket([frame(i) for i in range(3)], WsClose())
        self.pane.stream._select(bubbles_of(self.pane)[0])
        self.pane.append_frame(frame(3))

        self.assertEqual(self.pane.stream.selected_key, 0)

    def test_the_first_frame_switches_a_pending_flow_into_frame_mode(self) -> None:
        """选中时握手还没完成，帧却已经到了 —— 这一帧本身就是「它是 WS」的证据。"""
        self.pane.set_data({"raw_state": {"websocket": None}})
        self.assertFalse(self.pane.applicable)

        self.pane.append_frame(frame(0, content=b"early"))

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 1)
        self.assertEqual(self.pane.count, 1)


class CloseInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_an_open_connection_has_no_system_note(self) -> None:
        """开着时不摆任何条 —— 「保持中」不是一条消息。"""
        self.pane.show_websocket([frame(0)], WsClose())
        self.assertEqual(notes_of(self.pane), [])

    def test_the_close_note_carries_who_why_and_when(self) -> None:
        self.pane.show_websocket(
            [frame(0)],
            WsClose(
                closed_by_client=True,
                close_code=1000,
                close_reason="done",
                timestamp_end=1700000009.0,
            ),
        )
        notes = notes_of(self.pane)

        self.assertEqual(len(notes), 1)
        text = notes[0].label.text()
        self.assertIn("Closed by client", text)
        self.assertIn("1000", text)
        self.assertIn("done", text)

    def test_a_server_side_close_is_attributed_to_the_server(self) -> None:
        self.pane.show_websocket(
            [frame(0)], WsClose(closed_by_client=False, close_code=1006)
        )
        notes = notes_of(self.pane)
        self.assertIn("Closed by server", notes[0].label.text())

    def test_a_close_arriving_later_replaces_the_note_in_place(self) -> None:
        """`websocket_end` 可能晚于 `set_data` 到达，重复调用原地更新。"""
        self.pane.show_websocket([frame(0)], WsClose())
        self.pane.set_close(WsClose(closed_by_client=True, close_code=1001))

        notes = notes_of(self.pane)
        self.assertEqual(len(notes), 1)
        self.assertIn("1001", notes[0].label.text())
        # 关闭信息不该动气泡。
        self.assertEqual(len(bubbles_of(self.pane)), 1)


class NativeFlowTests(unittest.TestCase):
    """跟原生 flow 对一遍：一条都不许少，内容也不许错。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_a_native_websocket_flow_fills_the_stream(self) -> None:
        flow = tflow.twebsocketflow()
        assert flow.websocket is not None
        frames = ws_frames(flow.websocket)

        pane = MessagesPane()
        self.addCleanup(pane.deleteLater)
        pane.set_data(build_flow_detail(flow), frames, WsClose())

        bubbles = bubbles_of(pane)
        self.assertEqual(len(bubbles), len(frames))
        self.assertEqual(pane.count, len(frames))
        self.assertEqual(
            [b.content_label.text() for b in bubbles],
            [frame_preview(f) for f in frames],
        )
        # `tflow` 的第一帧是 BINARY —— 两种帧在同一条流里各按各的方式预览。
        self.assertNotIn("hello text", bubbles[0].content_label.text())
        self.assertEqual(bubbles[1].content_label.text(), "hello text")


class SseBubbleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def show_sse(self, body: str) -> None:
        """老接口退场后的便捷包装：解析一整份 body 再走整取。"""
        self.pane.show_sse_events(parse_sse(body))

    def test_every_event_gets_a_bubble(self) -> None:
        self.show_sse("data: a\n\ndata: b\n\ndata: c\n\n")

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 3)
        self.assertEqual(self.pane.count, 3)

    def test_a_bubble_shows_only_the_data(self) -> None:
        """事件名 / id / retry 只进搜索串 —— 气泡里只有数据本体。"""
        self.show_sse('event: price\ndata: {"px": 1}\nid: 7\nretry: 3000\n\n')
        bubble = bubbles_of(self.pane)[0]

        self.assertEqual(bubble.content_label.text(), '{"px": 1}')
        self.assertFalse(hasattr(bubble, "header_label"))
        self.assertFalse(hasattr(bubble, "footer_label"))

    def test_event_metadata_stays_searchable(self) -> None:
        """输事件名能过滤到对应事件 —— 元信息只是不占界面。"""
        self.show_sse('event: price\ndata: {"px": 1}\nid: 7\n\n')
        self.pane.filter_input.setText("price")

        self.assertFalse(bubbles_of(self.pane)[0].isHidden())

    def test_a_multi_line_data_event_keeps_its_lines(self) -> None:
        """气泡里摆全文（限高截断是控件的事），折成一行反而丢信息。"""
        self.show_sse("data: one\ndata: two\n\n")
        self.assertEqual(bubbles_of(self.pane)[0].content_label.text(), "one\ntwo")

    def test_a_heartbeat_becomes_a_centered_system_note(self) -> None:
        """心跳不是任何一方说的话，不占一枚气泡。"""
        self.show_sse(": keep-alive\n\n")

        self.assertEqual(bubbles_of(self.pane), [])
        notes = notes_of(self.pane)
        self.assertEqual(len(notes), 1)
        self.assertIn(": keep-alive", notes[0].label.text())
        self.assertEqual(self.pane.count, 1)

    def test_an_over_limit_stream_keeps_the_newest_events(self) -> None:
        total = MESSAGE_ROW_LIMIT + 5
        self.show_sse("".join(f"data: {i}\n\n" for i in range(total)))

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), MESSAGE_ROW_LIMIT)
        self.assertEqual(self.pane.count, total)
        self.assertEqual(bubbles[0].key, 5)

    def test_an_empty_event_list_shows_an_empty_stream(self) -> None:
        """流式中、事件还没到：摆一条空流，事件到了经 `append_event` 长出来。

        「响应体以流式转发，未缓冲」的占位文案已退役 —— tee 之后流式反而有数据。
        """
        self.pane.show_sse_events([])

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(bubbles_of(self.pane), [])
        self.assertEqual(self.pane.count, 0)

    def test_append_event_grows_the_stream_incrementally(self) -> None:
        """增量语义对齐 `append_frame`：计数 +1、不重建、超限从最旧端逐出。"""
        self.pane.show_sse_events(parse_sse("data: a\n\n"))
        self.pane.append_event(parse_sse("data: b\n\n")[0])

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), 2)
        self.assertEqual(self.pane.count, 2)
        self.assertEqual(bubbles[-1].content_label.text(), "b")

    def test_append_event_from_a_non_sse_mode_rebuilds(self) -> None:
        """选中时还没认出是事件流（`set_data` 走了别的分支），事件本身就是证据。"""
        self.pane.append_event(parse_sse("data: x\n\n")[0])

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 1)
        self.assertEqual(self.pane.count, 1)

    def test_set_data_prefers_the_archive_over_the_body(self) -> None:
        """流式中的流量走整取：事件从 addon 存档来，body 是旧的也不碍事。"""
        archived = parse_sse("data: from-archive\n\n")
        self.pane.set_data(sse_detail("data: stale-body\n\n"), events=archived)

        self.assertEqual(
            [b.content_label.text() for b in bubbles_of(self.pane)], ["from-archive"]
        )
        self.assertEqual(self.pane.count, 1)

    def test_set_data_falls_back_to_the_body_without_an_archive(self) -> None:
        """历史流量（从 `.flow` 文件读回来的）没有 addon 存档，兑底解 body。"""
        self.pane.set_data(sse_detail("data: from-body\n\n"))

        self.assertEqual(
            [b.content_label.text() for b in bubbles_of(self.pane)], ["from-body"]
        )

    def test_an_empty_archive_still_falls_back_to_the_body(self) -> None:
        """空表 = 还没攒到事件（与没有这个参数是同一条兑底路）。

        流式 body 本身是空的，解出来自然是空流 —— 事件到了经 `append_event`
        长出来，这条已经在上面钉过。
        """
        self.pane.set_data(sse_detail("data: from-body\n\n"), events=[])

        self.assertEqual(
            [b.content_label.text() for b in bubbles_of(self.pane)], ["from-body"]
        )


class FilterTests(unittest.TestCase):
    """过滤框：大小写不敏感子串，隐藏不销毁，清空全显。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)
        self.pane.show_websocket(
            [
                frame(0, from_client=True, content=b'{"action":"login"}'),
                frame(1, from_client=False, content=b'{"ok":true}'),
            ],
            WsClose(),
        )

    def test_a_substring_hides_only_the_misses(self) -> None:
        self.pane.filter_input.setText("LOGIN")

        up, down = bubbles_of(self.pane)
        self.assertFalse(up.isHidden())
        self.assertTrue(down.isHidden())
        # 隐藏不销毁。
        self.assertEqual(len(bubbles_of(self.pane)), 2)

    def test_the_direction_words_are_searchable(self) -> None:
        """输 "client" 只看上行帧 —— 排查订阅流时最快的切法。"""
        self.pane.filter_input.setText("client")

        up, down = bubbles_of(self.pane)
        # 两个方向文字里都含 "client"，但上行是 "Client → Server"。
        self.assertFalse(up.isHidden())
        self.assertFalse(down.isHidden())
        self.pane.filter_input.setText("client →")
        self.assertFalse(up.isHidden())
        self.assertTrue(down.isHidden())

    def test_a_binary_frame_matches_its_hex(self) -> None:
        self.pane.show_websocket([frame(0, opcode=2, content=b"\xde\xad")], WsClose())
        self.pane.filter_input.setText("de ad")

        self.assertFalse(bubbles_of(self.pane)[0].isHidden())

    def test_clearing_the_filter_shows_everything_again(self) -> None:
        self.pane.filter_input.setText("login")
        self.pane.filter_input.clear()

        self.assertTrue(all(not b.isHidden() for b in bubbles_of(self.pane)))

    def test_sse_events_match_data_and_event_name(self) -> None:
        self.pane.show_sse_events(
            parse_sse('event: price\ndata: {"px": 1}\n\ndata: keep\n\n')
        )
        self.pane.filter_input.setText("price")

        first, second = bubbles_of(self.pane)
        self.assertFalse(first.isHidden())
        self.assertTrue(second.isHidden())

    def test_switching_flows_clears_the_filter(self) -> None:
        self.pane.filter_input.setText("login")
        self.pane.show_websocket([frame(0)], WsClose())

        self.assertEqual(self.pane.filter_input.text(), "")
        self.assertFalse(bubbles_of(self.pane)[0].isHidden())


class OrderTests(unittest.TestCase):
    """正序 = 最早在上（默认），逆序 = 最新在上。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)
        self.pane.show_websocket([frame(i) for i in range(4)], WsClose())

    def keys(self) -> list[object]:
        return [b.key for b in bubbles_of(self.pane)]

    def test_the_default_order_is_oldest_first(self) -> None:
        self.assertEqual(self.keys(), [0, 1, 2, 3])
        self.assertFalse(self.pane.stream.descending)

    def test_toggling_rebuilds_in_reverse(self) -> None:
        self.pane.sort_btn.setChecked(True)

        self.assertEqual(self.keys(), [3, 2, 1, 0])
        self.assertTrue(self.pane.stream.descending)

    def test_the_selection_survives_an_order_switch(self) -> None:
        self.pane.stream._select(bubbles_of(self.pane)[1])
        self.pane.sort_btn.setChecked(True)

        self.assertEqual(self.pane.stream.selected_key, 1)
        selected = [b for b in bubbles_of(self.pane) if b.key == 1]
        self.assertEqual(len(selected), 1)

    def test_appending_in_reverse_lands_on_top(self) -> None:
        self.pane.sort_btn.setChecked(True)
        self.pane.append_frame(frame(4))

        self.assertEqual(self.keys(), [4, 3, 2, 1, 0])

    def test_a_system_note_follows_the_order(self) -> None:
        """逆序时关闭条也在「最新」那一端（顶部）。"""
        self.pane.sort_btn.setChecked(True)
        self.pane.set_close(WsClose(closed_by_client=True, close_code=1000))

        layout = self.pane.stream._host.layout()
        assert layout is not None
        item = layout.itemAt(0)
        first = item.widget() if item is not None else None
        self.assertIsInstance(first, SystemNote)


class ClearTests(unittest.TestCase):
    """清空只清显示：内核帧数据不动，后续帧照常追加，`count` 不受影响。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_clearing_empties_the_stream_but_keeps_the_total(self) -> None:
        self.pane.show_websocket([frame(i) for i in range(5)], WsClose())
        self.pane.clear_btn.click()

        self.assertEqual(bubbles_of(self.pane), [])
        self.assertEqual(self.pane.count, 5)
        self.assertTrue(self.pane.applicable)

    def test_new_frames_still_arrive_after_a_clear(self) -> None:
        self.pane.show_websocket([frame(0), frame(1)], WsClose())
        self.pane.clear_btn.click()
        self.pane.append_frame(frame(2))

        bubbles = bubbles_of(self.pane)
        self.assertEqual(len(bubbles), 1)
        self.assertEqual(bubbles[0].key, 2)
        self.assertEqual(self.pane.count, 3)

    def test_clearing_drops_the_selection(self) -> None:
        self.pane.show_websocket([frame(0)], WsClose())
        self.pane.stream._select(bubbles_of(self.pane)[0])
        self.pane.clear_btn.click()

        self.assertIsNone(self.pane.stream.selected_key)


class ModeSelectionTests(unittest.TestCase):
    """`set_data` 的三态分派。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)

    def test_a_websocket_flow_lands_on_the_stream(self) -> None:
        self.pane.set_data(
            {"raw_state": {"websocket": {}}}, [frame(0), frame(1)], WsClose()
        )

        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 2)

    def test_an_event_stream_lands_on_the_stream(self) -> None:
        self.pane.set_data(sse_detail("data: hi\n\n"))

        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)
        self.assertEqual(len(bubbles_of(self.pane)), 1)

    def test_a_parameterised_content_type_still_counts(self) -> None:
        self.pane.set_data(
            sse_detail("data: hi\n\n", "text/event-stream; charset=utf-8")
        )
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)

    def test_a_plain_flow_is_not_applicable(self) -> None:
        """普通请求点进去只会看到一条空流，所以整页该藏。"""
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_PLACEHOLDER)
        self.assertEqual(self.pane.count, 0)

    def test_a_websocket_flow_with_no_frames_yet_is_still_applicable(self) -> None:
        """握手成功就该有这一页 —— 帧还没来是另一回事。"""
        self.pane.set_data({"raw_state": {"websocket": {"messages": []}}}, [])

        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_STREAM)

    def test_going_from_websocket_to_plain_clears_the_frames(self) -> None:
        self.pane.set_data({"raw_state": {"websocket": {}}}, [frame(0)], WsClose())
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))

        self.assertEqual(bubbles_of(self.pane), [])
        self.assertEqual(self.pane.count, 0)

    def test_going_from_sse_to_websocket_clears_the_events(self) -> None:
        self.pane.set_data(sse_detail("data: a\n\ndata: b\n\n"))
        self.pane.set_data({"raw_state": {"websocket": {}}}, [frame(0)], WsClose())

        self.assertEqual(len(bubbles_of(self.pane)), 1)
        self.assertEqual(self.pane.count, 1)

    def test_a_four_key_dict_does_not_blow_up(self) -> None:
        """表格双击传过来的可能只有几个键。"""
        self.pane.set_data({"Method": "GET", "URL": "https://x/y"})
        self.assertFalse(self.pane.applicable)
