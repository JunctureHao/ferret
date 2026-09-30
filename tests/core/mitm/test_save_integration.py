from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.addons.save import Save
from mitmproxy.test import taddons, tflow

from ferret.core.mitm import CaptureMaster, FlowFile, HTTPFlow, MitmFacade, MitmRuntime
from ferret.core.mitm.bindings import FlowReadException


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
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = CaptureMaster(event_loop=self.loop)
        self.addCleanup(self.master.save.done)
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
        stream = self.master.save.stream
        assert stream is not None
        stream.fo.flush()

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
        first = self.recording
        self.facade.stop_capture_recording()

        second = self.facade.start_capture_recording()
        self.addCleanup(self.facade.stop_capture_recording)
        self.assertNotEqual(second, first)
        self.assertTrue(second.exists())  # 占位文件已建（Save 写入前是空的）
        self._record("/again")
        self.facade.stop_capture_recording()

        # 第一段的正文原样保留，没有落在第二段的文件里。
        self.assertEqual(self._recorded_paths(), ["/before"])
        second_paths = []
        for flow in FlowFile.read(second):
            assert isinstance(flow, HTTPFlow)
            second_paths.append(flow.request.path)
        self.assertEqual(second_paths, ["/again"])


if __name__ == "__main__":
    unittest.main()
