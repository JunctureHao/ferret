import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.flow.views import FlowViewerPane


class FlowViewerPaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.viewer = FlowViewerPane()
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
        with patch.object(self.viewer.panel, "set_data") as set_data:
            self.viewer.table.row_selected.emit({"id": "flow-1"})
            self.app.processEvents()

        set_data.assert_not_called()
        self.assertEqual(self.viewer.sizes()[1], 0)

    def test_single_click_updates_open_panel_without_changing_ratio(self) -> None:
        self.viewer.setSizes([650, 250])
        self.app.processEvents()
        before = self.viewer.sizes()
        data = {"id": "flow-2"}

        with patch.object(self.viewer.panel, "set_data") as set_data:
            self.viewer.table.row_selected.emit(data)
            self.app.processEvents()

        set_data.assert_called_once_with(data)
        self.assertEqual(self.viewer.sizes(), before)

    def test_double_click_opens_horizontal_panel_equally(self) -> None:
        self.viewer.setOrientation(Qt.Orientation.Horizontal)
        self.viewer.collapse_panel()
        data = {"id": "flow-3"}

        with patch.object(self.viewer.panel, "set_data") as set_data:
            self.viewer.table.row_double_clicked.emit(data)
            self.app.processEvents()

        set_data.assert_called_once_with(data)
        first, second = self.viewer.sizes()
        self.assertGreater(second, 0)
        self.assertLessEqual(abs(first - second), 1)

    def test_double_click_opens_vertical_panel_equally(self) -> None:
        self.viewer.setOrientation(Qt.Orientation.Vertical)
        self.viewer.collapse_panel()

        with patch.object(self.viewer.panel, "set_data"):
            self.viewer.table.row_double_clicked.emit({"id": "flow-4"})
            self.app.processEvents()

        first, second = self.viewer.sizes()
        available = self.viewer.height() - self.viewer.handleWidth()
        minimum_detail = self.viewer.panel.minimumSizeHint().height()
        expected_second = max(available - available // 2, minimum_detail)
        self.assertGreater(second, 0)
        self.assertEqual(first + second, available)
        self.assertLessEqual(abs(second - expected_second), 1)

    def test_close_request_collapses_panel(self) -> None:
        self.viewer.setSizes([450, 450])
        self.app.processEvents()
        self.viewer.panel.collapseRequested.emit()
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
            ["#", "Method", "URL", "Status", "Type", "Size", "Time"],
        )

    def test_all_table_columns_are_user_resizable(self) -> None:
        header = self.viewer.table.horizontalHeader()
        for column in range(self.viewer.table.model().columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column),
                header.ResizeMode.Interactive,
            )

    def test_detail_header_keeps_current_flow_context(self) -> None:
        data = {
            "Method": "GET",
            "URL": "https://api.example.com/v1/users",
            "Status Code": 200,
            "Duration": "128 ms",
        }
        self.viewer.table.row_double_clicked.emit(data)
        self.app.processEvents()

        self.assertEqual(self.viewer.panel.context_method.text(), "GET")
        self.assertEqual(
            self.viewer.panel.context_url.text(),
            "https://api.example.com/v1/users",
        )
        self.assertEqual(self.viewer.panel.context_status.text(), "200")
        self.assertEqual(self.viewer.panel.context_duration.text(), "128 ms")


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

            def replay_flow(self, flow_id):
                self.replay_calls.append(("single", flow_id))

            def replay_flows(self, flows):
                self.replay_calls.append(("multi", list(flows)))

            def save_flows(self, flows, path):
                self.save_calls.append((flows, path))

        return StubController()

    def _make_flow(self):
        from mitmproxy.test import tflow

        return tflow.tflow(resp=True)

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

    def test_block_host_requested_is_empty_without_a_host(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        hosts: list = []
        menu.block_host_requested.connect(hosts.append)
        menu.update_context(0, {"id": "flow-1"}, [])
        menu.block_host_action.trigger()
        self.assertEqual(hosts, [""])


if __name__ == "__main__":
    unittest.main()
