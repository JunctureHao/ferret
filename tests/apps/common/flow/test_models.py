import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gzip

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.models import (
    DURATION_MS_ROLE,
    FULL_URL_ROLE,
    MIME_ROLE,
    SIZE_BYTES_ROLE,
    SORT_ROLE,
    STATUS_KIND_ROLE,
    FlowProxyModel,
    FlowTableModel,
    format_duration,
)
from ferret.core.mitm import build_flow_detail


class _ListSource:
    """FlowSource 的最小替身：钉住 model 只依赖协议三方法（迭代/clear/remove）。"""

    def __init__(self, flows: list) -> None:
        self.flows = list(flows)
        self.cleared = False
        self.removed: list = []

    def __iter__(self):
        return iter(self.flows)

    def clear(self) -> None:
        self.cleared = True
        self.flows.clear()

    def remove(self, flows) -> None:
        self.removed.extend(flows)


class FlowTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def completed_flow(*, duration_ms=128, content_type="application/json"):
        flow = tflow.tflow(resp=True)
        flow.request.timestamp_start = 100.0
        flow.response.timestamp_end = 100.0 + duration_ms / 1000  # type: ignore
        flow.response.headers["Content-Type"] = content_type  # type: ignore
        flow.request.raw_content = b"req"
        flow.response.raw_content = b"response"  # type: ignore
        return flow

    def model_with(self, *flows) -> FlowTableModel:
        model = FlowTableModel(None)  # type: ignore
        model._rows = list(flows)
        return model

    def test_set_source_seeds_rows_and_refresh_rebuilds(self) -> None:
        """set_source 入表即拉一次 source；refresh 重新迭代 source 重建行集。"""
        first = self.completed_flow()
        source = _ListSource([first])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        self.assertEqual(model.rowCount(), 1)
        self.assertIs(model.get_flow(0), first)

        source.flows.append(self.completed_flow())
        model.handle_refresh()
        self.assertEqual(model.rowCount(), 2)

    def test_clear_data_and_remove_row_delegate_to_the_source(self) -> None:
        """clear/remove 只经 source 走 —— model 不再直连 View（AGENTS.md §3）。"""
        first = self.completed_flow()
        second = self.completed_flow()
        source = _ListSource([first, second])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        model.remove_row(0)
        self.assertEqual(source.removed, [first])

        model.clear_data()
        self.assertTrue(source.cleared)
        self.assertEqual(model.rowCount(), 0)

    def test_handle_add_is_ignored_before_a_source_is_attached(self) -> None:
        model = FlowTableModel(None)  # type: ignore
        model.handle_add(self.completed_flow())
        self.assertEqual(model.rowCount(), 0)

    def test_columns_and_semantic_roles(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)

        self.assertEqual(
            model.HEADERS, ("#", "Method", "URL", "Status", "Type", "Size", "Time")
        )
        self.assertEqual(model.data(model.index(0, 2)), flow.request.pretty_url)
        self.assertEqual(model.data(model.index(0, 3)), 200)
        self.assertEqual(model.data(model.index(0, 4)), "JSON")
        self.assertEqual(model.data(model.index(0, 5)), "11b")
        self.assertEqual(model.data(model.index(0, 6)), "128 ms")
        self.assertEqual(model.data(model.index(0, 3), STATUS_KIND_ROLE), "success")
        self.assertEqual(
            model.data(model.index(0, 2), FULL_URL_ROLE), flow.request.pretty_url
        )
        self.assertEqual(model.data(model.index(0, 4), MIME_ROLE), "application/json")
        self.assertAlmostEqual(model.data(model.index(0, 6), DURATION_MS_ROLE), 128)
        self.assertEqual(model.data(model.index(0, 5), SIZE_BYTES_ROLE), 11)

    def test_pending_and_error_states_keep_text(self) -> None:
        pending = tflow.tflow()
        error = tflow.tflow(err=True)
        model = self.model_with(pending, error)

        self.assertEqual(model.data(model.index(0, 3)), "Pending")
        self.assertEqual(model.data(model.index(0, 3), STATUS_KIND_ROLE), "pending")
        self.assertEqual(model.data(model.index(1, 3)), "Error")
        self.assertEqual(model.data(model.index(1, 3), STATUS_KIND_ROLE), "error")

    def test_duration_formats_boundaries(self) -> None:
        self.assertEqual(format_duration(0.2), "< 1 ms")
        self.assertEqual(format_duration(128), "128 ms")
        self.assertEqual(format_duration(1420), "1.42 s")

    def test_sort_uses_numeric_duration(self) -> None:
        slow = self.completed_flow(duration_ms=1200)
        fast = self.completed_flow(duration_ms=90)
        model = self.model_with(slow, fast)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(6, Qt.SortOrder.AscendingOrder)

        first = proxy.index(0, 6)
        self.assertAlmostEqual(first.data(DURATION_MS_ROLE), 90)
        self.assertIsInstance(first.data(SORT_ROLE), float)

    def test_proxy_row_numbers_stay_contiguous_after_sorting(self) -> None:
        flows = [
            self.completed_flow(duration_ms=duration) for duration in (300, 100, 200)
        ]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(6, Qt.SortOrder.AscendingOrder)

        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [2, 3, 1],
        )

    def test_number_column_sorts_by_stable_sequence(self) -> None:
        flows = [self.completed_flow(duration_ms=value) for value in (300, 100, 200)]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)

        proxy.sort(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [3, 2, 1],
        )

        proxy.sort(0, Qt.SortOrder.AscendingOrder)
        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [1, 2, 3],
        )

    def test_url_combines_host_and_path(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)
        self.assertEqual(model.data(model.index(0, 2)), flow.request.pretty_url)

    def test_horizontal_headers_are_left_aligned(self) -> None:
        model = FlowTableModel(None)  # type: ignore
        expected = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        for column in range(model.columnCount()):
            self.assertEqual(
                model.headerData(
                    column,
                    Qt.Orientation.Horizontal,
                    Qt.ItemDataRole.TextAlignmentRole,
                ),
                expected,
            )

    def test_the_size_column_and_the_detail_wire_rows_agree(self) -> None:
        """两处数字必须一致 —— 而且是结构上一致：同走 `wire_size()` 一个函数。

        表格这侧在 `_size_bytes`、详情那侧在 `build_flow_detail`，两段代码离得远，
        但都从 `core.mitm` 里 import 同一个函数。改造前表格量压缩后、详情量解压后，
        同一条 gzip 响应两处能差好几倍，用户没法判断哪个是真的。
        """
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"y" * 2048)
        model = self.model_with(flow)
        data = build_flow_detail(flow)

        column = model.data(model.index(0, 5), SIZE_BYTES_ROLE)
        self.assertEqual(data["req_wire_size"] + data["res_wire_size"], column)

    def test_the_size_tooltip_states_the_caliber(self) -> None:
        """列宽只放得下一个总数，口径得靠 tooltip 说清。"""
        model = self.model_with(tflow.tflow(resp=True))
        tooltip = model.data(model.index(0, 5), Qt.ItemDataRole.ToolTipRole)

        self.assertIn("wire", tooltip)
        self.assertIn("Request", tooltip)
        self.assertIn("Response", tooltip)


if __name__ == "__main__":
    unittest.main()
