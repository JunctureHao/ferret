from __future__ import annotations

import asyncio
import logging
import os
import socket
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.proxy import mode_specs
from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, MitmRuntimeState, ScriptEntry
from ferret.core.mitm.bindings import LocalRedirectorInstance, MitmLogHandler
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.modes import REVERSE_DEFAULT_PORT, SOCKS5_DEFAULT_PORT
from ferret.core.mitm.runtime import UiBridgeAddon, _MitmThread
from ferret.core.network import ANY_HOST, LOOPBACK_HOST

from ._qt import start_runtime, wait_for_signal, wait_ready, wait_until


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MitmRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_runtime_starts_and_stops_one_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        start_runtime(runtime)
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)
        self.assertTrue(runtime.stop())
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)

    def test_occupied_port_fails_without_entering_running(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            runtime = MitmRuntime(listen_port=blocker.getsockname()[1])
            runtime.start()

            values = wait_for_signal(runtime.failed)
            self.assertTrue(values)
            self.assertIn("已被占用", values[0][0])
            self.assertEqual(runtime.state, MitmRuntimeState.FAILED)
            self.assertTrue(runtime.stop())

    def test_immediate_stop_does_not_leave_background_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        runtime.start()

        self.assertTrue(runtime.stop())
        QCoreApplication.processEvents()
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)
        self.assertIsNone(runtime.master)
        self._assert_no_bridge_receivers(runtime)

    def _assert_no_bridge_receivers(self, runtime: MitmRuntime) -> None:
        """共享 view 上只允许 view 自身及其 focus / settings 的接收器。

        接收器留在跨代共享的 view 上，重启内核后每个 View 事件都会双发。
        Focus / Settings 组件及 FerretView 自身的容量记账与 view 同寿，不是
        每代产物，放行；弱引用已死的条目 SyncSignal 只做惰性清理，直接跳过。
        """
        view = runtime.view
        native_owners = {id(view), id(view.focus), id(view.settings)}
        for signal in (
            view.sig_store_add,
            view.sig_store_remove,
            view.sig_view_add,
            view.sig_view_update,
            view.sig_view_remove,
            view.sig_view_refresh,
        ):
            for ref in signal.receivers:
                receiver = ref()
                if receiver is None:
                    continue
                owner = getattr(receiver, "__self__", None)
                self.assertIsNotNone(owner)
                self.assertIn(id(owner), native_owners)

    def test_bridge_receivers_severed_even_when_done_hook_is_skipped(self) -> None:
        """原生 run() 在 setup_servers 阶段见到 should_exit 会直接 return，而这条
        早退不在保证 done() 的 try/finally 之内 —— done() 不保证执行，断连必须由
        `_run_master` 的 finally 兜底。用「run() 返回但不派发 DoneHook」的替身把
        这条路径钉死（真实早退依赖停止落在 setup_servers 窗口内，测不稳）。"""
        runtime = MitmRuntime(listen_port=free_port())

        async def run_without_done(self: FerretMaster) -> None:
            return None

        with patch.object(FerretMaster, "run", run_without_done):
            runtime.start()
            self.assertTrue(runtime.stop())
        self._assert_no_bridge_receivers(runtime)

        # 泄漏的可见后果：重启内核后同一 View 事件只允许发一次，旧缺陷下残留
        # 的旧接收器会再发一遍（flow_stored 翻倍）。
        start_runtime(runtime)
        self.addCleanup(runtime.stop)
        rows: list[object] = []
        runtime.flow_stored.connect(rows.append)
        runtime.view.sig_store_add.send(flow=tflow.tflow(resp=True))
        self.assertEqual(len(rows), 1)


class RuntimeRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch(
            "ferret.core.mitm.runtime.get_certs_dir", return_value=Path(tmp.name)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        cert_patcher = patch(
            "ferret.core.mitm.certificate.get_certs_dir", return_value=Path(tmp.name)
        )
        cert_patcher.start()
        self.addCleanup(cert_patcher.stop)

    def _runtime(self, **kwargs) -> MitmRuntime:
        runtime = MitmRuntime(listen_port=free_port(), **kwargs)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        return runtime

    def test_all_listeners_failing_emits_failure_and_leaves_starting(self) -> None:
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            runtime = MitmRuntime(listen_port=blocker.getsockname()[1])
            self.addCleanup(runtime.stop)
            errors = []
            runtime.failed.connect(errors.append)
            # Model the port becoming occupied after the early availability test.
            with patch.object(_MitmThread, "_ensure_port_available"):
                runtime.start()
                self.assertTrue(
                    wait_until(lambda: runtime.state is MitmRuntimeState.FAILED)
                )
                self.assertTrue(wait_until(lambda: runtime._thread is None))
            self.assertTrue(errors)
            self.assertIn("监听失败", errors[0])

    def test_channel_intent_changed_after_options_construction_is_applied_before_ready(
        self,
    ) -> None:
        options_constructed = threading.Event()
        release_startup = threading.Event()
        original = _MitmThread._apply_serverplayback

        def pause_after_options(thread, master):
            original(thread, master)
            options_constructed.set()
            if not release_startup.wait(5):
                raise RuntimeError("test did not release startup")

        runtime = MitmRuntime(
            listen_port=free_port(), use_socks5=True, socks5_port=free_port()
        )
        self.addCleanup(runtime.stop)
        self.addCleanup(release_startup.set)
        with patch.object(_MitmThread, "_apply_serverplayback", pause_after_options):
            runtime.start()
            self.assertTrue(wait_until(options_constructed.is_set))
            self.assertEqual(runtime.state, MitmRuntimeState.STARTING)
            runtime.set_channels_engaged(True)
            release_startup.set()
            wait_ready(runtime)
        master = runtime.master
        assert master is not None
        self.assertEqual(
            runtime.call(lambda: master.options.mode), runtime._mode_specs()
        )
        self.assertTrue(
            wait_until(lambda: runtime.channel_health().get("socks5") is True)
        )
        addresses = runtime.call(master.proxyserver.listen_addrs)
        self.assertTrue(any(address[1] == runtime.socks5_port for address in addresses))

    def test_a_started_synchronous_commit_returns_its_result_after_the_deadline(
        self,
    ) -> None:
        runtime = self._runtime()
        release = threading.Event()
        finished = threading.Event()
        timer = threading.Timer(0.08, release.set)
        self.addCleanup(timer.cancel)
        timer.start()

        def commit():
            self.assertTrue(release.wait(2))
            finished.set()
            return "committed"

        self.assertEqual(runtime.call(commit, timeout=0.01), "committed")
        self.assertTrue(finished.is_set())

    def test_settings_changed_after_startup_seeding_are_committed_before_ready(self):
        for enabled in (True, False):
            with (
                self.subTest(enabled=enabled),
                tempfile.TemporaryDirectory() as directory,
            ):
                seeded = threading.Event()
                release = threading.Event()
                original = _MitmThread._apply_serverplayback

                def pause_after_seed(
                    thread, master, seed=original, reached=seeded, gate=release
                ):
                    seed(thread, master)
                    reached.set()
                    if not gate.wait(5):
                        raise RuntimeError("test did not release startup")

                script = Path(directory) / "sample.py"
                script.write_text("def request(flow):\n    pass\n", encoding="utf-8")
                runtime = MitmRuntime(listen_port=free_port())
                runtime.apply_scripts([ScriptEntry(str(script))], enabled=not enabled)
                runtime.mock_pool = [tflow.tflow(resp=True)]
                runtime.apply_mock_enabled(not enabled)
                try:
                    with patch.object(
                        _MitmThread, "_apply_serverplayback", pause_after_seed
                    ):
                        runtime.start()
                        self.assertTrue(wait_until(seeded.is_set))
                        self.assertEqual(runtime.state, MitmRuntimeState.STARTING)
                        runtime.apply_protocol_options(http2=False, http3=False)
                        runtime.apply_dns_options(
                            name_servers=["1.1.1.1"], use_hosts_file=False
                        )
                        runtime.apply_ssl_options(
                            insecure=True, add_upstream_certs=True
                        )
                        runtime.apply_client_certs(path=directory)
                        runtime.apply_scripts(enabled=enabled)
                        runtime.apply_mock_enabled(enabled)
                        runtime.apply_mock_knobs({"server_replay_reuse": True})
                        release.set()
                        wait_ready(runtime)
                    master = runtime.master
                    assert master is not None

                    def inspect(master=master, enabled=enabled, directory=directory):
                        self.assertFalse(master.options.http2)
                        self.assertFalse(master.options.http3)
                        self.assertEqual(master.options.dns_name_servers, ["1.1.1.1"])
                        self.assertFalse(master.options.dns_use_hosts_file)
                        self.assertTrue(master.options.ssl_insecure)
                        self.assertTrue(
                            master.options.add_upstream_certs_to_client_chain
                        )
                        self.assertEqual(master.options.client_certs, directory)
                        self.assertEqual(bool(master.scripts.loaded), enabled)
                        self.assertEqual(bool(master.server_playback.flowmap), enabled)
                        self.assertTrue(master.options.server_replay_reuse)

                    runtime.call(inspect)
                finally:
                    release.set()
                    runtime.stop()

    def test_a_timed_out_queued_callback_never_runs_later(self) -> None:
        runtime = self._runtime()
        thread = runtime._thread
        assert thread is not None and thread.loop is not None
        blocking = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def occupy_loop():
            blocking.set()
            release.wait(2)

        thread.loop.call_soon_threadsafe(occupy_loop)
        self.assertTrue(blocking.wait(2))
        commit = Mock()
        with self.assertRaises(TimeoutError):
            runtime.call(commit, timeout=0.01)
        release.set()
        runtime.call(lambda: None)
        commit.assert_not_called()

    def test_ready_reconciliation_does_not_restore_consumed_mock_responses(self):
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        response = tflow.tflow(resp=True)
        runtime.mock_pool = [response]
        runtime.apply_mock_enabled(True)
        runtime.apply_mock_knobs({"server_replay_reuse": False})
        original = UiBridgeAddon.running
        consumed = []

        def consume_before_ready(bridge):
            master = bridge._master
            assert master is not None
            consumed.append(master.server_playback.next_flow(response) is not None)
            # An unrelated knob change must only rehash the remaining entries.
            runtime.apply_mock_knobs({"server_replay_refresh": False})
            original(bridge)

        with patch.object(UiBridgeAddon, "running", consume_before_ready):
            start_runtime(runtime)
        self.assertEqual(consumed, [True])
        master = runtime.master
        assert master is not None
        self.assertFalse(runtime.call(lambda: bool(master.server_playback.flowmap)))
        self.assertFalse(runtime.call(lambda: master.options.server_replay_refresh))

    def test_completion_racing_the_timeout_still_returns_the_committed_result(
        self,
    ) -> None:
        runtime = self._runtime()

        class CompletedAtDeadline(Future[object]):
            def result(self, timeout: float | None = None) -> object:
                value = super().result(timeout)
                if timeout is not None:
                    # Deterministically model completion between the expired
                    # wait and the cancellation/rollback decision.
                    raise TimeoutError("completion raced the deadline")
                return value

        with patch("ferret.core.mitm.runtime.Future", CompletedAtDeadline):
            self.assertEqual(runtime.call(lambda: "committed"), "committed")

    def test_async_timeout_waits_for_cancellation_cleanup(self) -> None:
        runtime = self._runtime()
        cleaned = threading.Event()

        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

        with self.assertRaises(TimeoutError):
            runtime.call(pending, timeout=0.02)
        self.assertTrue(cleaned.is_set())

    def test_cancelled_local_startup_placeholder_is_cleared_without_a_server(
        self,
    ) -> None:
        # Exercise the native startup ordering with its asynchronous Rust factory
        # replaced: no actual WinDivert service or UAC operation runs in this test.
        instance = LocalRedirectorInstance(mode_specs.LocalMode.parse("local"), Mock())
        server = Mock()
        start = AsyncMock(side_effect=[asyncio.CancelledError(), server])
        with (
            patch.object(LocalRedirectorInstance, "_server", None),
            patch.object(LocalRedirectorInstance, "_instance", None),
            patch("mitmproxy_rs.local.start_local_redirector", start),
        ):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(instance._start())
            self.assertIs(LocalRedirectorInstance._instance, instance)
            self.assertIsNone(LocalRedirectorInstance._server)
            MitmRuntime._disarm_local_redirector()
            self.assertIsNone(LocalRedirectorInstance._instance)
            asyncio.run(instance._start())
            server.set_intercept.assert_called_once()
            MitmRuntime._disarm_local_redirector()

    def test_shutdown_disarm_failure_is_logged_and_retryable(self) -> None:
        runtime = self._runtime()
        with (
            patch.object(LocalRedirectorInstance, "_instance", object()),
            patch.object(
                runtime,
                "_disarm_local_redirector",
                side_effect=RuntimeError("disarm failed"),
            ),
            patch("ferret.core.mitm.runtime.log") as logger,
        ):
            self.assertFalse(runtime.stop())
            self.assertTrue(runtime.is_running)
        logger.exception.assert_called_once()
        self.assertTrue(runtime.stop())

    def test_final_disarm_failure_can_be_retried_after_the_thread_exits(self) -> None:
        runtime = self._runtime()
        server = Mock()
        server.set_intercept.side_effect = [None, OSError("daemon unavailable"), None]
        with (
            patch.object(LocalRedirectorInstance, "_server", server),
            patch("ferret.core.mitm.runtime.log") as logger,
        ):
            self.assertFalse(runtime.stop())
            self.assertIsNone(runtime._thread)
            self.assertEqual(runtime.state, MitmRuntimeState.STOPPING)
            self.assertTrue(runtime.stop())
            self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)
        self.assertEqual(server.set_intercept.call_count, 3)
        logger.exception.assert_called_once()


