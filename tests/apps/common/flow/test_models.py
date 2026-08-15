import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

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


class FlowTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def completed_flow(*, duration_ms=128, content_type="application/json"):
        flow = tflow.tflow(resp=True)
        flow.request.timestamp_start = 100.0
        flow.response.timestamp_end = 100.0 + duration_ms / 1000
        flow.response.headers["Content-Type"] = content_type
        flow.request.raw_content = b"req"
        flow.response.raw_content = b"response"
        return flow

    def model_with(self, *flows) -> FlowTableModel:
        model = FlowTableModel(None)
        model._rows = list(flows)
        return model

    def test_columns_and_semantic_roles(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)

        self.assertEqual(
            model.HEADERS, ["#", "Method", "URL", "Status", "Type", "Size", "Time"]
        )
        self.assertEqual(model.data(model.index(0, 2)), flow.request.pretty_url)
        self.assertEqual(model.data(model.index(0, 3)), 200)
        self.assertEqual(model.data(model.index(0, 4)), "JSON")
        self.assertEqual(model.data(model.index(0, 5)), "11 B")
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

        self.assertEqual(model.data(model.index(0, 3)), "等待中")
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
        proxy = FlowProxyModel(None)
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
        proxy = FlowProxyModel(None)
        proxy.setSourceModel(model)
        proxy.sort(6, Qt.SortOrder.AscendingOrder)

        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [2, 3, 1],
        )

    def test_number_column_sorts_by_stable_sequence(self) -> None:
        flows = [self.completed_flow(duration_ms=value) for value in (300, 100, 200)]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)
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
        model = FlowTableModel(None)
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

    def test_cell_alignment_unchanged(self) -> None:
        # 单元格对齐不得因表头左对齐改动而改变
        flow = self.completed_flow()
        model = self.model_with(flow)

        # 第 0 列 (#) 单元格右对齐
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.TextAlignmentRole),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
        )
        # 第 1 列 (Method) 单元格居中
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole),
            int(Qt.AlignmentFlag.AlignCenter),
        )
        # 第 2 列 (URL) 单元格默认左对齐（模型未指定，返回 None）
        self.assertIsNone(
            model.data(model.index(0, 2), Qt.ItemDataRole.TextAlignmentRole),
        )


if __name__ == "__main__":
    unittest.main()
