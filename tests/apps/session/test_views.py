from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.session.controllers import SessionController, SessionViewController
from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.views import SessionViewerPage
from ferret.core.mitm import detail as detail_module


def _meta(flow_count: int = 1) -> SessionMeta:
    return SessionMeta(
        schema_version=1,
        session_id="sid",
        name="capture",
        path=Path("capture.flow"),
        created_at=datetime.now(UTC),
        modified_at=datetime.now(UTC),
        flow_count=flow_count,
        file_size=128,
        source=SessionSource.CAPTURE,
    )


class SessionViewerPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.addCleanup(
            QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete
        )
        self.controller = SessionController(repository=Mock())
        self.addCleanup(self.controller.deleteLater)
        self.page = SessionViewerPage(self.controller)
        self.addCleanup(self.page.deleteLater)

    def load_flow(self):
        flow = tflow.tflow(resp=True)
        vc = SessionViewController(_meta(), [flow], self.controller)
        self.page.load(_meta(), vc)
        self.page.resize(900, 600)
        self.page.show()
        self.app.processEvents()
        return vc, flow

    def test_viewer_uses_shared_flow_interaction(self) -> None:
        page = self.page
        self.assertIsNone(page.splitter)
        splitter = page._ensure_viewer()
        self.assertIsInstance(splitter, FlowViewerPane)
        self.assertIs(page.splitter, splitter)
        self.assertIs(page.table, splitter.table)
        self.assertIsNone(splitter.panel)
        self.assertIsNone(splitter.tree)
        self.assertIs(page._ensure_viewer(), splitter)

    def test_collapsed_single_click_does_not_open_detail(self) -> None:
        vc, _flow = self.load_flow()
        splitter = self.page._ensure_viewer()
        with patch.object(vc, "flow_summary", wraps=vc.flow_summary) as detail:
            self.page.table.selectRow(0)
            self.app.processEvents()

        detail.assert_not_called()
        self.assertIsNone(splitter.panel)
        self.assertEqual(splitter.sizes()[1], 0)

    def test_detail_close_button_collapses_panel(self) -> None:
        _vc, flow = self.load_flow()
        splitter = self.page._ensure_viewer()
        self.page.table.selectRow(0)
        splitter.open_selected()
        self.app.processEvents()
        panel = splitter.panel
        assert panel is not None
        self.assertEqual(panel.datas["id"], flow.id)
        self.assertIsNone(panel.comment_pane)
        panel.req_tabs.setCurrentTab("Comment")
        assert panel.comment_pane is not None
        self.assertTrue(panel.comment_pane.edit.is_read_only())
        self.assertGreater(splitter.sizes()[1], 0)

        panel.res_pane.close_button.click()
        self.app.processEvents()

        self.assertEqual(splitter.sizes()[1], 0)
        self.assertIs(splitter.panel, panel)

    def test_loading_another_session_before_opening_detail_uses_latest_controller(self):
        self.load_flow()
        vc, flow = self.load_flow()
        splitter = self.page._ensure_viewer()
        self.assertIsNone(splitter.panel)
        self.page.table.selectRow(0)
        splitter.open_selected()
        panel = splitter.panel
        assert panel is not None
        self.assertIs(panel.controller, vc)
        self.assertEqual(panel.datas["id"], flow.id)

    def test_body_is_only_built_for_the_opened_side_and_current_session(self):
        vc, flow = self.load_flow()
        assert flow.response is not None
        flow.response.content = b'{"session": 1}'
        splitter = self.page._ensure_viewer()
        with (
            patch.object(vc, "flow_detail", wraps=vc.flow_detail) as full_detail,
            patch.object(
                detail_module, "build_body", wraps=detail_module.build_body
            ) as body,
        ):
            self.page.table.selectRow(0)
            splitter.open_selected()
            self.app.processEvents()
            panel = splitter.panel
            assert panel is not None
            full_detail.assert_not_called()
            body.assert_not_called()
            self.assertIsNone(panel.req_body)
            self.assertIsNone(panel.res_pane.body_pane)
            panel.res_pane.setCurrentTab("Body")
            body.assert_called_once_with(flow, flow.response)
            response_body = panel.res_pane.body_pane
            assert response_body is not None
            self.assertIn('"session": 1', response_body.json_panel.plain_text())
            panel.res_pane.setCurrentTab("Headers")
            panel.res_pane.setCurrentTab("Body")
            self.assertEqual(body.call_count, 1)

        next_flow = tflow.tflow(resp=True)
        assert next_flow.response is not None
        next_flow.response.content = b'{"session": 2}'
        next_vc = SessionViewController(_meta(), [next_flow], self.controller)
        self.page.load(_meta(), next_vc)
        self.assertEqual(panel.datas, {})
        self.page.table.selectRow(0)
        splitter.open_selected()
        self.app.processEvents()
        self.assertIs(panel.res_pane.body_pane, response_body)
        self.assertIn('"session": 2', response_body.json_panel.plain_text())
        self.assertNotIn('"session": 1', response_body.json_panel.plain_text())


class SessionViewControllerBodyTests(unittest.TestCase):
    """会话页的 body 两件：死 flow 直读，不依赖任何内核（同 raw 三件的读法）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_bodies_come_from_the_loaded_flows(self) -> None:
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.content = b"dead-payload"
        vc = SessionViewController(_meta(), [flow])

        self.assertEqual(vc.get_request_body(flow.id), b"content")
        self.assertEqual(vc.get_response_body(flow.id), b"dead-payload")

    def test_an_unknown_id_yields_empty_bytes(self) -> None:
        vc = SessionViewController(_meta(), [tflow.tflow(resp=True)])
        self.assertEqual(vc.get_request_body("nope"), b"")
        self.assertEqual(vc.get_response_body("nope"), b"")


if __name__ == "__main__":
    unittest.main()