class ReverseChannelStateTests(unittest.TestCase):
    """reverse 三意图值与 engaged 闸门的内建状态机（不跑真内核）。

    让路判据与 channel_health reverse 键是反向代理通道的两个独立验收面：
    前者纯函数式可钉，后者需要真内核拉起实例、复现 spec 启动失败探针。
    让路与 engaged 闸门在这一组跑；channel_health reverse 键在
    ``test_master_addon_assembly``（test_facade）里跟 UpdateAltSvc 挂载点
    一同验。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_intents_have_reverse_disabled_and_empty_target(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.assertFalse(runtime.use_reverse)
        self.assertEqual(runtime.reverse_target, "")
        self.assertEqual(runtime.reverse_port, REVERSE_DEFAULT_PORT)

    def test_mode_specs_omit_reverse_when_disengaged(self) -> None:
        """未接通时 reverse 不进 mode 列表：与 local/wireguard 同构。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime._mode_specs(), ["regular"])

    def test_mode_specs_include_reverse_when_engaged(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True
        specs = runtime._mode_specs()
        self.assertEqual(specs[0], "regular")
        self.assertTrue(specs[1].startswith("reverse:"))
        self.assertIn("example.com", specs[1])

    def test_apply_channels_rejects_bad_target_and_rolls_back(self) -> None:
        """坏 target 在落盘前被原生解析器拒，意图值整体回滚到 previous。

        必须在 ``channels_engaged=True`` 下验证：未接通时 reverse spec 根本
        不进 ``_mode_specs()``（与 local/wireguard 同构），validate 当然过——
        这与 plan §4 的「不接通的意图值不算提交到内核」语义一致。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True
        previous = runtime._channel_intents()
        with self.assertRaises(ValueError):
            runtime.apply_channels(
                use_reverse=True, reverse_target="not a url", reverse_port=8081
            )
        # 快照断言一次，新增意图值只需改 _CHANNEL_INTENTS 一处（逐条展开的写法
        # 在字段变多后必然漏掉某一条，这正是抽出这对方法的动机）。
        self.assertEqual(runtime._channel_intents(), previous)


class Socks5ChannelStateTests(unittest.TestCase):
    """SOCKS5 入站两意图值与 engaged 闸门（docs/design.md#capture）。

    与 reverse 同构，只是 socks5 spec 追加在 wireguard 之后、不替换首槽。让路判据
    在 ``EffectiveBlockPrivateTests`` 一同验，channel_health socks5 键在 test_facade
    的实例装配用例里跟真内核一起跑。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_intents_have_socks5_disabled_on_conventional_port(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.assertFalse(runtime.use_socks5)
        self.assertEqual(runtime.socks5_port, SOCKS5_DEFAULT_PORT)

    def test_mode_specs_omit_socks5_when_disengaged(self) -> None:
        """未接通时 socks5 不进 mode 列表：与 local/wireguard/reverse 同构。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_socks5=True,
            socks5_port=1080,
        )
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime._mode_specs(), ["regular"])

    def test_mode_specs_include_socks5_when_engaged(self) -> None:
        """接通后 socks5 spec 追加在末尾、带显式 ``@``（独立端口，不占首槽）。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_socks5=True,
            socks5_port=1080,
        )
        runtime.listen_host = LOOPBACK_HOST
        runtime.channels_engaged = True
        specs = runtime._mode_specs()
        self.assertEqual(specs[0], "regular")
        self.assertTrue(specs[-1].startswith("socks5@"))
        self.assertIn(":1080", specs[-1])

    def test_apply_channels_carries_both_socks5_intents(self) -> None:
        """apply_channels 透传两意图值；未传的字段不动（None 语义）。"""
        runtime = MitmRuntime(listen_port=free_port())
        runtime.apply_channels(use_socks5=True, socks5_port=1081)
        self.assertTrue(runtime.use_socks5)
        self.assertEqual(runtime.socks5_port, 1081)

    def test_socks5_intents_are_part_of_the_rollback_snapshot(self) -> None:
        """两意图值进 ``_CHANNEL_INTENTS`` 快照：坏提交回滚时它们一并复原
        （§3.3-1；快照写法保证新增字段零散漏）。"""
        from ferret.core.mitm.runtime import _CHANNEL_INTENTS

        self.assertIn("use_socks5", _CHANNEL_INTENTS)
        self.assertIn("socks5_port", _CHANNEL_INTENTS)
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_socks5=True,
            socks5_port=1080,
        )
        previous = runtime._channel_intents()
        # 模拟一次半途改动后回滚：socks5 两值必须随快照整体复原。
        runtime.use_socks5 = False
        runtime.socks5_port = 9999
        runtime._restore_intents(previous)
        self.assertTrue(runtime.use_socks5)
        self.assertEqual(runtime.socks5_port, 1080)


