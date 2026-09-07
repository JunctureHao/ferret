import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.session.controllers import SessionController, SessionViewController
from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.apps.session.views import SessionViewerPage


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

    def test_viewer_uses_shared_flow_interaction(self) -> None:
        controller = SessionController()
        page = SessionViewerPage(controller)
        self.assertIsInstance(page.splitter, FlowViewerPane)
        self.assertIs(page.table, page.splitter.table)
        self.assertIs(page.panel, page.splitter.panel)
        page.close()

    def test_collapsed_single_click_does_not_open_detail(self) -> None:
        controller = SessionController()
        page = SessionViewerPage(controller)
        page.resize(900, 600)
        page.show()
        self.app.processEvents()

        with patch.object(page.panel, "set_data") as set_data:
            page.table.row_selected.emit({"id": "flow-1"})
            self.app.processEvents()

        set_data.assert_not_called()
        self.assertEqual(page.splitter.sizes()[1], 0)
        page.close()

    def test_detail_close_button_collapses_panel(self) -> None:
        controller = SessionController()
        page = SessionViewerPage(controller)
        page.resize(900, 600)
        page.show()
        self.app.processEvents()
        # 4ee294f 起无流量时空态会隐藏详情面板，先恢复显示才能谈「展开后收起」。
        page.panel.setVisible(True)
        page.splitter.setSizes([450, 450])
        self.app.processEvents()
        self.assertGreater(page.splitter.sizes()[1], 0)

        page.panel.res_pane.close_button.click()
        self.app.processEvents()

        self.assertEqual(page.splitter.sizes()[1], 0)
        page.close()


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
