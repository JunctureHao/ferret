import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.session.controllers import SessionController
from ferret.apps.session.views import SessionViewerPage


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
        page.splitter.setSizes([450, 450])
        self.app.processEvents()
        self.assertGreater(page.splitter.sizes()[1], 0)

        page.panel.res_panel.close_button.click()
        self.app.processEvents()

        self.assertEqual(page.splitter.sizes()[1], 0)
        page.close()


if __name__ == "__main__":
    unittest.main()