class UpstreamIntentTests(unittest.TestCase):
    """上游代理意图值、首槽替换与凭证拼装（docs/design.md#capture）。

    上游**不是**第五条通道：它换掉 ``mode[0]``，凭证走另一条正交的
    ``upstream_auth`` 选项。这一组只验纯状态机，不跑真内核。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_intents_have_upstream_disabled_and_empty_fields(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.assertFalse(runtime.use_upstream)
        self.assertEqual(runtime.upstream_target, "")
        self.assertEqual(runtime.upstream_username, "")
        self.assertEqual(runtime.upstream_password, "")

    def test_mode_specs_omit_upstream_when_disengaged(self) -> None:
        """未接通时首槽仍是 regular：与四条通道同一道 engaged 闸门。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime._mode_specs(), ["regular"])

    def test_mode_specs_replace_the_head_slot_when_engaged(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            use_wireguard=True,
        )
        runtime.channels_engaged = True
        specs = runtime._mode_specs()
        self.assertEqual(specs[0], "upstream:http://proxy.corp:8080")
        # 替换不是追加：regular 不得同时在场（都回退全局 listen_port，
        # 同时下发必被 proxyserver 的地址查重拒）。
        self.assertNotIn("regular", specs)

    def test_upstream_auth_is_none_when_username_is_empty(self) -> None:
        """空用户名必须回 ``None``，**不能**是空串。

        ``parse_upstream_auth`` 的正则是 ``.+:`` —— 实测 ``upstream_auth=""``
        抛 ``OptionsError: Invalid upstream auth specification:``，而这条路径在
        「用户只填地址、不填凭证」时天天走。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_is_none_when_upstream_is_off(self) -> None:
        """上游关掉时凭证一并失效。

        不只是「省一次赋值」：``UpstreamAuth.requestheaders``
        （``upstream_auth.py:56-58``）在 auth 非空时会给 **reverse 通道**的请求
        补 ``Authorization`` 头。关掉上游还留着凭证，等于把企业代理密码持续发给
        反代目标。这一条是那个串台面的主闸门。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=False,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_is_none_when_the_target_is_empty(self) -> None:
        """勾了上游但没填地址 → 凭证也必须失效。

        判据要与「首槽位是否真的换成了 upstream」对齐：``capture_mode_specs``
        见到空目标会保持 ``regular``（空 spec 推进内核只会让系统代理整条死掉），
        上游因此并未生效。若此处仍回凭证，配上 reverse 就是把企业代理密码白送给
        反代目标 —— 与 `test_upstream_auth_is_none_when_upstream_is_off` 钉的是
        同一个串台面，只是入口换成了「开着但没生效」。

        对话框拦得住「勾了没填」，但手改配置文件、或直接调
        ``apply_channels(use_upstream=True)`` 不带目标都绕得过去。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="   ",
            upstream_username="alice",
            upstream_password="secret",
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True

        # 前提：首槽位确实还是 regular，上游没进 mode。
        self.assertEqual(runtime._mode_specs()[0], "regular")
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_joins_user_and_password(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:secret")

    def test_password_containing_colon_is_passed_through(self) -> None:
        """密码含冒号原样拼：服务端按**首个**冒号切，所以只有用户名不能含冒号
        —— 这正是 UI 分两个输入框收、而不是让用户自己拼 ``user:pass`` 的理由。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="a:b:c",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:a:b:c")

    def test_empty_password_still_emits_a_credential(self) -> None:
        """用户名非空、密码为空是合法的（``.+:`` 只要求冒号前非空）。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:")

    def test_apply_channels_rejects_bad_upstream_and_rolls_back(self) -> None:
        """坏上游地址在下发前被原生解析器拒，意图值整体回滚。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        runtime.channels_engaged = True
        previous = runtime._channel_intents()
        with self.assertRaises(ValueError):
            runtime.apply_channels(
                use_upstream=True,
                upstream_target="ftp://proxy.corp:8080",
                upstream_username="alice",
                upstream_password="secret",
            )
        self.assertEqual(runtime._channel_intents(), previous)
        # 凭证也在回滚范围里：失败的提交不该把用户名密码留在内核里。
        self.assertEqual(runtime.upstream_username, "")
        self.assertEqual(runtime.upstream_password, "")


