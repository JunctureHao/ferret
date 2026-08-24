"""`MitmFacade` 的两条只在真跑一遍代理时才露头的契约。

一、**快照必须留住原来的 `flow.id`**；二、**放行要如实报数**。

原生 `Serializable.copy()` 会给副本换一个新 uuid。这件事只在真跑一遍代理时才露头
——界面从快照上读到 id，转手交给 `release_flows` / `apply_request_edits`，那边
`View.get_by_id` 一律落空，报的却是「这条流量已不在列表中」，看着像流量自己没了。

两处都刻意不起 asyncio 线程：快照那几个 lambda 在 runtime 没跑时就地执行，放行那几段
则拿 `_InlineRuntime` 把 `call` 就地跑掉（`Flow.intercept()` / `resume()` / `kill()`
原生都不需要事件循环）。（`save_flows` 反过来——它只在内核跑着时才拍快照，所以没法
在这里覆盖，那条路由端到端冒烟兜着。）
"""

import unittest

from mitmproxy.test import tflow

from ferret.core.mitm import MitmFacade, MitmRuntime, View
from ferret.core.mitm.addons import GatewayState
from ferret.core.mitm.intercept import InterceptState


class SnapshotIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facade = MitmFacade(MitmRuntime())
        self.flow = tflow.tflow(resp=True)
        self.facade.view.add([self.flow])

    def test_all_flows_keep_their_id(self) -> None:
        snapshot = self.facade.all_http_flows()
        self.assertEqual([f.id for f in snapshot], [self.flow.id])

    def test_intercepted_flows_keep_their_id(self) -> None:
        """断点页写回的唯一凭据。id 一换，改和放行全都找不到人。"""
        self.flow.intercept()
        snapshot = self.facade.intercepted_flows()
        self.assertEqual([f.id for f in snapshot], [self.flow.id])

    def test_a_snapshot_id_round_trips_through_get_by_id(self) -> None:
        """直接钉住那条断掉的链路：写回时就是拿这个 id 去 View 里找原来那条流量。"""
        self.flow.intercept()
        held = self.facade.intercepted_flows()[0]
        self.assertIs(self.facade.view.get_by_id(held.id), self.flow)

    def test_the_snapshot_is_still_a_copy(self) -> None:
        """留 id 不等于把活 flow 递出去 —— Qt 线程只许读副本（AGENTS.md §3）。"""
        snapshot = self.facade.all_http_flows()[0]
        self.assertIsNot(snapshot, self.flow)
        self.assertIsNot(snapshot.request, self.flow.request)
        snapshot.request.path = "/tampered"
        self.assertNotEqual(self.flow.request.path, "/tampered")


class _FakeMaster:
    """只有两本账的 master：`_resume` 用得到的就这两个属性。"""

    def __init__(self) -> None:
        self.gateway = GatewayState()
        self.intercept_state = InterceptState()


class _InlineRuntime:
    """内核「在跑」但 `call` 就地执行 —— 不起 asyncio 线程也能覆盖放行那几段。

    `Flow.intercept()` / `resume()` / `kill()` 都不需要事件循环（原生只翻标志位加
    一个 `Event`），所以这样是够的。
    """

    def __init__(self) -> None:
        self.view = View()
        self.master = _FakeMaster()
        self.is_running = True

    def call(self, callback, *, timeout: float = 5.0):
        return callback()


class ResumeCountTests(unittest.TestCase):
    """放行的返回值是界面的成功判据（`InterceptController._resume` 拿它发提示）。

    坑在于：两本账 `release` 完，流量已经不是 `intercepted` 了，兜底那趟 `_sweep`
    必然数出 0。只看兜底 = 每次成功放行都报「没放到人」，用户点了没有任何反馈。
    """

    def setUp(self) -> None:
        self.runtime = _InlineRuntime()
        self.facade = MitmFacade(self.runtime)  # type: ignore
        self.flow = tflow.tflow()
        self.runtime.view.add([self.flow])

    def test_releasing_a_flow_on_the_intercept_ledger_reports_one(self) -> None:
        self.runtime.master.intercept_state.arm(self.flow)
        self.assertEqual(self.facade.release_flows([self.flow.id]), 1)
        self.assertFalse(self.flow.intercepted)

    def test_dropping_a_flow_on_the_intercept_ledger_reports_one(self) -> None:
        self.runtime.master.intercept_state.arm(self.flow)
        self.assertEqual(self.facade.drop_flows([self.flow.id]), 1)

    def test_release_all_reports_what_the_ledgers_let_go(self) -> None:
        self.runtime.master.intercept_state.arm(self.flow)
        self.assertEqual(self.facade.release_all_intercepted(), 1)
        self.assertFalse(self.flow.intercepted)

    def test_a_flow_nobody_owns_is_still_swept_and_counted(self) -> None:
        """原生 addon 自己拦下的：两本账都不认，只能靠兜底那趟扫出来。"""
        self.flow.intercept()
        self.assertEqual(self.facade.release_flows([self.flow.id]), 1)
        self.assertFalse(self.flow.intercepted)

    def test_a_flow_is_never_counted_twice(self) -> None:
        """账上放掉的已经不 `intercepted` 了，兜底那趟不会再数它一遍。"""
        self.runtime.master.intercept_state.arm(self.flow)
        other = tflow.tflow()
        self.runtime.view.add([other])
        other.intercept()
        self.assertEqual(self.facade.release_flows([self.flow.id, other.id]), 2)

    def test_an_unknown_id_counts_for_nothing(self) -> None:
        self.assertEqual(self.facade.release_flows(["nope"]), 0)


if __name__ == "__main__":
    unittest.main()
