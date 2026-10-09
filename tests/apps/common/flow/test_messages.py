"""消息列表与详情导航：方向、返回、实时追加、过滤、排序和协议原文。

测试操作真实 Qt 选择模型，避免只验证某个 delegate 的内部实现。内容与关闭原因
始终按纯文本显示，显示容量、筛选结果数和内核总数保持各自的语义。
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication
from qfluentwidgets import FluentIcon, ListView

from ferret.apps.common.edit.syntax import Language
from ferret.apps.common.flow.fields import Field, FieldCard, Section
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
from tests.core.mitm._qt import wait_until

PAGE_PLACEHOLDER, PAGE_LIST = range(2)


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


class PureHelperTests(unittest.TestCase):
    def test_websocket_detection_reads_the_native_subtree(self) -> None:
        self.assertTrue(is_websocket({"raw_state": {"websocket": {"messages": []}}}))
        self.assertFalse(is_websocket({"raw_state": {"websocket": None}}))

    def test_websocket_detection_accepts_the_lightweight_summary(self) -> None:
        self.assertTrue(is_websocket({"is_websocket": True}))
        self.assertFalse(is_websocket({"is_websocket": False}))

    def test_websocket_detection_survives_a_missing_state(self) -> None:
        self.assertFalse(is_websocket({}))
        self.assertFalse(is_websocket({"raw_state": "broken"}))

    def test_native_flows_have_the_correct_protocol(self) -> None:
        self.assertTrue(is_websocket(build_flow_detail(tflow.twebsocketflow())))
        self.assertFalse(is_websocket(build_flow_detail(tflow.tflow(resp=True))))

    def test_json_sniffing_only_looks_at_the_first_character(self) -> None:
        self.assertTrue(looks_like_json('  {"a": 1}'))
        self.assertTrue(looks_like_json("[1, 2]"))
        self.assertFalse(looks_like_json("plain text"))
        self.assertFalse(looks_like_json(""))

    def test_frame_time_keeps_milliseconds_and_drops_the_date(self) -> None:
        self.assertRegex(frame_time(1700000000.5), r"^\d\d:\d\d:\d\d\.500$")

    def test_frame_time_says_something_when_there_is_no_timestamp(self) -> None:
        self.assertEqual(frame_time(None), "-")
        self.assertEqual(frame_time(0), "-")

    def test_preview_collapses_whitespace_and_clips_long_lines(self) -> None:
        self.assertEqual(preview_text('{\n  "a":\t1\n}'), '{ "a": 1 }')
        text = preview_text("x" * 500)
        self.assertTrue(text.endswith("…"))
        self.assertEqual(len(text), 161)

    def test_frame_previews_handle_text_binary_and_bad_utf8(self) -> None:
        self.assertEqual(frame_preview(frame(opcode=2, content=b"\x01\x02")), "01 02")
        self.assertEqual(frame_preview(frame(content=b"hi")), "hi")
        self.assertTrue(frame_preview(frame(content=b"\xff\xfe")))

    def test_hex_dump_lays_out_sixteen_bytes_per_line_and_respects_limit(self) -> None:
        lines = hex_dump(bytes(range(48)), limit=32).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("00000000"))
        self.assertTrue(lines[1].startswith("00000010"))
        self.assertIn("|", lines[0])


class PaneTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = MessagesPane()
        self.addCleanup(self.pane.deleteLater)
        self.addCleanup(self.pane.close)

    def select(self, row: int = 0) -> None:
        model = self.pane.message_list.model()
        assert model is not None
        self.pane.message_list.setCurrentIndex(model.index(row, 0))

    def open_row(self, row: int = 0) -> None:
        self.select(row)
        view = self.pane.message_list
        view.activated.emit(view.currentIndex())

    def selected_key(self) -> object:
        selected = self.pane.message_list.selected_row()
        return selected.key if selected is not None else None

    def keys(self) -> list[object]:
        return [row.key for row in self.pane.message_list.rows()]

    def assert_no_detail(self) -> None:
        self.assertIsNone(self.pane.message_list.selected_row())
        self.assertEqual(self.pane.detail_edit.text(), "")
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.stream_page)


class WebsocketListTests(PaneTestCase):
    def test_every_frame_gets_a_list_row_with_a_direction_icon(self) -> None:
        frames = [frame(0), frame(1, from_client=False)]
        self.pane.show_websocket(frames, WsClose())
        self.assertIsInstance(self.pane.message_list, ListView)
        self.assertEqual(self.keys(), [0, 1])
        rows = self.pane.message_list.rows()
        self.assertEqual([row.icon for row in rows], [FluentIcon.UP, FluentIcon.DOWN])
        self.assertEqual([row.payload for row in rows], frames)
        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.count, 2)
        self.assert_no_detail()

    def test_click_opens_details_and_keyboard_selection_requires_enter(self) -> None:
        content = '{\n  "message": "' + "long text " * 50 + '"\n}'
        self.pane.show_websocket(
            [frame(0, content=content.encode()), frame(1, content=b"second")],
            WsClose(),
        )
        self.pane.resize(800, 700)
        self.pane.show()
        self.app.processEvents()
        view = self.pane.message_list
        model = view.model()
        assert model is not None
        index = model.index(0, 0)
        self.assertTrue(
            wait_until(lambda: view.visualRect(index).isValid(), timeout_ms=1000)
        )
        QTest.mouseClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            pos=view.visualRect(index).center(),
        )
        self.assertEqual(self.selected_key(), 0)
        self.assertEqual(self.pane.detail_edit.text(), content)
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.detail_page)
        self.assertTrue(self.pane.detail_edit.is_read_only())
        self.assertEqual(self.pane.detail_edit.code_widget.language, Language.JSON)
        self.assertLess(len(self.pane.message_list.rows()[0].preview), len(content))
        self.assertTrue(self.pane.detail_edit.tool_widget.isHidden())
        visible_buttons = [
            button
            for button in self.pane.detail_page.findChildren(QAbstractButton)
            if button.isVisible()
            and not self.pane.detail_edit.code_widget.isAncestorOf(button)
        ]
        self.assertEqual(visible_buttons, [self.pane.back_btn, self.pane.wrap_btn])
        QTest.mouseClick(self.pane.back_btn, Qt.MouseButton.LeftButton)
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.stream_page)
        self.assertIs(QApplication.focusWidget(), view)
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(self.selected_key(), 1)
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.stream_page)
        QTest.keyClick(view, Qt.Key.Key_Return)
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.detail_page)
        self.assertEqual(self.pane.detail_edit.text(), "second")
        self.assertEqual(self.pane.detail_edit.code_widget.language, Language.TEXT)

    def test_return_keeps_scroll_and_selection_and_the_same_row_can_reopen(
        self,
    ) -> None:
        self.pane.show_websocket(
            [frame(i, content=f"message-{i}".encode()) for i in range(100)], WsClose()
        )
        self.pane.resize(480, 600)
        self.pane.show()
        self.app.processEvents()
        view = self.pane.message_list
        view.verticalScrollBar().setValue(403)
        index = view.indexAt(QPoint(view.viewport().width() // 2, 30))
        self.assertTrue(index.isValid())
        before = view.verticalScrollBar().value()
        for _ in range(2):
            QTest.mouseClick(
                view.viewport(),
                Qt.MouseButton.LeftButton,
                pos=view.visualRect(index).center(),
            )
            self.assertIs(
                self.pane.content_pages.currentWidget(), self.pane.detail_page
            )
            self.assertEqual(self.pane.detail_edit.text(), f"message-{index.row()}")
            QTest.mouseClick(self.pane.back_btn, Qt.MouseButton.LeftButton)
            self.app.processEvents()
            self.assertIs(
                self.pane.content_pages.currentWidget(), self.pane.stream_page
            )
            self.assertEqual(self.selected_key(), index.row())
            self.assertEqual(view.verticalScrollBar().value(), before)
            self.assertIs(QApplication.focusWidget(), view)

    def test_list_shows_only_content_while_status_remains_searchable(self) -> None:
        item = frame(0, dropped=True, injected=True)
        self.pane.show_websocket([item], WsClose())
        model = self.pane.message_list.model()
        assert model is not None
        self.assertEqual(model.index(0, 0).data(Qt.ItemDataRole.DisplayRole), "hi")
        for term in ("客户端", "服务端", "TEXT", "丢弃", "注入"):
            self.assertIn(term, self.pane.message_list.rows()[0].search_text)
        self.select()
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.stream_page)
        self.assertEqual(self.pane.detail_edit.text(), "")
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "hi")

    def test_binary_details_are_hex_with_offsets(self) -> None:
        item = frame(opcode=2, content=bytes(range(32)))
        self.pane.show_websocket([item], WsClose())
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), hex_dump(item.content))

    def test_binary_clipping_reports_the_bytes_actually_shown(self) -> None:
        for content, original_size, shown_size in (
            (bytes(17), 100_000, 17),
            (bytes(HEX_DUMP_LIMIT + 16), HEX_DUMP_LIMIT + 16, HEX_DUMP_LIMIT),
        ):
            with self.subTest(shown=shown_size):
                item = replace(
                    frame(opcode=2, content=content), original_size=original_size
                )
                self.pane.show_websocket([item], WsClose())
                self.open_row()
                self.assertEqual(self.pane.detail_edit.text(), hex_dump(content))
                notice = self.pane.detail_edit.notice.text()
                self.assertIn(str(shown_size), notice)
                self.assertIn(str(original_size), notice)
                self.assertFalse(self.pane.detail_edit.notice.isHidden())

    def test_text_preview_clipping_is_not_silently_presented_as_full_payload(
        self,
    ) -> None:
        item = replace(frame(content=b"prefix"), original_size=80000)
        self.pane.show_websocket([item], WsClose())
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "prefix")
        self.assertIn("6", self.pane.detail_edit.notice.text())
        self.assertIn("80000", self.pane.detail_edit.notice.text())

    def test_bad_utf8_can_be_selected_without_losing_the_message(self) -> None:
        item = frame(content=b"\xff\xfe")
        self.pane.show_websocket([item], WsClose())
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), item.text())

    def test_native_websocket_snapshot_fills_the_list(self) -> None:
        flow = tflow.twebsocketflow()
        frames = ws_frames(flow.websocket)
        self.pane.set_data(build_flow_detail(flow), frames=frames)
        self.assertEqual(
            [row.preview for row in self.pane.message_list.rows()],
            [frame_preview(item) for item in frames],
        )


class SseListTests(PaneTestCase):
    def test_sse_opens_the_complete_raw_event_without_a_view_switch(self) -> None:
        event = parse_sse(
            'event: price\ndata: {"px": 1}\nid: 7\nretry: 3000\n: comment\n\n'
        )[0]
        self.pane.show_sse_events([event])
        self.assertEqual(self.pane.message_list.rows()[0].icon, FluentIcon.DOWN)
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), event.raw)
        self.assertEqual(self.pane.detail_edit.code_widget.language, Language.TEXT)
        self.assertTrue(self.pane.detail_edit.tool_widget.isHidden())

    def test_multiline_data_is_preserved_in_the_detail(self) -> None:
        event = parse_sse("data: one\ndata: two\n\n")[0]
        self.pane.show_sse_events([event])
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), event.raw)
        self.assertNotIn("\n", self.pane.message_list.rows()[0].preview)

    def test_long_metadata_keeps_the_window_usable_and_raw_event_complete(self) -> None:
        event = parse_sse(
            "event: " + "long-event-" * 300 + "\n"
            "id: "
            + "long-id-" * 300
            + "\n"
            + ": description\n" * 60
            + "data: value\n\n"
        )[0]
        self.pane.resize(480, 600)
        self.pane.show_sse_events([event])
        self.pane.show()
        self.app.processEvents()
        self.open_row()
        self.app.processEvents()
        self.assertEqual(self.pane.height(), 600)
        self.assertLess(self.pane.minimumSizeHint().height(), 600)
        self.assertGreater(self.pane.detail_edit.code_widget.height(), 300)
        self.assertEqual(self.pane.detail_edit.text(), event.raw)

    def test_heartbeat_and_metadata_only_blocks_have_selectable_raw_details(
        self,
    ) -> None:
        for text in (": <b>keep-alive</b>\n\n", "id: last\nretry: 3000\n\n"):
            with self.subTest(text=text):
                event = parse_sse(text)[0]
                self.pane.show_sse_events([event])
                self.assertEqual(self.pane.count, 1)
                self.assertEqual(self.pane.message_list.message_count(), 1)
                self.open_row()
                self.assertEqual(self.pane.detail_edit.text(), event.raw)

    def test_empty_data_is_an_event_and_keeps_its_raw_field(self) -> None:
        event = parse_sse("data:\n\n")[0]
        self.pane.show_sse_events([event])
        self.open_row()
        self.assertEqual(self.pane.count, 1)
        self.assertEqual(self.pane.detail_edit.text(), event.raw)

    def test_heartbeat_rows_share_the_message_capacity(self) -> None:
        with patch("ferret.apps.common.flow.messages.MESSAGE_ROW_LIMIT", 3):
            for index in range(10):
                self.pane.append_event(
                    replace(parse_sse(f": heartbeat-{index}\n\n")[0], index=index)
                )
        self.assertEqual(self.keys(), [7, 8, 9])
        self.assertEqual(self.pane.count, 10)

    def test_archive_wins_and_history_without_archive_falls_back_to_body(self) -> None:
        archived = parse_sse("data: from-archive\n\n")
        self.pane.set_data(sse_detail("data: from-body\n\n"), events=archived)
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), archived[0].raw)
        for events in (None, []):
            self.pane.set_data(sse_detail("data: from-body\n\n"), events=events)
            self.open_row()
            self.assertEqual(
                self.pane.detail_edit.text(), parse_sse("data: from-body\n\n")[0].raw
            )

    def test_empty_live_sse_remains_applicable_and_accepts_first_event(self) -> None:
        self.pane.show_sse_events([])
        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_LIST)
        self.assertEqual(self.pane.count, 0)
        self.assertFalse(self.pane.empty_label.isHidden())
        event = parse_sse("data: arrived\n\n")[0]
        self.pane.append_event(event)
        self.assertEqual(self.pane.count, 1)
        self.assertTrue(self.pane.empty_label.isHidden())
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), event.raw)


class ListStateTests(PaneTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pane.show_websocket(
            [frame(i, content=f"message-{i}".encode()) for i in range(4)], WsClose()
        )

    def test_append_preserves_selected_detail_and_sort_uses_stable_keys(self) -> None:
        self.open_row(1)
        self.pane.append_frame(frame(4, content=b"latest"))
        self.assertEqual(self.selected_key(), 1)
        self.assertEqual(self.pane.detail_edit.text(), "message-1")
        self.assertIs(self.pane.content_pages.currentWidget(), self.pane.detail_page)
        for descending in (True, False, True):
            self.pane.sort_btn.setChecked(descending)
            self.assertEqual(self.selected_key(), 1)
            self.assertEqual(self.pane.detail_edit.text(), "message-1")
            self.assertIs(
                self.pane.content_pages.currentWidget(), self.pane.detail_page
            )
        self.open_row(0)
        self.assertEqual(self.selected_key(), 4)
        self.assertEqual(self.pane.detail_edit.text(), "latest")

    def test_reverse_append_lands_on_top_without_replacing_the_selection(self) -> None:
        self.pane.sort_btn.setChecked(True)
        self.open_row(1)
        self.assertEqual(self.selected_key(), 2)
        self.pane.append_frame(frame(4, content=b"new"))
        self.assertEqual(self.selected_key(), 2)
        self.open_row(0)
        self.assertEqual(self.selected_key(), 4)

    def test_switching_order_at_the_newest_edge_continues_following_live_messages(
        self,
    ) -> None:
        self.pane.resize(480, 600)
        self.pane.show_websocket([frame(i) for i in range(100)], WsClose())
        self.pane.show()
        self.app.processEvents()
        view = self.pane.message_list
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        view.scroll_to_newest()
        for index, descending in enumerate((True, False), start=100):
            self.pane.sort_btn.setChecked(descending)
            self.assertTrue(view.at_newest_edge())
            self.pane.append_frame(frame(index))
            self.assertTrue(view.at_newest_edge())

    def test_filtering_clears_hidden_selection_and_restoring_filter_preserves_rows(
        self,
    ) -> None:
        self.open_row(0)
        self.pane.filter_input.setText("MESSAGE-1")
        self.assertEqual(self.pane.message_list.visible_count(), 1)
        self.assertEqual(self.pane.message_list.message_count(), 4)
        self.assert_no_detail()
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "message-1")
        self.pane.filter_input.clear()
        self.assertEqual(self.pane.message_list.visible_count(), 4)
        self.assertEqual(self.selected_key(), 1)
        self.assertEqual(self.pane.count, 4)

    def test_unmatched_filter_shows_empty_hint_and_live_matching_rows_still_arrive(
        self,
    ) -> None:
        self.pane.filter_input.setText("incoming")
        self.assertEqual(self.pane.message_list.visible_count(), 0)
        self.assertFalse(self.pane.empty_label.isHidden())
        self.pane.append_frame(frame(4, content=b"hidden"))
        self.assertEqual(self.pane.message_list.visible_count(), 0)
        self.pane.append_frame(frame(5, content=b"incoming"))
        self.assertEqual(self.pane.message_list.visible_count(), 1)
        self.assertTrue(self.pane.empty_label.isHidden())
        self.assertEqual(self.pane.count, 6)
        self.open_row()
        self.assertEqual(self.selected_key(), 5)

    def test_direction_binary_and_sse_metadata_are_searchable(self) -> None:
        self.pane.show_websocket([frame(0), frame(1, from_client=False)], WsClose())
        self.pane.filter_input.setText("客户端 →")
        self.assertEqual(self.pane.message_list.visible_count(), 1)
        self.open_row()
        self.assertEqual(self.selected_key(), 0)
        self.pane.show_websocket([frame(opcode=2, content=b"\xde\xad")], WsClose())
        self.pane.filter_input.setText("DE AD")
        self.assertEqual(self.pane.message_list.visible_count(), 1)
        self.pane.show_sse_events(
            parse_sse(
                "event: price\ndata: value\nid: event-7\nretry: 3000\n\ndata: other\n\n"
            )
        )
        for term in ("PRICE", "event-7", "3000"):
            self.pane.filter_input.setText(term)
            self.assertEqual(self.pane.message_list.visible_count(), 1)

    def test_display_clear_keeps_total_and_filter_but_releases_detail(self) -> None:
        self.pane.filter_input.setText("message")
        self.open_row()
        self.pane.clear_btn.click()
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.pane.count, 4)
        self.assertEqual(self.pane.filter_input.text(), "message")
        self.assertTrue(self.pane.applicable)
        self.assert_no_detail()
        self.pane.append_frame(frame(4, content=b"message-4"))
        self.assertEqual(self.keys(), [4])
        self.assertEqual(self.pane.count, 5)
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "message-4")

    def test_switching_flows_releases_filter_selection_and_payload(self) -> None:
        self.open_row()
        self.pane.filter_input.setText("message")
        self.pane.show_websocket([frame(0, content=b"different")], WsClose())
        self.assertEqual(self.pane.filter_input.text(), "")
        self.assert_no_detail()
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "different")

    def test_sort_button_icon_and_tooltip_follow_the_order(self) -> None:
        ascending_tip = self.pane.sort_btn.toolTip()
        self.pane.sort_btn.setChecked(True)
        self.assertTrue(self.pane.message_list.descending)
        self.assertNotEqual(self.pane.sort_btn.toolTip(), ascending_tip)
        self.pane.sort_btn.setChecked(False)
        self.assertFalse(self.pane.message_list.descending)
        self.assertEqual(self.pane.sort_btn.toolTip(), ascending_tip)


class CapacityAndCloseTests(PaneTestCase):
    def test_capacity_keeps_newest_rows_and_count_keeps_the_total(self) -> None:
        self.assertEqual(MESSAGE_ROW_LIMIT, WS_FRAME_LIMIT)
        with patch("ferret.apps.common.flow.messages.MESSAGE_ROW_LIMIT", 3):
            self.pane.show_websocket([frame(i) for i in range(5)], WsClose())
            self.assertEqual(self.keys(), [2, 3, 4])
            self.assertEqual(self.pane.count, 5)
            self.pane.append_frame(frame(5))
            self.assertEqual(self.keys(), [3, 4, 5])
            self.assertEqual(self.pane.count, 6)
        self.pane.set_count(1000)
        self.assertEqual(self.pane.count, 1000)
        self.assertEqual(self.pane.message_list.message_count(), 3)

    def test_evicting_selected_oldest_message_clears_detail_in_either_order(
        self,
    ) -> None:
        with patch("ferret.apps.common.flow.messages.MESSAGE_ROW_LIMIT", 3):
            for descending in (False, True):
                with self.subTest(descending=descending):
                    self.pane.show_websocket([frame(i) for i in range(3)], WsClose())
                    self.pane.sort_btn.setChecked(descending)
                    self.open_row(2 if descending else 0)
                    self.assertEqual(self.selected_key(), 0)
                    self.pane.append_frame(frame(3))
                    self.assertEqual(self.keys(), [1, 2, 3])
                    self.assert_no_detail()

    def test_evicting_another_message_preserves_the_selected_details(self) -> None:
        with patch("ferret.apps.common.flow.messages.MESSAGE_ROW_LIMIT", 3):
            self.pane.show_websocket(
                [frame(i, content=str(i).encode()) for i in range(3)], WsClose()
            )
            self.open_row(1)
            self.pane.append_frame(frame(3))
            self.assertEqual(self.selected_key(), 1)
            self.assertEqual(self.pane.detail_edit.text(), "1")

    def test_close_info_is_separate_updates_and_does_not_consume_message_capacity(
        self,
    ) -> None:
        self.pane.show_websocket([frame()], WsClose())
        self.assertTrue(self.pane.close_label.isHidden())
        close = WsClose(True, 1000, "<b>finished</b>", 1700000000.5)
        self.pane.set_close(close)
        self.assertFalse(self.pane.close_label.isHidden())
        text = self.pane.close_label.text()
        for part in (
            "客户端",
            "1000",
            "<b>finished</b>",
            frame_time(close.timestamp_end),
        ):
            self.assertIn(part, text)
        self.assertEqual(self.pane.close_label.textFormat(), Qt.TextFormat.PlainText)
        self.pane.sort_btn.setChecked(True)
        self.assertEqual(self.pane.close_label.text(), text)
        self.pane.set_close(
            replace(close, closed_by_client=False, close_reason="later")
        )
        self.assertIn("服务端", self.pane.close_label.text())
        self.assertIn("later", self.pane.close_label.text())
        self.assertNotIn("finished", self.pane.close_label.text())
        self.assertEqual(self.pane.message_list.message_count(), 1)
        self.assertEqual(self.pane.count, 1)
        self.pane.set_close(WsClose())
        self.assertTrue(self.pane.close_label.isHidden())

    def test_multiline_close_reason_does_not_expand_the_window(self) -> None:
        reason = "<b>done</b>" + "\n" * 100
        self.assertLessEqual(len(reason.encode()), 123)
        self.pane.resize(480, 600)
        self.pane.show_websocket([frame()], WsClose())
        self.pane.show()
        self.app.processEvents()
        self.pane.set_close(WsClose(True, 1000, reason, 1700000000.0))
        self.app.processEvents()
        self.assertEqual(self.pane.height(), 600)
        self.assertGreater(self.pane.message_list.height(), 120)
        tooltip = self.pane.close_label.toolTip()
        self.assertIn("&lt;b&gt;done&lt;/b&gt;", tooltip)
        self.assertNotIn("<b>done</b>", tooltip)
        self.assertEqual(tooltip.count("<br>"), 100)


class ModeSelectionTests(PaneTestCase):
    def test_fresh_and_plain_flows_are_not_applicable(self) -> None:
        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.count, 0)
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertFalse(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_PLACEHOLDER)
        self.pane.set_data({"Method": "GET", "URL": "https://x/y"})
        self.assertFalse(self.pane.applicable)

    def test_websocket_handshake_with_no_frames_is_still_applicable(self) -> None:
        self.pane.set_data({"raw_state": {"websocket": {"messages": []}}}, [])
        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_LIST)
        self.assertEqual(self.pane.message_list.message_count(), 0)

    def test_first_frame_or_event_can_establish_the_mode(self) -> None:
        self.pane.append_frame(frame())
        self.assertTrue(self.pane.applicable)
        self.assertEqual(self.keys(), [0])
        event = parse_sse("data: sse\n\n")[0]
        self.pane.append_event(event)
        self.assertEqual(self.pane.message_list.rows()[0].payload, event)
        self.assertEqual(self.pane.count, 1)

    def test_parameterised_sse_type_is_recognised_and_protocol_switch_clears_detail(
        self,
    ) -> None:
        self.pane.set_data(
            sse_detail("data: hi\n\n", "text/event-stream; charset=utf-8")
        )
        self.assertEqual(self.pane.pages.currentIndex(), PAGE_LIST)
        self.open_row()
        self.pane.set_data({"raw_state": {"websocket": {}}}, [frame()], WsClose())
        self.assert_no_detail()
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), "hi")
        self.pane.set_data(build_flow_detail(tflow.tflow(resp=True)))
        self.assertEqual(self.keys(), [])
        self.assertEqual(self.pane.count, 0)
        self.assert_no_detail()


class PlainTextRenderingTests(PaneTestCase):
    MARKUP = "<b>KEEP-TAGS</b>"

    def test_message_content_and_sse_fields_preserve_protocol_markup(self) -> None:
        self.pane.show_websocket([frame(content=self.MARKUP.encode())], WsClose())
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), self.MARKUP)
        self.assertEqual(self.pane.message_list.rows()[0].preview, self.MARKUP)
        event = parse_sse(f"event: {self.MARKUP}\ndata: {self.MARKUP}\n\n")[0]
        self.pane.show_sse_events([event])
        self.open_row()
        self.assertEqual(self.pane.detail_edit.text(), event.raw)

    def test_a_field_card_value_is_plain_text(self) -> None:
        section = Section(title="X", fields=(Field("备注", "comment"),))
        card = FieldCard(section)
        self.addCleanup(card.deleteLater)
        card.set_data({"comment": self.MARKUP})
        rows = card.rows()
        self.assertTrue(rows)
        for row in rows:
            if not row.heading:
                self.assertEqual(row.value, self.MARKUP)


if __name__ == "__main__":
    unittest.main()
