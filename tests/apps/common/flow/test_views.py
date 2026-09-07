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
        # 4ee294f 起空态会隐藏详情面板，QSplitter 对隐藏件不分配尺寸；
        # 本测试要的是「面板已展开」状态，先显式恢复显示。
        self.viewer.panel.setVisible(True)
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
        self.assertEqual(self.viewer.empty_state.title.text(), "Waiting for traffic")
        self.assertEqual(self.viewer.empty_state.subtitle.text(), "127.0.0.1:8080")

        self.viewer.set_capture_context(
            capture_state="running",
            endpoint="127.0.0.1:8080",
            total_count=10,
            shown_count=0,
            active_filter_count=2,
        )
        self.assertEqual(self.viewer.empty_state.title.text(), "No matches")
        self.assertEqual(
            self.viewer.empty_state.subtitle.text(), "2 active condition(s)"
        )

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

    def test_detail_panel_consumes_the_row_data(self) -> None:
        """双击行 → 详情字典进面板并切到详情页（顶部上下文条已随改造移除）。"""
        data = {
            "Method": "GET",
            "URL": "https://api.example.com/v1/users",
            "Status Code": 200,
            "Duration": "128 ms",
        }
        self.viewer.table.row_double_clicked.emit(data)
        self.app.processEvents()

        self.assertEqual(self.viewer.panel.datas, data)
        self.assertEqual(self.viewer.panel.stack.currentIndex(), 1)


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

        return tflow.tflow(resp=True)

    def test_capture_capabilities_show_replay_actions(self) -> None:
        menu = self._make_menu(self.CAPTURE_CAPABILITIES)
        # Both replay actions should be present in the action list.
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertIn("Replay", actions_text)
        self.assertIn("Replay from file...", actions_text)

    def test_readonly_capabilities_hide_replay_actions(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("Replay", actions_text)
        self.assertNotIn("Replay from file...", actions_text)

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
        self.assertIn("Block this host", actions_text)

    def test_readonly_capabilities_hide_block_host_action(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("Block this host", actions_text)

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
        self.assertIn("Edit in Compose", actions_text)

    def test_readonly_capabilities_hide_edit_in_compose(self) -> None:
        menu = self._make_menu(self.READONLY_CAPABILITIES)
        actions_text = [a.text() for a in menu.actions() if a.text()]
        self.assertNotIn("Edit in Compose", actions_text)

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
                    "Save request body...",
                    "Save response body...",
                    "Save raw request...",
                    "Save raw response...",
                    "Save raw flow...",
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

        with tempfile.TemporaryDirectory() as tmp, patch(
            "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
            return_value=("", ""),
        ) as dialog:
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

        with tempfile.TemporaryDirectory() as tmp, patch(
            "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName"
        ) as dialog, patch(
            "ferret.apps.common.flow.menus.show_warning"
        ) as warning:
            menu.export_menu.save_response_body_action.trigger()
            self.app.processEvents()

            dialog.assert_not_called()
            warning.assert_called_once()
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
