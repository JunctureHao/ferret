import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gzip

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.marks import FALLBACK_GLYPH, emoji_font, marker_glyph
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
from ferret.core.mitm import MARKER_DEFAULT, build_flow_detail


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

    def test_remove_flows_delegates_the_whole_selection_in_one_call(self) -> None:
        """批量删除只调一次 source.remove，UI 更新走 View 的 flow_removed 回路。"""
        first = self.completed_flow()
        second = self.completed_flow()
        source = _ListSource([first, second])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        model.remove_flows([first, second])
        self.assertEqual(source.removed, [first, second])

        # 空调用是空转，不得触碰 source
        model.remove_flows([])
        self.assertEqual(source.removed, [first, second])

    def test_handle_add_is_ignored_before_a_source_is_attached(self) -> None:
        model = FlowTableModel(None)  # type: ignore
        model.handle_add(self.completed_flow())
        self.assertEqual(model.rowCount(), 0)

    def test_columns_and_semantic_roles(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)

        self.assertEqual(
            model.HEADERS,
            ("#", "Mark", "Method", "URL", "Status", "Type", "Size", "Time"),
        )
        self.assertEqual(model.data(model.index(0, 3)), flow.request.pretty_url)
        self.assertEqual(model.data(model.index(0, 4)), 200)
        self.assertEqual(model.data(model.index(0, 5)), "JSON")
        self.assertEqual(model.data(model.index(0, 6)), "11b")
        self.assertEqual(model.data(model.index(0, 7)), "128 ms")
        self.assertEqual(model.data(model.index(0, 4), STATUS_KIND_ROLE), "success")
        self.assertEqual(
            model.data(model.index(0, 3), FULL_URL_ROLE), flow.request.pretty_url
        )
        self.assertEqual(model.data(model.index(0, 5), MIME_ROLE), "application/json")
        self.assertAlmostEqual(model.data(model.index(0, 7), DURATION_MS_ROLE), 128)
        self.assertEqual(model.data(model.index(0, 6), SIZE_BYTES_ROLE), 11)

    def test_the_mark_column_sits_right_after_the_row_number(self) -> None:
        """表头文案「标记」与详情字段、筛选字段同名；列名 Mark 是取值不是文案。"""
        model = FlowTableModel(None)  # type: ignore
        self.assertEqual(model.HEADERS.index("Mark"), 1)
        self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal), "标记")

    def test_the_mark_column_renders_the_glyph_and_hides_the_shortcode(self) -> None:
        marked = self.completed_flow()
        marked.marked = ":bug:"
        plain = self.completed_flow()
        model = self.model_with(marked, plain)

        self.assertEqual(model.data(model.index(0, 1)), marker_glyph(":bug:"))
        self.assertNotEqual(model.data(model.index(0, 1)), ":bug:")
        # 认不出图形的人悬浮看短码原文。
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.ToolTipRole), ":bug:"
        )
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole),
            int(Qt.AlignmentFlag.AlignCenter),
        )
        # 未标记：格子空着、也不弹一个空 tooltip。
        self.assertEqual(model.data(model.index(1, 1)), "")
        self.assertIsNone(model.data(model.index(1, 1), Qt.ItemDataRole.ToolTipRole))

    def test_the_mark_column_paints_marked_cells_with_an_emoji_font(self) -> None:
        """标记格走 emoji-first 字体（✈ ♉ 这类文本态符号才画得出彩色）；
        delegate 认的是 FontRole，设在视图上会被盖掉。未标记格不掺字体。"""
        marked = self.completed_flow()
        marked.marked = ":bug:"
        plain = self.completed_flow()
        model = self.model_with(marked, plain)

        font = model.data(model.index(0, 1), Qt.ItemDataRole.FontRole)
        self.assertEqual(font, emoji_font(18))
        self.assertEqual(font.families()[0], "Segoe UI Emoji")
        self.assertIsNone(model.data(model.index(1, 1), Qt.ItemDataRole.FontRole))

    def test_the_mark_column_keeps_a_glyph_for_odd_markers(self) -> None:
        """`:default:`（旧开关写的值）查表就是 `"●"`；别人存的 .flow 里的野值不在
        字典里，走兜底 —— 两种都得有东西，不能空着也不能把短码原文铺进去。"""
        default = self.completed_flow()
        default.marked = MARKER_DEFAULT
        alien = self.completed_flow()
        alien.marked = ":no-such-emoji:"
        model = self.model_with(default, alien)
        self.assertEqual(model.data(model.index(0, 1)), marker_glyph(MARKER_DEFAULT))
        self.assertEqual(model.data(model.index(1, 1)), FALLBACK_GLYPH)

    def test_sorting_by_mark_separates_marked_from_unmarked(self) -> None:
        """SORT_ROLE 是短码字符串本身：空串与有值天然分堆，同类短码聚族。"""
        flows = [self.completed_flow() for _ in range(4)]
        flows[0].marked = ":fire:"
        flows[2].marked = ":bug:"
        model = self.model_with(*flows)
        self.assertEqual(model.data(model.index(0, 1), SORT_ROLE), ":fire:")
        self.assertEqual(model.data(model.index(1, 1), SORT_ROLE), "")

        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(1, Qt.SortOrder.DescendingOrder)
        marks = [proxy.data(proxy.index(row, 1), SORT_ROLE) for row in range(4)]
        self.assertEqual(marks, [":fire:", ":bug:", "", ""])

    def test_pending_and_error_states_keep_text(self) -> None:
        pending = tflow.tflow()
        error = tflow.tflow(err=True)
        model = self.model_with(pending, error)

        self.assertEqual(model.data(model.index(0, 4)), "等待中")
        self.assertEqual(model.data(model.index(0, 4), STATUS_KIND_ROLE), "pending")
        self.assertEqual(model.data(model.index(1, 4)), "Error")
        self.assertEqual(model.data(model.index(1, 4), STATUS_KIND_ROLE), "error")

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
        proxy.sort(7, Qt.SortOrder.AscendingOrder)

        first = proxy.index(0, 7)
        self.assertAlmostEqual(first.data(DURATION_MS_ROLE), 90)
        self.assertIsInstance(first.data(SORT_ROLE), float)

    def test_proxy_row_numbers_stay_contiguous_after_sorting(self) -> None:
        flows = [
            self.completed_flow(duration_ms=duration) for duration in (300, 100, 200)
        ]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(7, Qt.SortOrder.AscendingOrder)

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
        self.assertEqual(model.data(model.index(0, 3)), flow.request.pretty_url)

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

        column = model.data(model.index(0, 6), SIZE_BYTES_ROLE)
        self.assertEqual(data["req_wire_size"] + data["res_wire_size"], column)

    def test_the_size_tooltip_states_the_caliber(self) -> None:
        """列宽只放得下一个总数，口径得靠 tooltip 说清。"""
        model = self.model_with(tflow.tflow(resp=True))
        tooltip = model.data(model.index(0, 6), Qt.ItemDataRole.ToolTipRole)

        self.assertIn("线上", tooltip)
        self.assertIn("请求", tooltip)
        self.assertIn("响应", tooltip)


if __name__ == "__main__":
    unittest.main()
