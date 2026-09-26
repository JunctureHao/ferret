"""Mock 响应池（原生 ServerPlayback，.plans/0-server-playback.md）。

三层各钉各的契约：装配（master 持有实例并在链上）、行为（命中注入 / 未命中
策略 / 消耗与复用 / 哈希粒度热更）、facade（池副本的线程边界与托管文件回读）。
不起新的 asyncio 线程：facade 的「内核没跑就走调用线程」两路姿态让池操作可以
就地验证（同 test_facade.py 的手法）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.serverplayback import ServerPlayback
from mitmproxy.test import tflow

from ferret.core.mitm import CaptureMaster, FlowFile, HTTPFlow, MitmFacade, MitmRuntime
from ferret.core.mitm.addons import GatewayL7Addon


def _mock_pool_file(temp_dir: str) -> Any:
    """把 facade 的池文件指到临时目录，单元测试不写用户配置目录。"""
    return mock.patch(
        "ferret.core.mitm.facade.get_mock_pool_file",
        return_value=Path(temp_dir) / "mock_pool.flow",
    )


class ServerPlaybackAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = __import__("asyncio").new_event_loop()
        self.master = CaptureMaster(event_loop=self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_master_holds_server_playback_instance(self) -> None:
        self.assertIsInstance(self.master.server_playback, ServerPlayback)

    def test_server_playback_registered_in_addons_chain(self) -> None:
        self.assertIn(self.master.server_playback, self.master.addons.chain)

    def test_server_playback_sits_after_gateway_and_scripts(self) -> None:
        """链位契约（D3）：网关 L7 → scripts → server_playback → intercept。

        绕行截断在前（AddonHalt），断点在后（整链派发完才 wait_for_resume，
        断点看到的流已带上 mock 响应）—— 链上顺序就是这两条语义的载体。
        """
        chain = self.master.addons.chain
        l7 = next(a for a in chain if isinstance(a, GatewayL7Addon))
        self.assertLess(chain.index(l7), chain.index(self.master.server_playback))
        self.assertLess(
            chain.index(self.master.scripts),
            chain.index(self.master.server_playback),
        )
        self.assertLess(
            chain.index(self.master.server_playback),
            chain.index(self.master.intercept),
        )


class ServerPlaybackBehaviorTests(unittest.TestCase):
    """原生 addon 的行为契约。per-test 独立 Master：ctx.options 指向最后构造的
    Master，穿插构造会让 _hash 读到别家的选项。"""

    def setUp(self) -> None:
        import asyncio

        self.loop = asyncio.new_event_loop()
        self.master = CaptureMaster(event_loop=self.loop)
        self.addon = self.master.server_playback
        self.recorded = tflow.tflow(resp=True)

    def tearDown(self) -> None:
        self.loop.close()

    def _fired(self) -> HTTPFlow:
        """与 `self.recorded` 同请求的一条「刚抓到的」流（无响应）。"""
        target = tflow.tflow()
        target.request.content = self.recorded.request.content
        return target

    def _stranger(self) -> HTTPFlow:
        """请求对不上池里任何一条的流（主机不同 → 哈希不同）。"""
        target = self._fired()
        target.request.host = "unmatched.example"
        return target

    def test_empty_map_is_noop(self) -> None:
        target = self._fired()
        self.addon.request(target)
        self.assertIsNone(target.response)

    def test_matching_request_gets_recorded_response(self) -> None:
        self.addon.load_flows([self.recorded])
        target = self._fired()
        self.addon.request(target)
        assert target.response is not None
        assert self.recorded.response is not None
        self.assertEqual(target.response.content, self.recorded.response.content)
        self.assertEqual(target.is_replay, "response")

    def test_kill_extra(self) -> None:
        """未命中策略只作用于**没匹配上**的请求：命中池的照常回录好的响应。
        顺序不能反 —— 命中那条会把消耗式池打空，`if self.flowmap:` 门闩
        直接让后续请求全部直连。"""
        self.master.options.update(server_replay_extra="kill")
        self.addon.load_flows([self.recorded])
        stranger = self._stranger()
        self.addon.request(stranger)
        self.assertIsNotNone(stranger.error)
        matched = self._fired()
        self.addon.request(matched)
        self.assertIsNotNone(matched.response)

    def test_fixed_status_extra(self) -> None:
        self.master.options.update(server_replay_extra="404")
        self.addon.load_flows([self.recorded])
        stranger = self._stranger()
        self.addon.request(stranger)
        assert stranger.response is not None
        self.assertEqual(stranger.response.status_code, 404)

    def test_reuse_false_drains_and_falls_back_to_forward(self) -> None:
        """原生出厂是消耗式：池耗尽后 flowmap 变空，未命中策略跟着失效 ——
        流量静默转直连。这是 GUI 侧默认 reuse=True 的全部动机（D5）。"""
        self.addon.load_flows([self.recorded])
        first = self._fired()
        self.addon.request(first)
        self.assertIsNotNone(first.response)
        second = self._fired()
        self.addon.request(second)
        self.assertIsNone(second.response)

    def test_reuse_true_serves_repeatedly(self) -> None:
        self.master.options.update(server_replay_reuse=True)
        self.addon.load_flows([self.recorded])
        for _ in range(3):
            target = self._fired()
            self.addon.request(target)
            self.assertIsNotNone(target.response)

    def test_ignore_host_loosens_matching(self) -> None:
        """哈希粒度选项热更：configure 自动 recompute_hashes，换主机照样命中。"""
        self.master.options.update(server_replay_ignore_host=True)
        self.addon.load_flows([self.recorded])
        target = self._fired()
        target.request.host = "elsewhere.example"
        self.addon.request(target)
        self.assertIsNotNone(target.response)

    def test_responseless_pool_entry_serves_nothing(self) -> None:
        """原生 add_flows 不筛无响应流，但 next_flow 会跳过它们 —— 无响应源流
        回不了任何东西（facade 在入池前就过滤，见 MockFacadeTests）。"""
        self.addon.load_flows([tflow.tflow()])
        target = self._fired()
        self.addon.request(target)
        self.assertIsNone(target.response)

    def test_clear_stops_mocking(self) -> None:
        self.addon.load_flows([self.recorded])
        self.addon.clear()
        self.assertEqual(self.addon.count(), 0)
        target = self._fired()
        self.addon.request(target)
        self.assertIsNone(target.response)


class MockFacadeTests(unittest.TestCase):
    """池的 facade 契约。不内核起跑：add（要做活副本）必须报「内核未运行」，
    删减/快照/导出走死对象路径就地验证（与会话页加载 .flow 同一等级）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = _mock_pool_file(self._tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.facade = MitmFacade(MitmRuntime())
        self.recorded = tflow.tflow(resp=True)

    def test_add_requires_running_kernel(self) -> None:
        with self.assertRaises(RuntimeError):
            self.facade.add_mock_flows([self.recorded.id])

    def test_remove_clear_snapshot_roundtrip(self) -> None:
        self.facade.runtime.mock_pool = [self.recorded]
        pool_file = Path(self._tmp.name) / "mock_pool.flow"

        removed = self.facade.remove_mock_flows(["no-such-id"])
        self.assertEqual(removed, 0)

        removed = self.facade.remove_mock_flows([self.recorded.id])
        self.assertEqual(removed, 1)
        self.assertEqual(self.facade.runtime.mock_pool, [])
        # 删减即落盘：托管文件里已没有那条流。
        self.assertEqual(FlowFile.read(pool_file), [])

        snapshot = self.facade.mock_snapshot()
        self.assertEqual(snapshot["count"], 0)

    def test_snapshot_carries_display_fields(self) -> None:
        self.facade.runtime.mock_pool = [self.recorded]
        snapshot = self.facade.mock_snapshot()
        entry = snapshot["entries"][0]
        self.assertEqual(entry["id"], self.recorded.id)
        self.assertEqual(entry["method"], "GET")
        self.assertEqual(entry["status"], 200)
        self.assertTrue(entry["url"].startswith("http://"))
        self.assertTrue(entry["size"])
        self.assertFalse(snapshot["enabled"])

    def test_pool_file_reload_on_construction(self) -> None:
        pool_file = Path(self._tmp.name) / "mock_pool.flow"
        FlowFile.write(pool_file, [self.recorded])
        facade = MitmFacade(MitmRuntime())
        self.assertEqual([f.id for f in facade.runtime.mock_pool], [self.recorded.id])

    def test_export_pool(self) -> None:
        self.facade.runtime.mock_pool = [self.recorded]
        out = Path(self._tmp.name) / "export.flow"
        self.assertEqual(self.facade.export_mock_pool(out), 1)
        loaded = FlowFile.read(out)
        self.assertEqual([f.id for f in loaded], [self.recorded.id])

    def test_runtime_state_defaults_and_apply(self) -> None:
        runtime = self.facade.runtime
        self.assertFalse(runtime.mock_enabled)
        self.assertEqual(runtime.mock_knobs, {})
        runtime.apply_mock_enabled(True)
        self.assertTrue(runtime.mock_enabled)
        runtime.apply_mock_knobs({"server_replay_extra": "404"})
        self.assertEqual(runtime.mock_knobs["server_replay_extra"], "404")
        runtime.apply_mock_knobs({"server_replay_reuse": False})
        self.assertEqual(
            runtime.mock_knobs,
            {"server_replay_extra": "404", "server_replay_reuse": False},
        )


if __name__ == "__main__":
    unittest.main()
