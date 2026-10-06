"""Mock 页的装配与模型契约（docs/design.md#mock）。

模型是纯 Qt 表格；视图用离屏 QApplication 真构造一遍 —— 六张设置卡的
CONFIG 绑定、信号接线和摘要同步只有真装配时才露头。控制器走真
MitmFacade（内核不起）：add 需要活副本、必须报「内核未运行」，其余池
操作在死对象上就地执行（与 test_facade.py / test_serverplayback.py 同一手法的
界面侧延伸）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.mock.controllers import MockController
from ferret.apps.mock.models import MockPoolFilterProxyModel, MockPoolTableModel
from ferret.apps.mock.views import MockInterface
from ferret.core.mitm import FlowFile, MitmFacade, MitmRuntime

app = QApplication.instance() or QApplication([])


class MockPoolModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = MockPoolTableModel()
        self.proxy = MockPoolFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.entries = [
            {
                "id": "a",
                "method": "GET",
                "url": "http://x.example/api",
                "status": 200,
                "size": "1 B",
            },
            {
                "id": "b",
                "method": "POST",
                "url": "http://y.example/login",
                "status": 404,
                "size": "2 B",
            },
        ]
        self.model.set_entries(self.entries)

    def test_headers_and_rows(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 4)

    def test_filter_matches_method_and_url(self) -> None:
        self.proxy.set_filter_text("post")
        self.assertEqual(self.proxy.rowCount(), 1)
        self.proxy.set_filter_text("login")
        self.assertEqual(self.proxy.rowCount(), 1)
        self.proxy.set_filter_text("no-such-thing")
        self.assertEqual(self.proxy.rowCount(), 0)
        self.proxy.set_filter_text("")
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_entry_identity_survives_proxy_mapping(self) -> None:
        self.proxy.set_filter_text("login")
        source_row = self.proxy.mapToSource(self.proxy.index(0, 0)).row()
        entry = self.model.entry_at(source_row)
        assert entry is not None
        self.assertEqual(entry["id"], "b")


class MockViewAssemblyTests(unittest.TestCase):
    """真构造一遍 Mock 页：CONFIG 卡绑定 / 信号接线 / 空态切换都在这里露头。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch(
            "ferret.core.mitm.facade.get_mock_pool_file",
            return_value=Path(self._tmp.name) / "mock_pool.flow",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.facade = MitmFacade(MitmRuntime())
        self.controller = MockController(mitm=self.facade)
        self.view = MockInterface(controller=self.controller)

    def test_assembles_empty(self) -> None:
        self.assertEqual(self.view.source_model.rowCount(), 0)
        self.assertFalse(self.view.controller.enabled)
        self.assertEqual(self.view.content_stack.currentWidget(), self.view.empty_page)

    def test_pool_change_switches_to_table(self) -> None:
        flow = tflow.tflow(resp=True)
        self.facade.runtime.mock_pool = [flow]
        self.controller.refresh()
        self.assertEqual(self.view.source_model.rowCount(), 1)
        self.assertEqual(self.view.content_stack.currentWidget(), self.view.table)

    def test_add_requires_running_kernel_and_reports(self) -> None:
        failures = []
        self.controller.operation_failed.connect(lambda t, d: failures.append(t))
        self.controller.add_from_selection(["no-such-flow"])
        self.assertEqual(failures, ["加入 Mock 失败"])

    def test_remove_updates_snapshot_and_pool_file(self) -> None:
        flow = tflow.tflow(resp=True)
        self.facade.runtime.mock_pool = [flow]
        self.controller.refresh()
        self.controller.remove_entries([flow.id])
        self.assertEqual(self.controller.snapshot["count"], 0)
        self.assertEqual(FlowFile.read(Path(self._tmp.name) / "mock_pool.flow"), [])


if __name__ == "__main__":
    unittest.main()
