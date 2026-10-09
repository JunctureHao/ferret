"""Failed channel changes must retain usable WireGuard listeners and intent."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, WireGuardDevice
from ferret.core.mitm.bindings import WireGuardServerInstance

from ._qt import start_runtime, wait_until


def _free_port(kind: socket.SocketKind) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _udp_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return False
        return True


class WireGuardRollbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        paths = patch(
            "ferret.core.mitm.runtime.get_certs_dir", return_value=Path(directory.name)
        )
        paths.start()
        self.addCleanup(paths.stop)
        self.port = _free_port(socket.SOCK_DGRAM)
        self.runtime = MitmRuntime(
            listen_port=_free_port(socket.SOCK_STREAM),
            use_wireguard=True,
            wireguard_devices=[WireGuardDevice("phone", "Phone", self.port)],
        )
        self.addCleanup(self.stop_runtime)
        start_runtime(self.runtime)
        self.runtime.set_channels_engaged(True)
        self.assertTrue(
            wait_until(
                lambda: (
                    self.runtime.call(self.runtime.channel_health).get("wireguard")
                    is True
                )
            )
        )

    def stop_runtime(self) -> None:
        self.assertTrue(self.runtime.stop())

    def test_native_option_rejection_restarts_the_previously_closed_listener(
        self,
    ) -> None:
        self.assertFalse(_udp_available(self.port))
        previous = self.runtime._channel_intents()
        # The individual specs are valid; the native configure hook rejects
        # reverse and regular sharing a TCP address after WG teardown began.
        with self.assertRaises(ValueError):
            self.runtime.apply_channels(
                use_wireguard=False,
                use_reverse=True,
                reverse_target="http://example.com",
                reverse_port=self.runtime.listen_port,
            )
        self.assertEqual(self.runtime._channel_intents(), previous)
        self.assertTrue(self.runtime.call(self.runtime.channel_health)["wireguard"])
        # Native is_running only checks its handle list. Checking the socket
        # catches stale closed handles falsely reported as a healthy listener.
        self.assertFalse(_udp_available(self.port))

    def test_key_preparation_failure_restores_all_channel_intents(self) -> None:
        self.runtime.apply_channels(use_wireguard=False)
        previous = self.runtime._channel_intents()
        with (
            patch.object(
                self.runtime,
                "_prepare_wireguard_keys",
                side_effect=OSError("disk full"),
            ),
            self.assertRaises(OSError),
        ):
            self.runtime.apply_channels(use_wireguard=True, use_socks5=True)
        self.assertEqual(self.runtime._channel_intents(), previous)
        self.assertNotIn("wireguard", self.runtime.call(self.runtime.channel_health))
        self.assertTrue(_udp_available(self.port))

    def test_cancelled_rotation_drains_old_handle_before_restoring_listener(
        self,
    ) -> None:
        runtime = self.runtime
        previous = runtime.wireguard_devices
        master = runtime.master
        assert master is not None
        original_call = runtime.call
        old_server = original_call(
            lambda: next(
                server
                for server in master.proxyserver.servers
                if isinstance(server, WireGuardServerInstance)
            )
        )
        original_handle = old_server._servers[0]
        drain_started = Event()
        drained = Event()
        restore_started = Event()
        release_drain = asyncio.Event()

        class HeldHandle:
            # Keep the real listener and actual Rust shutdown. Only acknowledgement
            # is delayed, reproducing cancellation before graceful close finishes.
            sockets: tuple[HeldHandle, ...]

            def __init__(self) -> None:
                self.sockets = (self,)

            def close(self) -> None:
                original_handle.close()

            def getsockname(self):
                return original_handle.getsockname()

            async def wait_closed(self) -> None:
                await original_handle.wait_closed()
                drain_started.set()
                await release_drain.wait()
                drained.set()

        original_stop = WireGuardServerInstance._stop
        original_start = WireGuardServerInstance._start

        async def delayed_stop(server: WireGuardServerInstance) -> None:
            await original_stop(server)
            # This delay lets the short call deadline cancel a submission while
            # its native removal is in flight. It is fault injection, not a
            # readiness wait; observations below use the shared Qt polling helper.
            if server is old_server:
                await asyncio.sleep(0.2)

        async def observed_start(server: WireGuardServerInstance) -> None:
            restore_started.set()
            self.assertTrue(drained.is_set(), "listener rebound before close completed")
            await original_start(server)

        def short_call(callback, *, timeout=5.0):
            return original_call(callback, timeout=0.05)

        loop = original_call(asyncio.get_running_loop)
        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(old_server, "_servers", [HeldHandle()]),
            patch.object(WireGuardServerInstance, "_stop", delayed_stop),
            patch.object(WireGuardServerInstance, "_start", observed_start),
            patch.object(runtime, "call", short_call),
        ):
            pending = executor.submit(
                runtime.apply_wireguard_devices,
                [replace(previous[0], key_revision=previous[0].key_revision + 1)],
            )
            try:
                self.assertTrue(wait_until(drain_started.is_set, timeout_ms=5000))
                self.assertFalse(pending.done())
                self.assertFalse(restore_started.is_set())
            finally:
                loop.call_soon_threadsafe(release_drain.set)
            with self.assertRaises(TimeoutError):
                pending.result(timeout=5)
        self.assertTrue(drained.is_set())
        self.assertTrue(restore_started.is_set())
        self.assertEqual(runtime.wireguard_devices, previous)
        self.assertFalse(runtime._wireguard_changing)
        self.assertTrue(runtime.call(runtime.wireguard_device_health)[previous[0].id])
        self.assertFalse(_udp_available(self.port))


class WireGuardSubmissionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_key_preparation_rejects_concurrent_edits_and_failure_releases_guard(
        self,
    ) -> None:
        runtime = MitmRuntime(wireguard_devices=[])
        wanted = [WireGuardDevice("phone", "Phone", enabled=False)]
        entered = Event()
        release = Event()

        def prepare() -> None:
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release blocked key preparation")
            raise OSError("disk full")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(runtime, "_prepare_wireguard_keys", side_effect=prepare),
        ):
            pending = executor.submit(runtime.apply_wireguard_devices, wanted)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ValueError):
                    runtime.apply_wireguard_devices([])
            finally:
                release.set()
            with self.assertRaises(OSError):
                pending.result(timeout=5)
        self.assertEqual(runtime.wireguard_devices, ())
        self.assertFalse(runtime._wireguard_changing)
        # Disabled devices require no filesystem work. A subsequent legitimate
        # edit must be accepted, proving both the flag and lock were released.
        runtime.apply_wireguard_devices(wanted)
        self.assertEqual(runtime.wireguard_devices, tuple(wanted))
        self.assertFalse(runtime._wireguard_changing)