class EffectiveBlockPrivateTests(unittest.TestCase):
    """``_effective_block_private`` 让路判据（plan §4 定式）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _runtime(
        self,
        *,
        block_private: bool = True,
        listen_host: str = LOOPBACK_HOST,
        use_wireguard: bool = False,
        use_reverse: bool = False,
        use_socks5: bool = False,
        channels_engaged: bool = True,
    ) -> MitmRuntime:
        runtime = MitmRuntime(
            listen_port=free_port(),
            block_private=block_private,
            use_wireguard=use_wireguard,
            use_reverse=use_reverse,
            use_socks5=use_socks5,
        )
        runtime.listen_host = listen_host
        runtime.channels_engaged = channels_engaged
        return runtime

    def test_no_yield_when_block_private_disabled(self) -> None:
        runtime = self._runtime(
            block_private=False, listen_host=ANY_HOST, use_reverse=True
        )
        self.assertFalse(runtime._effective_block_private())

    def test_no_yield_when_disengaged(self) -> None:
        """engaged 闸门：未接通时让路不发生（空操作、保留原值）。"""
        runtime = self._runtime(
            listen_host=ANY_HOST, use_reverse=True, channels_engaged=False
        )
        self.assertTrue(runtime._effective_block_private())

    def test_yield_only_when_reverse_enabled_and_bound_to_any(self) -> None:
        """bind 环回时 reverse 不让路（局域网流量到不了环回 socket），
        bind ANY_HOST 时让路生效（与 wireguard 让路条件同式）。"""
        loopback = self._runtime(listen_host=LOOPBACK_HOST, use_reverse=True)
        any_host = self._runtime(listen_host=ANY_HOST, use_reverse=True)
        self.assertTrue(loopback._effective_block_private())
        self.assertFalse(any_host._effective_block_private())

    def test_yield_only_when_socks5_enabled_and_bound_to_any(self) -> None:
        """socks5 与 reverse 同式：绑环回不让路，绑 ANY_HOST 才让路
        （docs/design.md#capture）。"""
        loopback = self._runtime(listen_host=LOOPBACK_HOST, use_socks5=True)
        any_host = self._runtime(listen_host=ANY_HOST, use_socks5=True)
        self.assertTrue(loopback._effective_block_private())
        self.assertFalse(any_host._effective_block_private())

    def test_socks5_yield_respects_engaged_gate(self) -> None:
        """未接通时 socks5 让路不发生：意图开着也保留原值。"""
        runtime = self._runtime(
            listen_host=ANY_HOST, use_socks5=True, channels_engaged=False
        )
        self.assertTrue(runtime._effective_block_private())

    def test_wireguard_still_yields_regardless_of_listen_host(self) -> None:
        """wireguard 客户端来自固定 10.0.0.x 段，让路与绑定地址无关
        —— 这是 wireguard 先例，reverse 的让路条件按「绑定非环回」是新增的。"""
        runtime = self._runtime(listen_host=LOOPBACK_HOST, use_wireguard=True)
        self.assertFalse(runtime._effective_block_private())

    def test_neither_yields_keeps_user_value(self) -> None:
        """两条让路都不成立时原值下发；用户偏好保留。"""
        runtime = self._runtime(listen_host=LOOPBACK_HOST)
        self.assertTrue(runtime._effective_block_private())

    def test_upstream_does_not_change_the_yield_verdict(self) -> None:
        """上游代理**不让路**，理由干净：Block 管的是客户端**来源** IP，上游
        只换出口，监听地址与来源判定一字未动（计划 §6）。开上游时让路判据
        必须与不开时逐项相同。
        """
        for listen_host in (LOOPBACK_HOST, ANY_HOST):
            for use_reverse in (False, True):
                with self.subTest(listen_host=listen_host, use_reverse=use_reverse):
                    off = self._runtime(
                        listen_host=listen_host, use_reverse=use_reverse
                    )
                    on = self._runtime(listen_host=listen_host, use_reverse=use_reverse)
                    on.use_upstream = True
                    on.upstream_target = "http://proxy.corp:8080"
                    self.assertEqual(
                        on._effective_block_private(),
                        off._effective_block_private(),
                    )


