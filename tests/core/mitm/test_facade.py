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

from ferret.core.mitm import MitmFacade, MitmRuntime, View, WsClose
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


class FlowDetailTests(unittest.TestCase):
    """详情字典必须**穿过 `runtime.call`** 才交给界面（AGENTS.md §3）。

    界面早先自己拿着活 flow 现算（`FlowTableModel._build_row_data`），选一行就在 Qt
    线程上读一遍正在被 mitm 改写的对象。这里钉的是「走没走 `call`」这条路，不是字典
    内容 —— 内容由 `tests/core/mitm/test_detail.py` 覆盖。
    """

    def setUp(self) -> None:
        self.runtime = _InlineRuntime()
        self.facade = MitmFacade(self.runtime)  # type: ignore
        self.flow = tflow.tflow(resp=True)
        self.runtime.view.add([self.flow])

    def test_the_detail_is_built_through_the_runtime(self) -> None:
        calls: list[str] = []
        inner = self.runtime.call

        def spy(callback, *, timeout: float = 5.0):
            calls.append("call")
            return inner(callback, timeout=timeout)

        self.runtime.call = spy  # type: ignore
        data = self.facade.flow_detail(self.flow.id)

        self.assertEqual(calls, ["call"])
        self.assertEqual(data["id"], self.flow.id)
        self.assertEqual(data["state"], "complete")

    def test_an_unknown_id_yields_an_empty_dict(self) -> None:
        """选中行和内核删流量能抢在一起 —— 空字典让面板清空，而不是抛异常。"""
        self.assertEqual(self.facade.flow_detail("nope"), {})

    def test_a_non_http_flow_yields_an_empty_dict(self) -> None:
        """View 里也躺着 tcp/udp 流量，详情面板目前只认 HTTP。"""
        tcp = tflow.ttcpflow()
        self.runtime.view.add([tcp])
        self.assertEqual(self.facade.flow_detail(tcp.id), {})

    def test_a_stopped_kernel_still_answers(self) -> None:
        """没跑内核就没有事件循环 —— 此时就地构建，不能因为 `call` 抛错而空着。"""
        facade = MitmFacade(MitmRuntime())
        facade.view.add([self.flow])
        self.assertEqual(facade.flow_detail(self.flow.id)["id"], self.flow.id)


class WebsocketReadTests(unittest.TestCase):
    """取帧刻意**不**走 `_snapshot()`，所以得单独钉一遍它守住了什么。

    `flow.copy()` 会把每条消息连内容一起深拷一遍 —— 上千帧的行情连接白拷一份，还是
    为了马上丢掉。这里换成在 mitm 线程上就地摘成值对象，代价是「跨线程该守的」不再由
    `copy()` 顺带保证，只能由测试盯着：出来的东西不带 flow 引用、原 flow 之后被改也
    不影响已经交出去的帧。
    """

    def setUp(self) -> None:
        self.runtime = _InlineRuntime()
        self.facade = MitmFacade(self.runtime)  # type: ignore
        # 原生工厂的 `close_reason` 默认是空串（会盖掉 `twebsocket()` 里那个值），
        # 这里显式给一个，好让关闭原因这条路真的被走到。
        self.flow = tflow.twebsocketflow(close_reason="Close Reason")
        self.runtime.view.add([self.flow])

    def test_frames_come_back_in_order(self) -> None:
        frames = self.facade.websocket_frames(self.flow.id)
        self.assertEqual([f.index for f in frames], [0, 1, 2])
        self.assertEqual(frames[-1].content, b"it's me")

    def test_the_frames_are_read_through_the_runtime(self) -> None:
        """内核跑着时必须借 mitm 线程读 —— Qt 线程不许碰活 flow（AGENTS.md §3）。"""
        calls: list[str] = []
        inner = self.runtime.call

        def spy(callback, *, timeout: float = 5.0):
            calls.append("call")
            return inner(callback, timeout=timeout)

        self.runtime.call = spy  # type: ignore
        self.facade.websocket_frames(self.flow.id)
        self.facade.websocket_close(self.flow.id)
        self.assertEqual(calls, ["call", "call"])

    def test_frames_do_not_track_later_edits(self) -> None:
        """不走 `copy()` 换来的那条保证，在这里兑现。"""
        frames = self.facade.websocket_frames(self.flow.id)
        assert self.flow.websocket is not None
        self.flow.websocket.messages[-1].content = b"tampered"
        self.assertEqual(frames[-1].content, b"it's me")

    def test_close_info_comes_back_whole(self) -> None:
        info = self.facade.websocket_close(self.flow.id)
        self.assertEqual(info.close_code, 1000)
        self.assertEqual(info.close_reason, "Close Reason")
        self.assertTrue(info.is_closed)

    def test_a_plain_http_flow_has_no_frames(self) -> None:
        """`~http` 底座下两种流量同列，选中普通请求时消息页得干净地空着。"""
        plain = tflow.tflow(resp=True)
        self.runtime.view.add([plain])
        self.assertEqual(self.facade.websocket_frames(plain.id), [])
        self.assertFalse(self.facade.websocket_close(plain.id).is_closed)

    def test_an_unknown_id_yields_nothing(self) -> None:
        """选中行和内核删流量会抢在一起，和 `flow_detail` 同一个道理。"""
        self.assertEqual(self.facade.websocket_frames("nope"), [])
        self.assertEqual(self.facade.websocket_close("nope"), WsClose())

    def test_a_non_http_flow_yields_nothing(self) -> None:
        tcp = tflow.ttcpflow()
        self.runtime.view.add([tcp])
        self.assertEqual(self.facade.websocket_frames(tcp.id), [])
        self.assertEqual(self.facade.websocket_close(tcp.id), WsClose())

    def test_a_stopped_kernel_still_answers(self) -> None:
        """会话页那条路：flow 是从文件读回来的，本来就没有 mitm 线程。"""
        facade = MitmFacade(MitmRuntime())
        facade.view.add([self.flow])
        self.assertEqual(len(facade.websocket_frames(self.flow.id)), 3)
        self.assertEqual(facade.websocket_close(self.flow.id).close_code, 1000)


if __name__ == "__main__":
    unittest.main()
