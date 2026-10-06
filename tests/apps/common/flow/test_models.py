import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gzip
from dataclasses import replace

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.marks import FALLBACK_GLYPH, emoji_font, marker_glyph
from ferret.apps.common.flow.models import (
    DURATION_MS_ROLE,
    FULL_URL_ROLE,
    HIGHLIGHT_ROLE,
    MIME_ROLE,
    SIZE_BYTES_ROLE,
    SORT_ROLE,
    STATUS_KIND_ROLE,
    FlowProxyModel,
    FlowTableModel,
    format_duration,
)
from ferret.core.mitm import MARKER_DEFAULT, FlowRow, build_flow_detail, flow_row


class _ListSource:
    """FlowSource 的最小替身：钉住 model 只依赖协议三方法（迭代/clear/remove）。

    #90 之后行集是 `FlowRow` 快照、remove 收 id 列表。
    """

    def __init__(self, rows: list) -> None:
        self.rows = list(rows)
        self.cleared = False
        self.removed: list = []

    def __iter__(self):
        return iter(self.rows)

    def clear(self) -> None:
        self.cleared = True
        self.rows.clear()

    def remove(self, flow_ids) -> None:
        self.removed.extend(flow_ids)


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

    @classmethod
    def completed_row(cls, **kwargs) -> FlowRow:
        return flow_row(cls.completed_flow(**kwargs))

    def model_with(self, *rows) -> FlowTableModel:
        model = FlowTableModel(None)  # type: ignore
        model._rows = list(rows)
        return model

    def test_set_source_seeds_rows_and_refresh_rebuilds(self) -> None:
        """set_source 入表即拉一次 source；refresh 重新迭代 source 重建行集。"""
        first = self.completed_row()
        source = _ListSource([first])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        self.assertEqual(model.rowCount(), 1)
        self.assertIs(model.get_row(0), first)

        source.rows.append(self.completed_row())
        model.handle_refresh()
        self.assertEqual(model.rowCount(), 2)

    def test_id_lookup_tracks_removal_refresh_and_append(self) -> None:
        """行按 flow.id 寻址：删除/刷新/追加后反查随行集走，同 id 不重复入表。"""
        first, second, third = [
            self.completed_row() for _ in range(3)
        ]
        source = _ListSource([first, second])
        model = FlowTableModel(self.app)
        model.set_source(source)
        model.handle_remove(first, 999)
        self.assertEqual(model._row_of(second), 0)
        self.assertEqual(model._row_of(first), -1)
        model.handle_add(third)
        model.handle_add(third)
        self.assertEqual(model._row_of(third), 1)
        self.assertEqual(model.rowCount(), 2)
        source.rows = [third, second]
        model.handle_refresh()
        self.assertEqual(model._row_of(third), 0)
        self.assertEqual(model._row_of(second), 1)
        model.clear_data()
        self.assertEqual(model._row_of(second), -1)

    def test_handle_update_replaces_the_stored_row(self) -> None:
        """更新按 id 整体替换快照：data() 随新快照走，行号不变。"""
        old = self.completed_row()
        model = FlowTableModel(self.app)
        model.set_source(_ListSource([old]))
        self.assertEqual(model.data(model.index(0, 4)), 200)

        newer_flow = self.completed_flow()
        newer_flow.response.status_code = 503
        newer = flow_row(newer_flow)
        # 行快照每次折叠都是新对象；只有 id 相同才认得是同一行。
        newer = replace(newer, id=old.id)
        model.handle_update(newer)

        self.assertEqual(model.rowCount(), 1)
        self.assertIs(model.get_row(0), newer)
        self.assertEqual(model.data(model.index(0, 4)), 503)

    def test_clear_data_and_remove_row_delegate_to_the_source(self) -> None:
        """clear/remove 只经 source 走 —— model 不再直连 View（AGENTS.md §3）。"""
        first, second = self.completed_row(), self.completed_row()
        source = _ListSource([first, second])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        model.remove_row(0)
        self.assertEqual(source.removed, [first.id])

        model.clear_data()
        self.assertTrue(source.cleared)
        self.assertEqual(model.rowCount(), 0)

    def test_remove_flows_delegates_the_whole_selection_in_one_call(self) -> None:
        """批量删除只调一次 source.remove（载荷是 id），UI 更新走桥接信号回路。"""
        first, second = self.completed_row(), self.completed_row()
        source = _ListSource([first, second])
        model = FlowTableModel(None)  # type: ignore
        model.set_source(source)

        model.remove_flows([first.id, second.id])
        self.assertEqual(source.removed, [first.id, second.id])

        # 空调用是空转，不得触碰 source
        model.remove_flows([])
        self.assertEqual(source.removed, [first.id, second.id])

    def test_handle_add_is_ignored_before_a_source_is_attached(self) -> None:
        model = FlowTableModel(None)  # type: ignore
        model.handle_add(self.completed_row())
        self.assertEqual(model.rowCount(), 0)

    def test_columns_and_semantic_roles(self) -> None:
        row = self.completed_row()
        model = self.model_with(row)

        self.assertEqual(
            model.HEADERS,
            ("#", "Mark", "Method", "URL", "Status", "Type", "Size", "Time"),
        )
        self.assertEqual(model.data(model.index(0, 3)), row.url)
        self.assertEqual(model.data(model.index(0, 4)), 200)
        self.assertEqual(model.data(model.index(0, 5)), "JSON")
        self.assertEqual(model.data(model.index(0, 6)), "11b")
        self.assertEqual(model.data(model.index(0, 7)), "128 ms")
        self.assertEqual(model.data(model.index(0, 4), STATUS_KIND_ROLE), "success")
        self.assertEqual(model.data(model.index(0, 3), FULL_URL_ROLE), row.url)
        self.assertEqual(model.data(model.index(0, 5), MIME_ROLE), "application/json")
        self.assertAlmostEqual(model.data(model.index(0, 7), DURATION_MS_ROLE), 128)
        self.assertEqual(model.data(model.index(0, 6), SIZE_BYTES_ROLE), 11)

    def test_highlight_role_reports_membership_for_every_column(self) -> None:
        """命中判定是整行的：HIGHLIGHT_ROLE 在任意列都回同一个真值，委托据此整行铺底。

        只查一份 id 集（O(1)）；放在类型分流之前，未命中行回 False（其 id 本就不
        在集里）。
        """
        hit, miss = self.completed_row(), self.completed_row()
        model = self.model_with(hit, miss)
        model.set_highlight_ids({hit.id})

        self.assertTrue(model.data(model.index(0, 0), HIGHLIGHT_ROLE))
        self.assertTrue(model.data(model.index(0, 3), HIGHLIGHT_ROLE))
        self.assertFalse(model.data(model.index(1, 0), HIGHLIGHT_ROLE))

    def test_set_highlight_ids_repaints_the_whole_table_once(self) -> None:
        """回推命中集只发一次全表 HIGHLIGHT_ROLE 的 dataChanged —— 只刷背景不动行集。"""
        rows = [self.completed_row() for _ in range(3)]
        model = self.model_with(*rows)
        seen: list = []
        model.dataChanged.connect(
            lambda tl, br, roles: seen.append((tl.row(), br.row(), list(roles)))
        )

        model.set_highlight_ids({rows[1].id})

        self.assertEqual(len(seen), 1)
        top, bottom, roles = seen[0]
        self.assertEqual((top, bottom), (0, 2))
        self.assertIn(HIGHLIGHT_ROLE, roles)

    def test_set_highlight_ids_short_circuits_on_an_unchanged_set(self) -> None:
        """集合相等就短路：直播重算时同一份命中集不该无谓刷屏。"""
        rows = [self.completed_row() for _ in range(2)]
        model = self.model_with(*rows)
        model.set_highlight_ids({rows[0].id})
        seen: list = []
        model.dataChanged.connect(lambda *args: seen.append(args))

        model.set_highlight_ids({rows[0].id})

        self.assertEqual(seen, [])

    def test_the_mark_column_sits_right_after_the_row_number(self) -> None:
        """表头文案「标记」与详情字段、筛选字段同名；列名 Mark 是取值不是文案。"""
        model = FlowTableModel(None)  # type: ignore
        self.assertEqual(model.HEADERS.index("Mark"), 1)
        self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal), "标记")

    def test_the_mark_column_renders_the_glyph_and_hides_the_shortcode(self) -> None:
        marked_flow = self.completed_flow()
        marked_flow.marked = ":bug:"
        marked, plain = flow_row(marked_flow), self.completed_row()
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
        marked_flow = self.completed_flow()
        marked_flow.marked = ":bug:"
        marked, plain = flow_row(marked_flow), self.completed_row()
        model = self.model_with(marked, plain)

        font = model.data(model.index(0, 1), Qt.ItemDataRole.FontRole)
        self.assertEqual(font, emoji_font(18))
        self.assertEqual(font.families()[0], "Segoe UI Emoji")
        self.assertIsNone(model.data(model.index(1, 1), Qt.ItemDataRole.FontRole))

    def test_the_mark_column_keeps_a_glyph_for_odd_markers(self) -> None:
        """`:default:`（旧开关写的值）查表就是 `"●"`；别人存的 .flow 里的野值不在
        字典里，走兜底 —— 两种都得有东西，不能空着也不能把短码原文铺进去。"""
        default_flow = self.completed_flow()
        default_flow.marked = MARKER_DEFAULT
        alien_flow = self.completed_flow()
        alien_flow.marked = ":no-such-emoji:"
        model = self.model_with(flow_row(default_flow), flow_row(alien_flow))
        self.assertEqual(model.data(model.index(0, 1)), marker_glyph(MARKER_DEFAULT))
        self.assertEqual(model.data(model.index(1, 1)), FALLBACK_GLYPH)

    def test_sorting_by_mark_separates_marked_from_unmarked(self) -> None:
        """SORT_ROLE 是短码字符串本身：空串与有值天然分堆，同类短码聚族。"""
        rows = [self.completed_row() for _ in range(4)]
        with_marked = []
        for index, shortcode in ((0, ":fire:"), (2, ":bug:")):
            flow = self.completed_flow()
            flow.marked = shortcode
            rows[index] = flow_row(flow)
        with_marked = rows
        model = self.model_with(*with_marked)
        self.assertEqual(model.data(model.index(0, 1), SORT_ROLE), ":fire:")
        self.assertEqual(model.data(model.index(1, 1), SORT_ROLE), "")

        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(1, Qt.SortOrder.DescendingOrder)
        marks = [proxy.data(proxy.index(row, 1), SORT_ROLE) for row in range(4)]
        self.assertEqual(marks, [":fire:", ":bug:", "", ""])

    def test_pending_and_error_states_keep_text(self) -> None:
        pending = flow_row(tflow.tflow())
        error = flow_row(tflow.tflow(err=True))
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
        slow = self.completed_row(duration_ms=1200)
        fast = self.completed_row(duration_ms=90)
        model = self.model_with(slow, fast)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(7, Qt.SortOrder.AscendingOrder)

        first = proxy.index(0, 7)
        self.assertAlmostEqual(first.data(DURATION_MS_ROLE), 90)
        self.assertIsInstance(first.data(SORT_ROLE), float)

    def test_proxy_row_numbers_stay_contiguous_after_sorting(self) -> None:
        rows = [self.completed_row(duration_ms=d) for d in (300, 100, 200)]
        model = self.model_with(*rows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(7, Qt.SortOrder.AscendingOrder)

        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [2, 3, 1],
        )

    def test_number_column_sorts_by_stable_sequence(self) -> None:
        rows = [self.completed_row(duration_ms=v) for v in (300, 100, 200)]
        model = self.model_with(*rows)
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
        row = flow_row(flow)
        model = self.model_with(row)
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

        表格这侧读快照折好的 `req_wire + resp_wire`（内核侧调用 wire_size），
        详情那侧在 `build_flow_detail`；同一条 gzip 响应两处不得差分毫。
        """
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"y" * 2048)
        model = self.model_with(flow_row(flow))
        data = build_flow_detail(flow)

        column = model.data(model.index(0, 6), SIZE_BYTES_ROLE)
        self.assertEqual(data["req_wire_size"] + data["res_wire_size"], column)

    def test_the_size_tooltip_states_the_caliber(self) -> None:
        """列宽只放得下一个总数，口径得靠 tooltip 说清。"""
        model = self.model_with(self.completed_row())
        tooltip = model.data(model.index(0, 6), Qt.ItemDataRole.ToolTipRole)

        self.assertIn("线上", tooltip)
        self.assertIn("请求", tooltip)
        self.assertIn("响应", tooltip)


if __name__ == "__main__":
    unittest.main()
