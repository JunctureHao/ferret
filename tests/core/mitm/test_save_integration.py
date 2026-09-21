import asyncio
import tempfile
import unittest
from pathlib import Path

from mitmproxy.addons.save import Save
from mitmproxy.test import taddons, tflow

from ferret.core.mitm import CaptureMaster, FlowFile


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


if __name__ == "__main__":
    unittest.main()
