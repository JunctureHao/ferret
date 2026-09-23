import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.models import (
    HIGHLIGHT_ROLE,
    SORT_ROLE,
    FlowConnProxyModel,
    FlowConnTreeModel,
)


class _ListSource:
    """FlowSource 最小替身：只暴露协议三方法（迭代 / clear / remove）。"""

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


class FlowConnTreeModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def flow_on(conn_id, *, host="api.example.com", duration_ms=100):
        flow = tflow.tflow(resp=True)
        flow.client_conn.id = conn_id
        flow.client_conn.peername = ("192.168.1.10", 52341)
        flow.request.host = host
        flow.request.port = 443
        flow.request.timestamp_start = 100.0
        flow.response.timestamp_end = 100.0 + duration_ms / 1000  # type: ignore
        flow.request.raw_content = b"req"
        flow.response.raw_content = b"response"  # type: ignore
        return flow

    def model_with(self, *flows) -> FlowConnTreeModel:
        model = FlowConnTreeModel(None)  # type: ignore
        model.set_source(_ListSource(list(flows)))
        return model

    # ------------------------------------------------------------------
    # 分组
    # ------------------------------------------------------------------
    def test_flows_on_one_connection_collapse_into_a_single_node(self) -> None:
        a1 = self.flow_on("conn-A")
        a2 = self.flow_on("conn-A")
        b1 = self.flow_on("conn-B")
        model = self.model_with(a1, a2, b1)

        self.assertEqual(model.rowCount(), 2)  # 两个连接节点
        node_a = model.index(0, 0, QModelIndex())
        self.assertEqual(model.rowCount(node_a), 2)  # conn-A 两条子流
        node_b = model.index(1, 0, QModelIndex())
        self.assertEqual(model.rowCount(node_b), 1)

    def test_missing_conn_id_falls_into_a_single_unknown_group(self) -> None:
        one = self.flow_on("")
        two = self.flow_on("")
        one.client_conn.id = ""
        two.client_conn.id = ""
        model = self.model_with(one, two)
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.rowCount(model.index(0, 0, QModelIndex())), 2)

    def test_child_count_reports_visible_flows_not_nodes(self) -> None:
        model = self.model_with(
            self.flow_on("conn-A"), self.flow_on("conn-A"), self.flow_on("conn-B")
        )
        self.assertEqual(model.child_count(), 3)
        self.assertEqual(model.rowCount(), 2)

    # ------------------------------------------------------------------
    # 聚合列
    # ------------------------------------------------------------------
    def test_parent_node_aggregates_count_size_and_transport(self) -> None:
        model = self.model_with(self.flow_on("conn-A"), self.flow_on("conn-A"))
        node = model.index(0, 0, QModelIndex())

        # `#` 列 = 子流计数
        self.assertEqual(model.data(model.index(0, 0, QModelIndex())), 2)
        # Type 列 = 传输协议（alpn 解出）
        self.assertEqual(model.data(model.index(0, 5, QModelIndex())), "http/1.1")
        # Size 列非空
        self.assertTrue(model.data(model.index(0, 6, QModelIndex())))
        # 父节点粗体
        font = model.data(node, Qt.ItemDataRole.FontRole)
        self.assertTrue(font.bold())

    def test_single_host_label_shows_the_target(self) -> None:
        model = self.model_with(self.flow_on("conn-A", host="api.example.com"))
        label = model.data(model.index(0, 3, QModelIndex()))
        self.assertIn("192.168.1.10:52341", label)
        self.assertIn("api.example.com:443", label)

    def test_multi_host_label_reports_target_count(self) -> None:
        model = self.model_with(
            self.flow_on("conn-A", host="api.example.com"),
            self.flow_on("conn-A", host="cdn.example.com"),
        )
        label = model.data(model.index(0, 3, QModelIndex()))
        self.assertIn("2", label)
        self.assertNotIn("api.example.com", label)

    def test_child_number_column_is_group_local(self) -> None:
        model = self.model_with(self.flow_on("conn-A"), self.flow_on("conn-A"))
        parent = model.index(0, 0, QModelIndex())
        self.assertEqual(model.data(model.index(0, 0, parent)), 1)
        self.assertEqual(model.data(model.index(1, 0, parent)), 2)

    # ------------------------------------------------------------------
    # 增量
    # ------------------------------------------------------------------
    def test_handle_add_appends_child_under_existing_connection(self) -> None:
        first = self.flow_on("conn-A")
        model = self.model_with(first)
        model.handle_add(self.flow_on("conn-A"))
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.rowCount(model.index(0, 0, QModelIndex())), 2)

    def test_handle_add_opens_a_new_top_level_node(self) -> None:
        model = self.model_with(self.flow_on("conn-A"))
        model.handle_add(self.flow_on("conn-B"))
        self.assertEqual(model.rowCount(), 2)

    def test_handle_add_dedupes_by_flow_id(self) -> None:
        first = self.flow_on("conn-A")
        model = self.model_with(first)
        model.handle_add(first)
        self.assertEqual(model.rowCount(model.index(0, 0, QModelIndex())), 1)

    def test_handle_update_emits_datachanged_without_reset(self) -> None:
        first = self.flow_on("conn-A")
        model = self.model_with(first)
        resets: list = []
        changes: list = []
        model.modelReset.connect(lambda: resets.append(True))
        model.dataChanged.connect(lambda *a: changes.append(a))

        model.handle_update(first)

        self.assertEqual(resets, [])  # 绝不整树 reset（展开状态保持）
        self.assertTrue(changes)

    def test_handle_remove_drops_child_then_empties_node(self) -> None:
        a1 = self.flow_on("conn-A")
        a2 = self.flow_on("conn-A")
        model = self.model_with(a1, a2)

        model.handle_remove(a1, 0)
        self.assertEqual(model.rowCount(model.index(0, 0, QModelIndex())), 1)

        model.handle_remove(a2, 0)
        self.assertEqual(model.rowCount(), 0)  # 空节点连父摘除

    # ------------------------------------------------------------------
    # 排序（首见序 vs 用户聚合）
    # ------------------------------------------------------------------
    def test_default_order_is_first_seen_and_stable(self) -> None:
        model = self.model_with(
            self.flow_on("conn-A", duration_ms=900),
            self.flow_on("conn-B", duration_ms=100),
        )
        proxy = FlowConnProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(7, Qt.SortOrder.AscendingOrder)  # 未 mark：应无视聚合值

        # 顶层仍按 append 序（conn-A 在前），不随 Time 聚合抖动
        first_label = proxy.data(proxy.index(0, 3, QModelIndex()))
        self.assertIn("192.168.1.10", first_label)
        self.assertEqual(proxy.data(proxy.index(0, 0, QModelIndex())), 1)

    def test_user_sort_switches_to_aggregate_order(self) -> None:
        model = self.model_with(
            self.flow_on("conn-A", duration_ms=900),
            self.flow_on("conn-B", duration_ms=100),
        )
        proxy = FlowConnProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.mark_user_sorted()
        proxy.sort(7, Qt.SortOrder.AscendingOrder)  # 按 Time 升序聚合

        # conn-B（末端更早）应排到前面
        top_size_sort = proxy.data(proxy.index(0, 7, QModelIndex()), SORT_ROLE)
        bottom_size_sort = proxy.data(proxy.index(1, 7, QModelIndex()), SORT_ROLE)
        self.assertLessEqual(top_size_sort, bottom_size_sort)

    # ------------------------------------------------------------------
    # 高亮
    # ------------------------------------------------------------------
    def test_highlight_marks_matching_child_rows(self) -> None:
        hit = self.flow_on("conn-A")
        miss = self.flow_on("conn-A")
        model = self.model_with(hit, miss)
        model.set_highlight_ids({hit.id})

        parent = model.index(0, 0, QModelIndex())
        self.assertTrue(model.data(model.index(0, 0, parent), HIGHLIGHT_ROLE))
        self.assertFalse(model.data(model.index(1, 0, parent), HIGHLIGHT_ROLE))
        # 父节点第一版不染色
        self.assertFalse(model.data(parent, HIGHLIGHT_ROLE))

    def test_flows_under_expands_parent_to_children(self) -> None:
        a1 = self.flow_on("conn-A")
        a2 = self.flow_on("conn-A")
        model = self.model_with(a1, a2)
        parent = model.index(0, 0, QModelIndex())
        self.assertEqual(model.flows_under(parent), [a1, a2])
        child = model.index(0, 0, parent)
        self.assertEqual(model.flows_under(child), [a1])

    def test_connection_detail_summarises_the_node(self) -> None:
        model = self.model_with(self.flow_on("conn-A"), self.flow_on("conn-A"))
        node = model.node_at(model.index(0, 0, QModelIndex()))
        assert node is not None
        detail = model.connection_detail(node)
        self.assertEqual(detail["kind"], "connection")
        self.assertEqual(detail["flow_count"], 2)
        self.assertEqual(detail["client"], "192.168.1.10:52341")
        self.assertEqual(detail["transport"], "http/1.1")


if __name__ == "__main__":
    unittest.main()
