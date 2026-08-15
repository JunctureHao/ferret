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
                context.options.save_stream_file = str(path)
                flow = tflow.tflow(resp=True)
                addon.request(flow)
                addon.response(flow)
                context.options.save_stream_file = None

            self.assertEqual(len(FlowFile.read(path)), 1)


if __name__ == "__main__":
    unittest.main()
