from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.save import Save
from mitmproxy.test import taddons, tflow

from ferret.core.mitm import CaptureMaster, FlowFile, HTTPFlow, MitmFacade, MitmRuntime
from ferret.core.mitm.bindings import Flow, FlowReadException
from ferret.core.mitm.gateway import GatewayPolicy
from ferret.core.mitm.sse import SSE_EVENTS_TRUNCATED_KEY
from ferret.core.mitm.view import FerretView

from .test_gateway_addons import l7, ruleset


class NativeSaveIntegrationTests(unittest.TestCase):
    def test_capture_master_registers_native_save(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            self.assertIsInstance(master.save, Save)
            self.assertIn(master.save, master.addons.chain)
        finally:
            loop.close()

    def test_native_save_writes_readable_http_flow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.flow"
            addon = Save()
            with taddons.context(addon) as context:
                # taddons.context 退出时只关循环，不摘它挂到根 logger 上的
                # LegacyLogEvents（上游靠 PYTEST_CURRENT_TEST 换手，我们跑
                # unittest）——留下一个指向死循环的 handler，顺手摘掉。
                self.addCleanup(context.master._legacy_log_events.uninstall)
                context.options.save_stream_file = str(path)
                flow = tflow.tflow(resp=True)
                addon.request(flow)
                addon.response(flow)
                context.options.save_stream_file = None

            self.assertEqual(len(FlowFile.read(path)), 1)


class RecordingImportTests(unittest.TestCase):
    """真 Save / ReadFile 与磁盘文件；只替代 runtime 的跨线程调度。"""

    def setUp(self) -> None:
        # AddonManager logs and swallows hook errors; recording assertions alone
        # must not let a broken lifecycle pass unnoticed.
        self.enterContext(self.assertNoLogs("mitmproxy.addonmanager", level="ERROR"))
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = CaptureMaster(event_loop=self.loop)
        self.addCleanup(self.master.save.done)
        # No master.run() here, so TlsConfig.running() never initializes its CA.
        # Dispatch the native configure hook before loading live requests, using
        # an isolated store rather than the user's certificates.
        self.master.options.update(confdir=str(self.directory / "certs"))
        runtime = mock.Mock(
            spec=MitmRuntime,
            master=self.master,
            view=self.master.view,
            is_running=True,
        )
        runtime.call.side_effect = self._call
        self.enterContext(
            mock.patch(
                "ferret.core.mitm.facade.get_sessions_dir", return_value=self.directory
            )
        )
        self.enterContext(
            mock.patch(
                "ferret.core.mitm.facade.get_mock_pool_file",
                return_value=self.directory / "mock.flow",
            )
        )
        self.facade = MitmFacade(runtime)
        self.recording = self.facade.start_capture_recording()
        self._record("/before")
        history = tflow.tflow(resp=True)
        history.request.path = "/history"
        self.history = self.directory / "history.flow"
        FlowFile.write(self.history, [history])

    def _call(self, callback, *, timeout: float = 5.0):
        async def invoke():
            result = callback()
            return await result if inspect.isawaitable(result) else result

        return self.loop.run_until_complete(invoke())

    def _record(self, path: str) -> None:
        flow = tflow.tflow(resp=True)
        flow.request.path = path
        self._call(lambda: self.master.load_flow(flow))
        flow.live = False
        stream = self.master.save.stream
        assert stream is not None
        stream.fo.flush()

    def test_import_preserves_websocket_numbers_after_retention(self) -> None:
        flow = tflow.twebsocketflow()
        view = self.master.view
        assert isinstance(view, FerretView)
        with mock.patch("ferret.core.mitm.view.WS_FRAME_LIMIT", 2):
            view.websocket_message(flow)
            FlowFile.write(self.history, [flow])
            self.assertEqual(self.facade.load_flow_file(self.history), 1)
        snapshot = self.facade.flow_messages(flow.id)
        self.assertEqual([frame.index for frame in snapshot["frames"]], [1, 2])
        self.assertEqual(snapshot["count"], 3)
        self.assertEqual(
            [frame.index for frame in self.facade.websocket_frames(flow.id)], [1, 2]
        )

    def _recorded_paths(self) -> list[str]:
        paths = []
        for flow in FlowFile.read(self.recording):
            assert isinstance(flow, HTTPFlow)
            paths.append(flow.request.path)
        return paths

    def test_repeated_imports_preserve_recording_and_resume_writing(self) -> None:
        for index in range(2):
            with self.subTest(index=index):
                before = self.recording.read_bytes()
                self.assertEqual(self.facade.load_flow_file(self.history), 1)
                self.assertEqual(self.recording.read_bytes(), before)
                self._record(f"/after-{index}")

        self.facade.stop_capture_recording()
        self.assertEqual(
            self._recorded_paths(),
            ["/before", "/after-0", "/after-1"],
        )

    def test_empty_import_keeps_recording_on_stop(self) -> None:
        self.history.write_bytes(b"")
        before = self.recording.read_bytes()

        self.assertEqual(self.facade.load_flow_file(self.history), 0)
        self.facade.stop_capture_recording()

        self.assertEqual(self.recording.read_bytes(), before)

    def test_failed_imports_preserve_recording_and_resume_writing(self) -> None:
        self.history.write_bytes(b"invalid-flow")
        for source in (self.history, self.directory / "missing.flow"):
            with self.subTest(source=source.name):
                before = self.recording.read_bytes()
                with (
                    self.assertLogs(level="ERROR"),
                    self.assertRaises(FlowReadException),
                ):
                    self.facade.load_flow_file(source)
                self.assertEqual(self.recording.read_bytes(), before)
                self._record(f"/after-{source.stem}")

        self.facade.stop_capture_recording()
        self.assertEqual(
            self._recorded_paths(),
            ["/before", "/after-history", "/after-missing"],
        )

    def test_importing_active_recording_preserves_source(self) -> None:
        before = self.recording.read_bytes()

        self.assertEqual(self.facade.load_flow_file(self.recording), 1)
        self.facade.stop_capture_recording()

        self.assertEqual(self.recording.read_bytes(), before)

    def test_import_after_stop_does_not_restart_recording(self) -> None:
        self.facade.stop_capture_recording()
        before = self.recording.read_bytes()

        self.assertEqual(self.facade.load_flow_file(self.history), 1)

        self.assertIsNone(self.master.save.stream)
        self.assertIsNone(self.master.options.save_stream_file)
        self.assertEqual(self.recording.read_bytes(), before)

    def test_restart_in_the_same_second_gets_a_unique_file(self) -> None:
        """#74：秒精度文件名 + Save 的 wb 截断，同秒重开会吃掉上一段录制。"""
        self.facade.stop_capture_recording()
        frozen = self.enterContext(mock.patch("ferret.core.mitm.facade.datetime"))
        frozen.now.return_value = datetime(2026, 9, 30, 12, tzinfo=UTC)
        first = self.facade.start_capture_recording()
        self._record("/first")
        self.facade.stop_capture_recording()
        before = first.read_bytes()
        second = self.facade.start_capture_recording()
        self.addCleanup(self.facade.stop_capture_recording)
        self.assertNotEqual(second, first)
        self.assertEqual(second.stem, first.stem + "-2")
        self.assertTrue(second.exists())  # 占位文件已建（Save 写入前是空的）
        self._record("/again")
        self.facade.stop_capture_recording()

        # 第一段的正文原样保留，没有落在第二段的文件里。
        self.assertEqual(self._recorded_paths(), ["/before"])
        self.assertEqual(first.read_bytes(), before)
        second_paths = []
        for flow in FlowFile.read(second):
            assert isinstance(flow, HTTPFlow)
            second_paths.append(flow.request.path)
        self.assertEqual(second_paths, ["/again"])

    def test_repeated_start_on_the_same_kernel_reuses_the_recording_file(self) -> None:
        """录制已与系统代理勾选解耦：抓包中重挂代理会再次调 start，同内核同文件
        幂等返回，不把一段会话拆成两个 capture 文件（原生 Save 换路径会关旧流）。"""
        again = self.facade.start_capture_recording()
        self.assertEqual(again, self.recording)
        self._record("/after-restart")
        self.assertEqual(self._recorded_paths(), ["/before", "/after-restart"])

    def test_start_after_the_stream_was_reset_opens_a_fresh_file(self) -> None:
        """内核换血后 options 全新（save_stream_file=None）：旧路径是陈账，录制
        重开新文件，不复用死内核留下的路径。"""
        self.master.options.update(save_stream_file=None)
        self.assertIsNone(self.master.save.stream)
        second = self.facade.start_capture_recording()
        self.addCleanup(self.facade.stop_capture_recording)
        self.assertNotEqual(second, self.recording)
        self._record("/fresh")
        fresh_paths = []
        for flow in FlowFile.read(second):
            assert isinstance(flow, HTTPFlow)
            fresh_paths.append(flow.request.path)
        self.assertEqual(fresh_paths, ["/fresh"])

    def test_import_id_collection_excludes_other_async_tasks(self) -> None:
        imported_ids: set[str] = set()
        external = tflow.tflow(resp=True)
        external.request.path = "/concurrent-network-request"
        external_task = self.loop.create_task(self.master.load_flow(external))
        self.facade.load_flow_file(self.history, imported_ids=imported_ids)
        self.loop.run_until_complete(external_task)
        self.assertEqual(
            imported_ids, {flow.id for flow in FlowFile.read(self.history)}
        )
        self.assertNotIn(external.id, imported_ids)

    def test_importing_with_a_flow_in_flight_records_it_once_and_complete(self) -> None:
        """#79：导入触发暂停冲刷，在途流不能按「未完成」先写一条。

        修复前磁盘上是同 id 的未完成+完成两条，会话打开时 View.add 只收
        首条，展示的永远是没有响应的那条。
        """
        in_flight = tflow.tflow()
        in_flight.request.path = "/in-flight"
        self._call(lambda: self.master.load_flow(in_flight))

        self.assertEqual(self.facade.load_flow_file(self.history), 1)

        in_flight.response = tflow.tresp()
        self._call(lambda: self.master.load_flow(in_flight))
        self.facade.stop_capture_recording()

        flows = [f for f in FlowFile.read(self.recording) if isinstance(f, HTTPFlow)]
        self.assertEqual([f.request.path for f in flows], ["/before", "/in-flight"])
        self.assertIsNotNone(flows[1].response)

    def test_network_flow_completed_during_import_is_recorded(self) -> None:
        entered = asyncio.Event()
        completed = asyncio.Event()
        external = tflow.tflow(resp=True)
        external.request.path = "/during-import"
        native_load = self.master.readfile.load_flows_from_path

        async def load(path: str) -> int:
            entered.set()
            await completed.wait()
            return await native_load(path)

        async def network() -> None:
            await entered.wait()
            await self.master.load_flow(external)
            completed.set()

        task = self.loop.create_task(network())
        with mock.patch.object(self.master.readfile, "load_flows_from_path", load):
            self.assertEqual(self.facade.load_flow_file(self.history), 1)
        self.loop.run_until_complete(task)
        self.facade.stop_capture_recording()
        self.assertEqual(self._recorded_paths(), ["/before", "/during-import"])

    def test_imported_duplicate_id_uses_last_record(self) -> None:
        first = tflow.tflow()
        first.request.path = "/duplicate"
        last = first.copy()
        last.id = first.id
        last.response = tflow.tresp(content=b"complete response")
        FlowFile.write(self.history, [first, last])
        added: list[HTTPFlow] = []
        updated: list[HTTPFlow] = []

        def on_add(flow: Flow) -> None:
            assert isinstance(flow, HTTPFlow)
            added.append(flow)

        def on_update(flow: Flow) -> None:
            assert isinstance(flow, HTTPFlow)
            updated.append(flow)

        self.master.view.sig_view_add.connect(on_add)
        self.master.view.sig_view_update.connect(on_update)

        self.assertEqual(self.facade.load_flow_file(self.history), 2)

        stored = self.master.view.get_by_id(first.id)
        assert isinstance(stored, HTTPFlow)
        self.assertIsNotNone(stored.response)
        assert stored.response is not None
        self.assertEqual(stored.response.raw_content, b"complete response")
        self.assertEqual(added, [stored])
        self.assertTrue(updated)
        self.assertTrue(all(flow is stored for flow in updated))

    def test_import_does_not_overwrite_a_live_flow_with_the_same_id(self) -> None:
        live = tflow.tflow()
        live.live = True
        live.request.path = "/live"
        self._call(lambda: self.master.load_flow(live))
        archived = live.copy()
        archived.id = live.id
        archived.request.path = "/archived"
        archived.response = tflow.tresp()
        FlowFile.write(self.history, [archived])
        imported_ids: set[str] = set()

        self.assertEqual(
            self.facade.load_flow_file(self.history, imported_ids=imported_ids), 1
        )

        self.assertIs(self.master.view.get_by_id(live.id), live)
        self.assertEqual(live.request.path, "/live")
        self.assertIsNone(live.response)
        self.assertTrue(live.live)
        self.assertFalse(imported_ids)

    def test_bypassed_duplicate_import_preserves_the_last_admitted_record(self) -> None:
        first = tflow.tflow(resp=True)
        first.request.host = "allowed.test"
        first.request.content = b"original request"
        assert first.response is not None
        first.response.content = b"original response"
        excluded = first.copy()
        excluded.id = first.id
        excluded.request.host = "excluded.test"
        excluded.request.content = b"excluded request"
        assert excluded.response is not None
        excluded.response.content = b"excluded response"
        FlowFile.write(self.history, [first, excluded])
        self.master.gateway.set_rules(
            ruleset(l7(GatewayPolicy.BYPASS, "excluded.test"))
        )
        admitted: list[Flow] = []

        def on_add(flow: Flow) -> None:
            admitted.append(flow)

        self.master.view.sig_view_add.connect(on_add)
        imported_ids: set[str] = set()

        self.assertEqual(
            self.facade.load_flow_file(self.history, imported_ids=imported_ids), 2
        )

        stored = self.master.view.get_by_id(first.id)
        self.assertEqual(admitted, [stored])
        assert isinstance(stored, HTTPFlow)
        self.assertEqual(stored.request.host, "allowed.test")
        self.assertEqual(stored.request.raw_content, b"original request")
        assert stored.response is not None
        self.assertEqual(stored.response.raw_content, b"original response")
        self.assertEqual(imported_ids, {first.id})

    def test_admitted_duplicate_keeps_later_sse_metadata_on_stored_flow(self) -> None:
        first = tflow.tflow()
        last = first.copy()
        last.id = first.id
        last.response = tflow.tresp(content=b"data: first\n\ndata: last\n\n")
        last.response.headers["Content-Type"] = "text/event-stream"
        FlowFile.write(self.history, [first, last])

        with mock.patch("ferret.core.mitm.sse.SSE_EVENT_LIMIT", 1):
            self.assertEqual(self.facade.load_flow_file(self.history), 2)

        stored = self.master.view.get_by_id(first.id)
        assert isinstance(stored, HTTPFlow)
        self.assertTrue(stored.metadata[SSE_EVENTS_TRUNCATED_KEY])
        self.assertEqual(
            [event.data for event in self.master.sse.events(first.id)], ["last"]
        )
        assert stored.response is not None
        self.assertEqual(stored.response.raw_content, b"data: first\n\ndata: last\n\n")

    def test_active_recording_import_is_bounded_and_child_tasks_are_recorded(
        self,
    ) -> None:
        external = tflow.tflow(resp=True)
        external.request.path = "/spawned-during-import"
        native_load = self.master.load_flow
        spawned = False

        async def load(flow) -> None:
            nonlocal spawned
            if not spawned:
                spawned = True
                # This task inherits the import ContextVar, but its actual
                # traffic belongs to Save and must not extend the imported file.
                await asyncio.create_task(native_load(external))
            await native_load(flow)

        imported_ids: set[str] = set()
        with mock.patch.object(self.master, "load_flow", load):
            self.assertEqual(
                self.facade.load_flow_file(self.recording, imported_ids=imported_ids), 1
            )
        self.facade.stop_capture_recording()

        self.assertEqual(self._recorded_paths(), ["/before", "/spawned-during-import"])
        self.assertNotIn(external.id, imported_ids)
        self.assertEqual(len(imported_ids), 1)

    def test_unfinished_import_is_not_flushed_into_recording_on_stop(self) -> None:
        unfinished = tflow.tflow()
        unfinished.request.path = "/unfinished-history"
        FlowFile.write(self.history, [unfinished])

        self.assertEqual(self.facade.load_flow_file(self.history), 1)
        self.facade.stop_capture_recording()

        self.assertEqual(self._recorded_paths(), ["/before"])

    def test_import_excludes_native_protocol_completion_and_error_hooks(self) -> None:
        archived = [
            tflow.tflow(err=True),
            tflow.twebsocketflow(),
            tflow.ttcpflow(),
            tflow.ttcpflow(err=True),
            tflow.tudpflow(),
            tflow.tudpflow(err=True),
            tflow.tdnsflow(resp=True),
            tflow.tdnsflow(resp=True, err=True),
        ]
        FlowFile.write(self.history, archived)
        self._call(lambda: self.master.options.update(save_stream_filter=None))

        self.assertEqual(self.facade.load_flow_file(self.history), len(archived))
        self.assertFalse(self.master.save.active_flows)

        external = [
            tflow.twebsocketflow(),
            tflow.ttcpflow(),
            tflow.tudpflow(),
            tflow.tdnsflow(resp=True),
        ]
        for flow in external:
            self._call(lambda flow=flow: self.master.load_flow(flow))
        self.facade.stop_capture_recording()

        recorded = FlowFile.read(self.recording)
        self.assertEqual(len(recorded), 1 + len(external))
        self.assertEqual(
            {flow.id for flow in recorded[1:]}, {flow.id for flow in external}
        )

    def test_partial_stop_failure_retains_only_unwritten_flows_for_retry(self) -> None:
        in_flight = [tflow.tflow(), tflow.tflow()]
        for index, flow in enumerate(in_flight):
            flow.request.path = f"/pending-{index}"
            self._call(lambda flow=flow: self.master.load_flow(flow))
        stream = self.master.save.stream
        assert stream is not None
        native_add = stream.add
        written: list[HTTPFlow] = []

        def fail_second(flow: HTTPFlow) -> None:
            if written:
                raise OSError("disk write failed")
            native_add(flow)
            written.append(flow)

        with (
            mock.patch.object(stream, "add", fail_second),
            self.assertRaisesRegex(OSError, "disk write failed"),
        ):
            self.facade.stop_capture_recording()

        self.assertEqual(self.facade._recording_path, self.recording)
        self.assertEqual(self.master.options.save_stream_file, str(self.recording))
        self.assertIs(self.master.save.stream, stream)
        self.assertEqual(self.master.save.active_flows, set(in_flight) - set(written))

        self.facade.stop_capture_recording()

        paths = self._recorded_paths()
        self.assertEqual(len(paths), 3)
        self.assertEqual(set(paths), {"/before", "/pending-0", "/pending-1"})
        self.assertIsNone(self.facade._recording_path)
        self.assertIsNone(self.master.save.stream)

    def test_close_failure_retains_recording_state_for_retry(self) -> None:
        stream = self.master.save.stream
        assert stream is not None
        with (
            mock.patch.object(stream.fo, "close", side_effect=OSError("close failed")),
            self.assertRaisesRegex(OSError, "close failed"),
        ):
            self.facade.stop_capture_recording()

        self.assertEqual(self.facade._recording_path, self.recording)
        self.assertEqual(self.master.save.current_path, str(self.recording))
        self.assertEqual(self.master.options.save_stream_file, str(self.recording))
        self.assertIs(self.master.save.stream, stream)
        self.facade.stop_capture_recording()
        self.assertEqual(self._recorded_paths(), ["/before"])


if __name__ == "__main__":
    unittest.main()
