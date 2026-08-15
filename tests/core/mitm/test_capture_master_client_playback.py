"""Tests for CaptureMaster exposing the ClientPlayback addon instance."""

import asyncio
import unittest

from mitmproxy.addons.clientplayback import ClientPlayback
from mitmproxy.addons.readfile import ReadFile
from mitmproxy.addons.save import Save

from ferret.core.mitm import CaptureMaster


class CaptureMasterClientPlaybackTests(unittest.TestCase):
    def test_capture_master_holds_client_playback_instance(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            self.assertIsInstance(master.client_playback, ClientPlayback)
        finally:
            loop.close()

    def test_client_playback_registered_in_addons_chain(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            self.assertIn(master.client_playback, master.addons.chain)
        finally:
            loop.close()

    def test_client_playback_lookup_by_name(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            looked_up = master.addons.get("clientplayback")
            self.assertIs(looked_up, master.client_playback)
        finally:
            loop.close()

    def test_save_addon_still_registered(self) -> None:
        """Regression: ensure adding client_playback reference didn't evict Save."""
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            self.assertIsInstance(master.save, Save)
            self.assertIn(master.save, master.addons.chain)
        finally:
            loop.close()

    def test_readfile_addon_is_registered(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            master = CaptureMaster(event_loop=loop)
            self.assertIsInstance(master.readfile, ReadFile)
            self.assertIn(master.readfile, master.addons.chain)
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
