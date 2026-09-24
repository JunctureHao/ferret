"""流列表列布局的 Qt 行为（.plans/0-flow-list-columns.md §6.2）。

平铺表格与连接树共用同一份布局，两套视图都要覆盖：默认 8 列、显隐、重排（视觉
列序 + 无串列）、响应式按稳定 key、用户隐藏不被响应式复原、列宽按 key、Mark 固定宽、
高亮圆角按视觉首末列。持久化用 patch 隔离，不碰真实 config.json。
"""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow import views
from ferret.apps.common.flow.columns import (
    default_layout,
    logical_index,
    normalize,
)
from ferret.apps.common.flow.views import FlowViewerPane, _visual_caps


def _visual_order(header) -> list[str]:
    """表头当前视觉顺序 → 稳定 key 列表（隐藏列也算，只看排布）。"""
    from ferret.apps.common.flow.columns import DEFAULT_ORDER

    return [DEFAULT_ORDER[header.logicalIndex(v)] for v in range(header.count())]


def _visible_visual_order(header) -> list[str]:
    from ferret.apps.common.flow.columns import DEFAULT_ORDER

    return [
        DEFAULT_ORDER[header.logicalIndex(v)]
        for v in range(header.count())
        if not header.isSectionHidden(header.logicalIndex(v))
    ]


class ColumnLayoutQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 隔离持久化：init 读默认、变更写到 mock，不触真实 config.json。
        self._load_patch = patch.object(views, "load_layout", return_value=default_layout())
        self._save_patch = patch.object(views, "save_layout")
        self._load_patch.start()
        self.save_mock = self._save_patch.start()
        self.viewer = FlowViewerPane()
        self.viewer.resize(1200, 600)
        self.viewer.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.viewer.close()
        self.viewer.deleteLater()
        self.app.processEvents()
        self._load_patch.stop()
        self._save_patch.stop()

    @property
    def table_header(self):
        return self.viewer.table.horizontalHeader()

    @property
    def tree_header(self):
        return self.viewer.tree.header()

    def test_default_eight_columns_visible(self) -> None:
        for header in (self.table_header, self.tree_header):
            self.assertEqual(header.count(), 8)
            self.assertEqual(
                _visible_visual_order(header),
                ["index", "mark", "method", "url", "status", "type", "size", "time"],
            )

    def test_mark_is_fixed_width_mode(self) -> None:
        from PySide6.QtWidgets import QHeaderView

        mark_logical = logical_index("mark")
        for header in (self.table_header, self.tree_header):
            self.assertEqual(
                header.sectionResizeMode(mark_logical),
                QHeaderView.ResizeMode.Fixed,
            )

    def test_hide_column_reflects_in_both_views(self) -> None:
        layout = default_layout().with_visible("status", False)
        self.viewer.table._commit_column_layout(layout)
        self.app.processEvents()
        for view in (self.viewer.table, self.viewer.tree):
            self.assertTrue(view.isColumnHidden(logical_index("status")))
        # 未隐藏列仍在
        self.assertFalse(self.viewer.tree.isColumnHidden(logical_index("url")))

    def test_reorder_reflects_visual_order_both_views(self) -> None:
        order = ["index", "url", "time", "method", "mark", "status", "type", "size"]
        layout = default_layout().with_order(order)
        self.viewer.tree._commit_column_layout(layout)
        self.app.processEvents()
        self.assertEqual(_visual_order(self.table_header), order)
        self.assertEqual(_visual_order(self.tree_header), order)

    def test_reorder_does_not_swap_data_columns(self) -> None:
        # 逻辑列恒定：重排只动视觉位，url 的逻辑列永远是 3（数据不串列）。
        order = ["index", "url", "method", "mark", "status", "type", "size", "time"]
        layout = default_layout().with_order(order)
        self.viewer.table._commit_column_layout(layout)
        self.app.processEvents()
        # url 视觉位变 1，逻辑列仍 3
        self.assertEqual(self.table_header.visualIndex(logical_index("url")), 1)
        self.assertEqual(logical_index("url"), 3)

    def test_commit_persists_and_syncs_other_view(self) -> None:
        layout = default_layout().with_visible("size", False)
        self.viewer.table._commit_column_layout(layout)
        self.app.processEvents()
        self.save_mock.assert_called()
        # 另一视图（树）被同步
        self.assertTrue(self.viewer.tree.isColumnHidden(logical_index("size")))

    def test_responsive_hides_status_type_by_key(self) -> None:
        self.viewer.table._apply_responsive_columns(400)  # 窄
        self.assertTrue(self.viewer.table.isColumnHidden(logical_index("status")))
        self.assertTrue(self.viewer.table.isColumnHidden(logical_index("type")))
        # 非响应式列不受影响
        self.assertFalse(self.viewer.table.isColumnHidden(logical_index("url")))

    def test_responsive_restore_on_widen(self) -> None:
        self.viewer.table._apply_responsive_columns(400)
        self.viewer.table._apply_responsive_columns(1200)  # 宽
        self.assertFalse(self.viewer.table.isColumnHidden(logical_index("status")))
        self.assertFalse(self.viewer.table.isColumnHidden(logical_index("type")))

    def test_user_hidden_not_resurrected_by_widen(self) -> None:
        # 用户隐藏 status → 窄再宽后仍隐藏（用户可见性是权威，响应式只叠加）。
        layout = default_layout().with_visible("status", False)
        self.viewer.table.apply_column_layout(layout)
        self.viewer.table._apply_responsive_columns(400)
        self.viewer.table._apply_responsive_columns(1200)
        self.assertTrue(self.viewer.table.isColumnHidden(logical_index("status")))

    def test_responsive_does_not_persist(self) -> None:
        # 响应式隐藏绝不回写配置（§4.2）。
        self.save_mock.reset_mock()
        self.viewer.table._apply_responsive_columns(400)
        self.app.processEvents()
        self.save_mock.assert_not_called()

    def test_width_resize_emits_by_key(self) -> None:
        received: list = []
        self.viewer.table.column_layout_changed.connect(received.append)
        # 模拟用户把 url（逻辑列 3）拖到 300
        self.viewer.table._on_section_resized(logical_index("url"), 420, 300)
        self.assertTrue(received)
        self.assertEqual(received[-1].width("url"), 300)

    def test_width_resize_ignores_mark_fixed(self) -> None:
        received: list = []
        self.viewer.table.column_layout_changed.connect(received.append)
        self.viewer.table._on_section_resized(logical_index("mark"), 64, 120)
        self.assertFalse(received)  # 固定宽列不记

    def test_width_resize_suppressed_while_applying(self) -> None:
        received: list = []
        self.viewer.table.column_layout_changed.connect(received.append)
        # 程序化应用布局：闸门短路，setColumnWidth 触发的 sectionResized 不回写。
        self.viewer.table.apply_column_layout(default_layout().with_width("url", 250))
        self.app.processEvents()
        self.assertFalse(received)

    def test_visual_caps_after_reorder(self) -> None:
        order = ["index", "url", "time", "method", "mark", "status", "type", "size"]
        layout = default_layout().with_order(order)
        self.viewer.table._commit_column_layout(layout)
        self.app.processEvents()
        header = self.table_header
        # index 仍视觉首（pinned），size 视觉末
        first_first, first_last = _visual_caps(header, logical_index("index"))
        self.assertTrue(first_first)
        self.assertFalse(first_last)
        last_first, last_last = _visual_caps(header, logical_index("size"))
        self.assertFalse(last_first)
        self.assertTrue(last_last)

    def test_visual_caps_last_skips_hidden(self) -> None:
        # 末列 time 隐藏后，视觉末应落到 size（隐藏列不计）。
        layout = normalize(
            {
                **default_layout().to_dict(),
                "visible": ["index", "mark", "method", "url", "status", "type", "size"],
            }
        )
        self.viewer.table._commit_column_layout(layout)
        self.app.processEvents()
        header = self.table_header
        self.assertTrue(_visual_caps(header, logical_index("size"))[1])
        self.assertFalse(_visual_caps(header, logical_index("time"))[1])


class ColumnSettingsDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, layout):
        from PySide6.QtWidgets import QWidget

        from ferret.apps.common.flow.column_settings import ColumnSettingsDialog

        self._parent = QWidget()
        return ColumnSettingsDialog(layout, self._parent)

    def test_lists_all_columns_in_order(self) -> None:
        from PySide6.QtCore import Qt

        dlg = self._dialog(default_layout())
        keys = [
            dlg.list_widget.item(r).data(Qt.ItemDataRole.UserRole)
            for r in range(dlg.list_widget.count())
        ]
        self.assertEqual(
            keys,
            ["index", "mark", "method", "url", "status", "type", "size", "time"],
        )

    def test_result_reflects_unchecked_optional(self) -> None:
        from PySide6.QtCore import Qt

        dlg = self._dialog(default_layout())
        # 找到 status 项取消勾选
        for r in range(dlg.list_widget.count()):
            item = dlg.list_widget.item(r)
            if item.data(Qt.ItemDataRole.UserRole) == "status":
                item.setCheckState(Qt.CheckState.Unchecked)
        result = dlg.result_layout()
        self.assertFalse(result.is_visible("status"))
        self.assertTrue(result.is_visible("url"))

    def test_result_normalizes_required_visible(self) -> None:
        # 即使 UI 里没能勾必需列，归一化也会强制其可见（对话框不必自守）。
        dlg = self._dialog(default_layout())
        result = dlg.result_layout()
        for key in ("index", "method", "url"):
            self.assertTrue(result.is_visible(key))

    def test_reset_restores_default(self) -> None:
        from PySide6.QtCore import Qt

        layout = default_layout().with_visible("status", False)
        dlg = self._dialog(layout)
        dlg._reset()
        # 恢复默认后 status 复选回勾
        found = False
        for r in range(dlg.list_widget.count()):
            item = dlg.list_widget.item(r)
            if item.data(Qt.ItemDataRole.UserRole) == "status":
                found = True
                self.assertEqual(item.checkState(), Qt.CheckState.Checked)
        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