class MasterLoggingTests(unittest.TestCase):
    """内核装配不许在根 logger 上留东西。

    原生 `Master.__init__` 把 `LegacyLogEvents` 挂上根 logger 且从不摘，它每条日志
    都 `call_soon_threadsafe` 回内核循环发早已废弃的 add_log 钩子。停掉抓包后循环
    一关，应用里随便哪句 log 都会从 `Handler.emit` 里抛 RuntimeError 穿透调用方
    —— 一次抓包就够把之后所有日志变成地雷，所以 FerretMaster 构造时就摘掉它。
    """

    def setUp(self) -> None:
        # 同进程先跑过的 `taddons.context` 用例若漏摘，根 logger 上会留一个指向
        # 死循环的 LegacyLogEvents —— 本类钉的是「FerretMaster 装配不新增」，
        # 先把前人漏摘的垃圾清掉再验（漏摘处已在各自用例里补摘，这里只是兜底）。
        for handler in logging.getLogger().handlers[:]:
            if isinstance(handler, MitmLogHandler):
                handler.uninstall()

    def _master(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        FerretMaster(event_loop=loop)
        return loop

    def test_assembly_leaves_no_handler_on_the_root_logger(self) -> None:
        loop = self._master()
        self.addCleanup(loop.close)
        self.assertEqual(
            [h for h in logging.getLogger().handlers if isinstance(h, MitmLogHandler)],
            [],
        )

    def test_logging_survives_the_kernel_loop_going_away(self) -> None:
        loop = self._master()
        loop.close()
        # 兜底 handler 只为压住 `lastResort` 往 stderr 打这条 WARNING，
        # 真正要验的是这句 log 不抛。
        root = logging.getLogger()
        quiet = logging.NullHandler()
        root.addHandler(quiet)
        self.addCleanup(root.removeHandler, quiet)
        logging.getLogger("ferret.test").warning("内核已停，日志照记")


if __name__ == "__main__":
    unittest.main()
