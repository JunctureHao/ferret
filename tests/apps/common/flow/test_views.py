from __future__ import annotations

import os
import unittest
from dataclasses import replace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.flow.columns import default_layout, logical_index
from ferret.apps.common.flow.models import HIGHLIGHT_ROLE
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.core.mitm import WsClose, WsFrame, build_flow_detail, flow_row, parse_sse


class _Source:
    def __init__(self, rows):
        self.rows = list(rows)

    def __iter__(self):
        return iter(self.rows)

    def clear(self):
        self.rows.clear()

    def remove(self, ids):
        self.rows[:] = [row for row in self.rows if row.id not in ids]


class _Controller(QObject):
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)
    sse_started = Signal(str)
    sse_event = Signal(str, object)
    sse_ended = Signal(str)

    def __init__(self):
        super().__init__()
        self.details: dict[str, dict] = {}
        self.requests: list[str] = []
        self.full_requests: list[str] = []
        self.body_requests: list[tuple[str, str]] = []
        self.message_requests: list[str] = []
        self.events: list = []
        self.frames: list = []

    def flow_detail(self, flow_id):
        self.requests.append(flow_id)
        self.full_requests.append(flow_id)
        return self.details.get(flow_id, {"id": flow_id})

    def flow_summary(self, flow_id):
        self.requests.append(flow_id)
        data = self.details.get(flow_id, {"id": flow_id})
        summary = {
            key: value
            for key, value in data.items()
            if " Body" not in key
            and not key.endswith("_decoded_size")
            and key not in ("raw_state", "curl_command", "Request Form")
        }
        if "raw_state" in data:
            websocket = data["raw_state"].get("websocket") is not None
            kind = (
                "websocket"
                if websocket
                else (
                    "sse"
                    if data.get("Response Content-Type") == "text/event-stream"
                    else ""
                )
            )
            summary.update(
                is_websocket=websocket,
                message_kind=kind,
                message_count=len(self.frames if websocket else self.events),
            )
        return summary

    def flow_body(self, flow_id, side):
        self.body_requests.append((flow_id, side))
        return {
            key: value
            for key, value in self.details.get(flow_id, {}).items()
            if key.startswith(f"{side} Body")
            or key in (f"{side} Content-Type", "Request Form")
        }

    def flow_messages(self, flow_id):
        self.message_requests.append(flow_id)
        data = self.details.get(flow_id, {})
        websocket = data.get("raw_state", {}).get("websocket") is not None
        kind = "websocket" if websocket else "sse"
        items = self.frames if websocket else self.events
        return {
            "kind": kind,
            "count": len(items),
            "frames": list(self.frames),
            "close": WsClose(),
            "events": list(self.events),
        }

    def total_count(self):
        return len(self.details)

    def get_raw_request(self, _flow_id):
        return ""

    def get_raw_response(self, _flow_id):
        return ""

    def sse_events(self, _flow_id):
        return list(self.events)

    def websocket_frames(self, _flow_id):
        return list(self.frames)

    def websocket_close(self, _flow_id):
        return WsClose()


class FlowViewerPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.controller = _Controller()
        self.viewer = FlowViewerPane(controller=self.controller)
        self.viewer.resize(900, 600)
        self.viewer.show()
        self.app.processEvents()
        self.viewer.collapse_panel()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.viewer.close()
        self.viewer.deleteLater()
        self.app.processEvents()

    def test_single_click_does_not_open_collapsed_panel(self) -> None:
        self._load_flows(tflow.tflow(resp=True))
        self.viewer.table.selectRow(0)
        self.app.processEvents()

        self.assertIsNone(self.viewer.panel)
        self.assertEqual(self.controller.requests, [])
        self.assertEqual(self.viewer.sizes()[1], 0)

    def test_single_click_updates_open_panel_without_changing_ratio(self) -> None:
        # 4ee294f 起空态会隐藏详情面板，QSplitter 对隐藏件不分配尺寸；
        # 本测试要的是「面板已展开」状态，先显式恢复显示。
        panel = self.viewer._ensure_panel()
        panel.setVisible(True)
        self.viewer.setSizes([650, 250])
        self.app.processEvents()
        before = self.viewer.sizes()
        data = {"id": "flow-2"}

        with patch.object(panel, "set_data") as set_data:
            self.viewer.table.row_selected.emit(data)
            self.app.processEvents()

        set_data.assert_called_once_with(data)
        self.assertEqual(self.viewer.sizes(), before)

    def test_double_click_opens_horizontal_panel_equally(self) -> None:
        self.viewer.setOrientation(Qt.Orientation.Horizontal)
        self.viewer.collapse_panel()
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.table.row_double_clicked.emit({"id": flow.id})
        self.app.processEvents()
        assert self.viewer.panel is not None
        self.assertEqual(self.viewer.panel.datas["id"], flow.id)
        self.assertEqual(self.controller.requests, [flow.id])
        self.assertEqual(self.controller.full_requests, [])
        first, second = self.viewer.sizes()
        self.assertGreater(second, 0)
        self.assertLessEqual(abs(first - second), 1)

    def test_double_click_opens_vertical_panel_equally(self) -> None:
        self.viewer.setOrientation(Qt.Orientation.Vertical)
        self.viewer.collapse_panel()
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.table.row_double_clicked.emit({"id": flow.id})
        self.app.processEvents()

        first, second = self.viewer.sizes()
        available = self.viewer.height() - self.viewer.handleWidth()
        assert self.viewer.panel is not None
        minimum_detail = self.viewer.panel.minimumSizeHint().height()
        expected_second = max(available - available // 2, minimum_detail)
        self.assertGreater(second, 0)
        self.assertEqual(first + second, available)
        self.assertLessEqual(abs(second - expected_second), 1)

    def test_close_request_collapses_panel(self) -> None:
        panel = self.viewer._ensure_panel()
        panel.setVisible(True)
        self.viewer.setSizes([450, 450])
        self.app.processEvents()
        panel.collapseRequested.emit()
        self.app.processEvents()
        self.assertEqual(self.viewer.sizes()[1], 0)

    def test_empty_state_tracks_capture_and_filter_context(self) -> None:
        self.viewer.set_capture_context(
            capture_state="running",
            endpoint="127.0.0.1:8080",
            total_count=0,
            shown_count=0,
            active_filter_count=0,
        )
        self.assertEqual(self.viewer.empty_state.title.text(), "等待流量")
        self.assertEqual(self.viewer.empty_state.subtitle.text(), "127.0.0.1:8080")

        self.viewer.set_capture_context(
            capture_state="running",
            endpoint="127.0.0.1:8080",
            total_count=10,
            shown_count=0,
            active_filter_count=2,
        )
        self.assertEqual(self.viewer.empty_state.title.text(), "没有匹配结果")
        self.assertEqual(self.viewer.empty_state.subtitle.text(), "当前有 2 个有效条件")

    def test_table_defaults_to_newest_first(self) -> None:
        header = self.viewer.table.horizontalHeader()
        self.assertEqual(header.sortIndicatorSection(), 0)
        self.assertEqual(header.sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)
        self.assertEqual(
            [
                self.viewer.table.source_model.headerData(i, Qt.Orientation.Horizontal)
                for i in range(self.viewer.table.source_model.columnCount())
            ],
            ["#", "标记", "Method", "URL", "Status", "Type", "Size", "Time"],
        )

    def test_all_table_columns_but_the_mark_are_user_resizable(self) -> None:
        """Mark 列为固定列不可拖拽；其余列都归用户拖。"""
        header = self.viewer.table.horizontalHeader()
        mark = self.viewer.table.source_model.HEADERS.index("Mark")
        for column in range(self.viewer.table.model().columnCount()):
            expected = (
                header.ResizeMode.Fixed
                if column == mark
                else header.ResizeMode.Interactive
            )
            with self.subTest(column=column):
                self.assertEqual(header.sectionResizeMode(column), expected)
        self.assertEqual(header.sectionSize(mark), 64)

    def test_detail_panel_consumes_the_row_data(self) -> None:
        """双击行 → 详情字典进面板并切到详情页（顶部上下文条已随改造移除）。"""
        data = {
            "id": "flow-5",
            "Method": "GET",
            "URL": "https://api.example.com/v1/users",
            "Status Code": 200,
            "duration_ms": 128.0,
        }
        self.controller.details["flow-5"] = data
        self.viewer.table.row_double_clicked.emit({"id": "flow-5"})
        self.app.processEvents()

        assert self.viewer.panel is not None
        self.assertEqual(self.viewer.panel.datas, data)
        self.assertEqual(self.viewer.panel.stack.currentIndex(), 1)

    def _load_flows(self, *flows):
        self.controller.details = {flow.id: build_flow_detail(flow) for flow in flows}
        source = _Source(flow_row(flow) for flow in flows)
        self.viewer.set_source(source)
        self.app.processEvents()
        return source

    def test_housekeeping_does_not_create_panel_or_tree(self) -> None:
        flow = tflow.tflow(resp=True)
        source = self._load_flows(flow)
        row = source.rows[0]
        self.viewer.set_highlight_ids({row.id})
        self.viewer.on_flow_updated(row)
        self.viewer.on_view_refreshed()
        self.viewer.set_controller(self.controller)
        self.viewer.collapse_panel()
        self.viewer.clear_all()
        self.app.processEvents()
        self.assertIsNone(self.viewer.panel)
        self.assertIsNone(self.viewer.tree)
        self.assertEqual(self.controller.requests, [])

    def test_enter_reads_latest_detail_once_and_reuses_panel(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.table.selectRow(0)
        latest = {**self.controller.details[flow.id], "comment": "latest"}
        self.controller.details[flow.id] = latest
        self.viewer.open_selected()
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertEqual(self.controller.requests, [flow.id])
        self.assertEqual(self.controller.full_requests, [])
        self.assertEqual(panel.datas["id"], flow.id)
        self.assertEqual(panel.datas["comment"], "latest")
        self.assertNotIn("Response Body", panel.datas)
        self.assertEqual(self.controller.body_requests, [])
        self.viewer.collapse_panel()
        self.viewer.open_selected()
        self.app.processEvents()
        self.assertIs(self.viewer.panel, panel)
        self.assertEqual(self.viewer.count(), 2)

    def test_enter_without_selection_keeps_panel_lazy(self) -> None:
        self._load_flows(tflow.tflow(resp=True))
        self.viewer.open_selected()
        self.assertIsNone(self.viewer.panel)
        self.assertEqual(self.controller.requests, [])

    def test_drag_opens_latest_selection_and_does_not_refetch_per_pixel(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.table.selectRow(0)
        self.viewer.setOrientation(Qt.Orientation.Horizontal)
        self.viewer.moveSplitter(450, 1)
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertEqual(panel.datas["id"], flow.id)
        self.assertEqual(self.controller.requests, [flow.id])
        self.viewer.moveSplitter(400, 1)
        self.app.processEvents()
        self.assertEqual(self.controller.requests, [flow.id])
        self.viewer.collapse_panel()
        self.viewer.moveSplitter(450, 1)
        self.app.processEvents()
        self.assertEqual(self.controller.requests, [flow.id, flow.id])

    def test_drag_without_selection_opens_neutral_empty_page(self) -> None:
        self._load_flows(tflow.tflow(resp=True))
        self.viewer.moveSplitter(450, 1)
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertIs(panel.stack.currentWidget(), panel.empty_page)
        self.assertEqual(panel.datas, {})
        self.assertEqual(self.controller.requests, [])

    def test_drag_after_clearing_selection_releases_the_previous_flow(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.table.selectRow(0)
        self.viewer.open_selected()
        self.app.processEvents()
        self.viewer.collapse_panel()
        self.viewer.table.clearSelection()
        self.viewer.moveSplitter(450, 1)
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertIs(panel.stack.currentWidget(), panel.empty_page)
        self.assertEqual(panel.datas, {})
        self.assertEqual(self.controller.requests, [flow.id])
        self.assertIsNone(panel.messages)
        self.controller.websocket_frame.emit(
            flow.id, WsFrame(0, False, 1, b"old", 1.0, False, False)
        )
        self.assertIsNone(panel.messages)
        self.assertEqual(self.controller.message_requests, [])

    def test_queued_callbacks_are_cancelled_when_viewer_is_destroyed(self) -> None:
        flow = tflow.tflow(resp=True)
        self.controller.details[flow.id] = build_flow_detail(flow)
        viewer = FlowViewerPane(controller=self.controller)
        viewer.resize(900, 600)
        viewer.show()
        viewer.set_source(_Source([flow_row(flow)]))
        viewer.table.selectRow(0)
        viewer.set_grouping_mode("conn")
        viewer.set_grouping_mode("flat")
        viewer.open_selected()
        with patch("sys.excepthook") as errors:
            viewer.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        errors.assert_not_called()

    def test_right_click_fetches_context_without_creating_panel(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        index = self.viewer.table.proxy_model.index(0, 0)
        pos = self.viewer.table.visualRect(index).center()
        with patch.object(self.viewer.table.context_menu, "exec"):
            self.viewer.table.customContextMenuRequested.emit(pos)
        self.assertEqual(self.controller.requests, [flow.id])
        self.assertEqual(self.viewer.table.context_menu.row_data["id"], flow.id)
        self.assertIsNone(self.viewer.panel)

    def test_tree_first_open_uses_latest_source_and_highlights(self) -> None:
        old = tflow.tflow(resp=True)
        source = self._load_flows(old)
        added = flow_row(tflow.tflow(resp=True))
        source.rows.append(added)
        self.viewer.on_flow_added(added)
        updated = replace(added, status_code=201)
        source.rows[1] = updated
        self.viewer.on_flow_updated(updated)
        removed = source.rows.pop(0)
        self.viewer.on_flow_removed(removed, 0)
        self.viewer.set_highlight_ids({updated.id})
        self.assertIsNone(self.viewer.tree)
        self.viewer.set_grouping_mode("conn")
        self.app.processEvents()
        tree = self.viewer.tree
        assert tree is not None
        self.assertEqual(tree.source_model.child_count(), 1)
        parent = tree.source_model.index(0, 0)
        child = tree.source_model.index(0, 0, parent)
        self.assertEqual(tree.source_model.flow_at(child), updated)
        self.assertTrue(child.data(HIGHLIGHT_ROLE))
        self.viewer.set_grouping_mode("flat")
        self.viewer.set_grouping_mode("conn")
        self.assertIs(self.viewer.tree, tree)
        self.assertIsNone(self.viewer.panel)

    def test_late_tree_receives_latest_layout_and_controller(self) -> None:
        layout = default_layout().with_visible("size", False).with_width("url", 321)
        with patch("ferret.apps.common.flow.views.save_layout"):
            self.viewer.table._commit_column_layout(layout)
        controller = _Controller()
        self.viewer.set_controller(controller)
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        self.assertIs(tree.controller, controller)
        self.assertIs(tree.context_menu.controller, controller)
        self.assertTrue(tree.isColumnHidden(logical_index("size")))
        self.assertEqual(tree.columnWidth(logical_index("url")), 321)
        self.assertIsNone(self.viewer.panel)

    def test_connection_selection_is_lazy_and_double_click_only_expands_tree(
        self,
    ) -> None:
        self._load_flows(tflow.tflow(resp=True))
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        parent = tree.proxy_model.index(0, 0)
        tree.setCurrentIndex(parent)
        tree.doubleClicked.emit(parent)
        self.app.processEvents()
        self.assertIsNone(self.viewer.panel)
        self.assertEqual(self.controller.requests, [])
        self.viewer.open_selected()
        self.app.processEvents()
        assert self.viewer.panel is not None
        self.assertEqual(self.viewer.panel.stack.currentIndex(), 2)
        self.assertEqual(self.controller.requests, [])

    def test_tree_child_selection_is_lazy_until_double_click(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        parent = tree.proxy_model.index(0, 0)
        child = tree.proxy_model.index(0, 0, parent)
        tree.setCurrentIndex(child)
        self.assertEqual(self.controller.requests, [])
        self.assertIsNone(self.viewer.panel)
        tree.doubleClicked.emit(child)
        self.app.processEvents()
        self.assertEqual(self.controller.requests, [flow.id])
        assert self.viewer.panel is not None
        self.assertEqual(self.viewer.panel.datas["id"], flow.id)

    def test_hidden_view_cannot_replace_stats_or_selection(self) -> None:
        flow = tflow.tflow(resp=True)
        self._load_flows(flow)
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        self.viewer.set_grouping_mode("flat")
        self.viewer.table.selectRow(0)
        self.viewer.open_selected()
        self.app.processEvents()
        received = []
        self.viewer.stats_updated.connect(lambda *values: received.append(values))
        self.controller.requests.clear()
        tree.stats_updated.emit(10, 0, 0)
        tree.row_selected.emit({"id": "hidden"})
        self.assertEqual(received, [])
        self.assertEqual(self.controller.requests, [])
        self.assertIs(self.viewer.table_stack.currentWidget(), self.viewer.table)
        self.viewer.table.stats_updated.emit(10, 1, 1)
        self.assertEqual(received, [(10, 1, 1)])
        self.viewer.set_grouping_mode("conn")
        received.clear()
        self.viewer.table.stats_updated.emit(10, 0, 0)
        self.viewer.table.row_selected.emit({"id": "hidden"})
        self.assertEqual(received, [])
        self.assertEqual(self.controller.requests, [])
        tree.stats_updated.emit(10, 1, 1)
        self.assertEqual(received, [(10, 1, 1)])

    def test_menus_from_late_tree_use_pane_signals(self) -> None:
        received = []
        self.viewer.replay_file_requested.connect(lambda: received.append("replay"))
        self.viewer.block_host_requested.connect(received.append)
        self.viewer.edit_in_compose_requested.connect(received.append)
        self.viewer.add_to_mock_requested.connect(received.append)
        self.viewer.table.context_menu.replay_file_requested.emit()
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        tree.context_menu.replay_file_requested.emit()
        tree.context_menu.block_host_requested.emit("host")
        tree.context_menu.edit_in_compose_requested.emit("flow-id")
        tree.context_menu.add_to_mock_requested.emit(["flow-id"])
        self.assertEqual(received, ["replay", "replay", "host", "flow-id", ["flow-id"]])

    def test_late_panel_recovers_sse_archive_and_receives_new_events(self) -> None:
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["content-type"] = "text/event-stream"
        flow.response.content = b""
        self._load_flows(flow)
        self.viewer.table.selectRow(0)
        self.controller.events = parse_sse("data: before-open\n\n")
        self.controller.sse_started.emit(flow.id)
        self.assertIsNone(self.viewer.panel)
        self.viewer.open_selected()
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertIsNone(panel.messages)
        self.assertEqual(self.controller.message_requests, [])
        panel.res_pane.setCurrentTab("Messages")
        assert panel.messages is not None
        self.assertEqual(panel.messages.count, 1)
        self.controller.sse_event.emit(
            flow.id, replace(parse_sse("data: after-open\n\n")[0], index=1)
        )
        self.assertEqual(panel.messages.count, 2)
        controller = _Controller()
        controller.details = dict(self.controller.details)
        controller.events = parse_sse("data: before-open\n\ndata: after-open\n\n")
        self.viewer.set_controller(controller)
        self.assertEqual(panel.datas, {})
        self.controller.sse_event.emit(
            flow.id, parse_sse("data: old-controller\n\n")[0]
        )
        self.assertEqual(panel.messages.count, 0)
        self.assertEqual(panel.messages.stream.message_count(), 0)
        self.viewer.open_selected()
        self.app.processEvents()
        self.assertEqual(panel.messages.count, 2)
        controller.sse_event.emit(
            flow.id, replace(parse_sse("data: new-controller\n\n")[0], index=2)
        )
        self.assertEqual(panel.messages.count, 3)

    def test_websocket_start_updates_open_overview_and_table_url(self) -> None:
        flow = tflow.twebsocketflow(messages=False)
        flow.request.url = "https://example.com/ws?token=a%2Fb"
        request_state = flow.request.get_state()
        response, websocket = flow.response, flow.websocket
        flow.response = None
        flow.websocket = None
        source = self._load_flows(flow)
        self.viewer.table.selectRow(0)
        self.viewer.open_selected()
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        panel.req_tabs.setCurrentTab("Overview")
        overview = panel.overview
        assert overview is not None

        def overview_url() -> str:
            return next(
                row.value
                for card in overview.cards
                for row in card.rows()
                if row.label == "URL"
            )

        model = self.viewer.table.source_model
        url_index = model.index(0, logical_index("url"))
        self.assertEqual(model.data(url_index), flow.request.pretty_url)
        self.assertEqual(overview_url(), flow.request.pretty_url)
        self.assertEqual(panel.datas["Scheme"], "https")
        self.controller.requests.clear()
        with patch.object(overview, "set_data", wraps=overview.set_data) as render:
            self.controller.websocket_started.emit("another-flow")
            self.app.processEvents()
            render.assert_not_called()
            self.assertEqual(self.controller.requests, [])

            flow.response, flow.websocket = response, websocket
            self.controller.details[flow.id] = build_flow_detail(flow)
            source.rows[0] = flow_row(flow)
            self.viewer.on_flow_updated(source.rows[0])
            self.controller.websocket_started.emit(flow.id)
            self.app.processEvents()
            render.assert_called_once()

        expected_url = "wss://example.com/ws?token=a%2Fb"
        self.assertEqual(model.data(url_index), expected_url)
        self.assertEqual(overview_url(), expected_url)
        self.assertEqual(panel.datas["URL"], expected_url)
        self.assertEqual(panel.datas["Scheme"], "wss")
        self.assertEqual(panel.datas["Status Code"], 101)
        self.assertEqual(self.controller.requests, [flow.id])
        self.assertEqual(self.controller.full_requests, [])
        self.assertEqual(self.controller.message_requests, [])
        self.assertEqual(flow.request.get_state(), request_state)

    def test_late_panel_recovers_websocket_frames_and_receives_new_frames(self) -> None:
        flow = tflow.twebsocketflow()
        self._load_flows(flow)
        self.viewer.table.selectRow(0)
        self.controller.frames = [WsFrame(0, True, 1, b"before", 1.0, False, False)]
        self.controller.websocket_started.emit(flow.id)
        self.assertIsNone(self.viewer.panel)
        self.viewer.open_selected()
        self.app.processEvents()
        panel = self.viewer.panel
        assert panel is not None
        self.assertIsNone(panel.messages)
        self.assertEqual(self.controller.message_requests, [])
        panel.res_pane.setCurrentTab("Messages")
        assert panel.messages is not None
        self.assertEqual(panel.messages.count, 1)
        self.controller.websocket_frame.emit(
            flow.id, WsFrame(1, False, 1, b"after", 2.0, False, False)
        )
        self.assertEqual(panel.messages.count, 2)

    def test_created_tree_stays_current_when_hidden(self) -> None:
        source = self._load_flows(tflow.tflow(resp=True))
        self.viewer.set_grouping_mode("conn")
        tree = self.viewer.tree
        assert tree is not None
        self.viewer.set_grouping_mode("flat")
        added = flow_row(tflow.tflow(resp=True))
        source.rows.append(added)
        self.viewer.on_flow_added(added)
        updated = replace(added, status_code=201)
        source.rows[1] = updated
        self.viewer.on_flow_updated(updated)
        removed = source.rows.pop(0)
        self.viewer.on_flow_removed(removed, 0)
        self.viewer.set_grouping_mode("conn")
        self.app.processEvents()
        self.assertEqual(tree.source_model.child_count(), 1)
        parent = tree.source_model.index(0, 0)
        child = tree.source_model.index(0, 0, parent)
        self.assertEqual(tree.source_model.flow_at(child), updated)
        self.viewer.clear_all()
        self.app.processEvents()
        self.assertEqual(tree.source_model.child_count(), 0)
        self.assertEqual(self.viewer.table.source_model.rowCount(), 0)
        self.assertIsNone(self.viewer.panel)


class MenuReExportTests(unittest.TestCase):
    """三个菜单搬去了 `menus.py`，但两个挂载点照旧从 `views` 导入。

    `views.py` 里那两行 ``# noqa: F401`` 看上去像死代码，删了不会报错 ——
    只会在右键菜单弹不出来的时候才发现。这条把三个名字都钉住。
    """

    def test_views_still_exposes_the_three_menus(self) -> None:
        from ferret.apps.common.flow import menus, views

        for name in ("FlowContextMenu", "FlowExportMenu", "FlowSubViewMenu"):
            with self.subTest(name=name):
                self.assertIs(getattr(views, name), getattr(menus, name))


class FlowContextMenuTests(unittest.TestCase):
    """Replay menu entry visibility and multi-select dispatch."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from ferret.apps.common.flow.protocols import (
            CAPTURE_CAPABILITIES,
            READONLY_CAPABILITIES,
        )
        from ferret.apps.common.flow.views import FlowContextMenu

        self.CAPTURE_CAPABILITIES = CAPTURE_CAPABILITIES
        self.READONLY_CAPABILITIES = READONLY_CAPABILITIES
        self.FlowContextMenu = FlowContextMenu
        # Parent must have a window() for QFileDialog calls.
        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def _make_menu(self, capabilities):
        controller = self._make_controller()
        return self.FlowContextMenu(self.parent, controller, capabilities)

    def _make_controller(self):
        """Build a stub controller exposing replay_flows/replay_flow."""

        class StubController:
            def __init__(self) -> None:
                self.replay_calls: list = []
                self.save_calls: list = []
                self.bodies = {
                    "request_body": b"content",
                    "response_body": b"\x00\x01binary",
                    "raw_request": b"GET /path HTTP/1.1\r\n",
                    "raw_response": b"HTTP/1.1 200 OK\r\n",
                    "raw_flow": b"raw-flow-bytes",
                }

            def replay_flow(self, flow_id):
                self.replay_calls.append(("single", flow_id))

            def replay_flows(self, flows):
                self.replay_calls.append(("multi", list(flows)))

            def save_flows(self, flows, path):
                self.save_calls.append((flows, path))

            def get_request_body(self, flow_id):
                return self.bodies["request_body"]

            def get_response_body(self, flow_id):
                return self.bodies["response_body"]

            def get_raw_request(self, flow_id):
                return self.bodies["raw_request"]

            def get_raw_response(self, flow_id):
                return self.bodies["raw_response"]

            def get_raw_flow(self, flow_id):
                return self.bodies["raw_flow"]

        return StubController()

    def _make_flow(self):
        from mitmproxy.test import tflow

        from ferret.core.mitm import flow_row

        # 菜单选区收行快照（#90）：在内核侧同位置折叠。
        return flow_row(tflow.tflow(resp=True))

    def test_capture_capabilities_show_replay_actions(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        # Both replay actions should be present in the action list.
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertIn("重发", actions_text)
        self.assertIn("从文件回放…", actions_text)

    def test_readonly_capabilities_hide_replay_actions(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("重发", actions_text)
        self.assertNotIn("从文件回放…", actions_text)

    def test_single_selection_calls_replay_flow(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        menu.update_context(0, {"id": "flow-1"}, [])
        # Trigger the replay action.
        menu.client_replay_action.trigger()
        self.assertEqual(len(menu.controller.replay_calls), 1)
        kind, payload = menu.controller.replay_calls[0]
        self.assertEqual(kind, "single")
        self.assertEqual(payload, "flow-1")

    def test_multi_selection_calls_replay_flows(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        flows = [self._make_flow() for _ in range(3)]
        menu.update_context(0, {"id": flows[0].id}, flows)
        # Action label should reflect count.
        self.assertIn("3", menu.client_replay_action.text())
        menu.client_replay_action.trigger()
        self.assertEqual(len(menu.controller.replay_calls), 1)
        kind, payload = menu.controller.replay_calls[0]
        self.assertEqual(kind, "multi")
        self.assertEqual(len(payload), 3)

    def test_replay_file_requested_signal_emits(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        signals: list = []
        menu.replay_file_requested.connect(lambda: signals.append(1))
        menu.replay_from_file_action.trigger()
        self.assertEqual(signals, [1])

    def test_capture_capabilities_show_block_host_action(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertIn("屏蔽此主机", actions_text)

    def test_readonly_capabilities_hide_block_host_action(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("屏蔽此主机", actions_text)

    def test_block_host_requested_carries_the_row_host(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        hosts: list = []
        menu.block_host_requested.connect(hosts.append)
        menu.update_context(0, {"id": "flow-1", "Host": "ads.example.com"}, [])
        menu.block_host_action.trigger()
        self.assertEqual(hosts, ["ads.example.com"])

    def test_capture_capabilities_show_edit_in_compose(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertIn("在 Compose 中编辑", actions_text)

    def test_readonly_capabilities_hide_edit_in_compose(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("在 Compose 中编辑", actions_text)

    def test_edit_in_compose_is_enabled_for_a_single_http_flow(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        flow = self._make_flow()
        menu.update_context(0, {"id": flow.id, "Method": "GET"}, [flow])
        self.assertTrue(menu.edit_in_compose_action.isEnabled())

    def test_edit_in_compose_is_disabled_for_multi_selection(self) -> None:
        """多选「编辑并重发」语义不明 —— 禁用最诚实。"""
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        flows = [self._make_flow() for _ in range(2)]
        menu.update_context(0, {"id": flows[0].id, "Method": "GET"}, flows)
        self.assertFalse(menu.edit_in_compose_action.isEnabled())

    def test_edit_in_compose_is_disabled_for_connect(self) -> None:
        """CONNECT 是隧道请求，没有可编辑的报文形态。"""
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        flow = self._make_flow()
        menu.update_context(0, {"id": flow.id, "Method": "CONNECT"}, [flow])
        self.assertFalse(menu.edit_in_compose_action.isEnabled())

    def test_edit_in_compose_carries_the_flow_id(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        flow = self._make_flow()
        ids: list = []
        menu.edit_in_compose_requested.connect(ids.append)
        menu.update_context(0, {"id": flow.id, "Method": "GET"}, [flow])
        menu.edit_in_compose_action.trigger()
        self.assertEqual(ids, [flow.id])

    def test_block_host_requested_is_empty_without_a_host(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        hosts: list = []
        menu.block_host_requested.connect(hosts.append)
        menu.update_context(0, {"id": "flow-1"}, [])
        menu.block_host_action.trigger()
        self.assertEqual(hosts, [""])

    def _save_actions(self, menu):
        return {
            "request_body": menu.export_menu.save_request_body_action,
            "response_body": menu.export_menu.save_response_body_action,
            "raw_request": menu.export_menu.save_raw_request_action,
            "raw_response": menu.export_menu.save_raw_response_action,
            "raw_flow": menu.export_menu.save_raw_flow_action,
        }

    def test_save_actions_show_under_both_capability_sets(self) -> None:
        """Save 组不设能力门控：抓包页与只读会话页都拿得到（getter 两页都实现）。"""
        for capabilities in (self.CAPTURE_CAPABILITIES, self.READONLY_CAPABILITIES):
            with self.subTest(capabilities=capabilities):
                menu = self._make_menu(capabilities)
                actions_text = [
                    action.text()
                    for action in menu.export_menu.actions()
                    if action.text()
                ]
                for text in (
                    "另存请求体为文件…",
                    "另存响应体为文件…",
                    "另存原始请求为文件…",
                    "另存原始响应为文件…",
                    "另存原始流量为文件…",
                ):
                    self.assertIn(text, actions_text)

    def test_save_bytes_writes_the_getter_bytes_verbatim(self) -> None:
        """文件逐字节等于 getter 字节 —— 二进制不变形，本功能的立身之本。"""
        import tempfile
        from pathlib import Path

        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        menu.update_context(0, {"id": "flow-1"}, [])

        with tempfile.TemporaryDirectory() as tmp:
            for kind, action in self._save_actions(menu).items():
                target = str(Path(tmp) / f"{kind}.bin")
                with patch(
                    "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                    return_value=(target, ""),
                ):
                    action.trigger()
                    self.app.processEvents()
                self.assertEqual(
                    Path(target).read_bytes(), menu.controller.bodies[kind]
                )

    def test_cancelling_the_dialog_has_no_side_effects(self) -> None:
        import tempfile
        from pathlib import Path

        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        menu.update_context(0, {"id": "flow-1"}, [])

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                return_value=("", ""),
            ) as dialog,
        ):
            menu.export_menu.save_response_body_action.trigger()
            self.app.processEvents()
            self.assertEqual(dialog.call_count, 1)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_an_empty_body_warns_without_writing(self) -> None:
        import tempfile
        from pathlib import Path

        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        menu.update_context(0, {"id": "flow-1"}, [])
        menu.controller.bodies["response_body"] = b""

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName"
            ) as dialog,
            patch("ferret.apps.common.flow.menus.show_warning") as warning,
        ):
            menu.export_menu.save_response_body_action.trigger()
            self.app.processEvents()

            dialog.assert_not_called()
            warning.assert_called_once()
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
