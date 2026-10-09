"""Application startup/shutdown recovery failures reach the GUI boundary."""

from __future__ import annotations

import os
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.wireguard import WireGuardDevice
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import CONFIG


class ApplicationRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.kernel = Mock()
        self.proxy = Mock()
        self.facade = Mock()
        with (
            patch.object(
                ApplicationRuntime, "_build_mitm_runtime", return_value=self.kernel
            ),
            patch("ferret.core.runtime.SystemProxyService", return_value=self.proxy),
            patch("ferret.core.runtime.MitmFacade", return_value=self.facade),
        ):
            self.runtime = ApplicationRuntime()

    def test_failed_startup_recovery_is_reported_and_does_not_disable_the_kernel(
        self,
    ) -> None:
        self.proxy.recover.return_value = False
        messages = []
        self.runtime.startup_error.connect(messages.append)
        with patch("ferret.core.runtime.log"):
            self.runtime.start()
        self.assertEqual(messages, [self.runtime.last_startup_error])
        self.assertIn("恢复记录已保留", messages[0])
        self.kernel.start.assert_called_once()
        self.proxy.recover.return_value = True
        self.runtime.start()
        self.assertEqual(self.runtime.last_startup_error, "")
        self.assertEqual(len(messages), 1)

    def test_failed_shutdown_exposes_a_retryable_reason(self) -> None:
        self.proxy.detach.return_value = False
        self.assertFalse(self.runtime.shutdown())
        self.assertIn("系统代理恢复失败", self.runtime.last_shutdown_error)
        self.kernel.stop.assert_not_called()
        self.proxy.detach.return_value = True
        self.kernel.stop.return_value = False
        self.assertFalse(self.runtime.shutdown())
        self.assertIn("内核尚未停止", self.runtime.last_shutdown_error)
        self.kernel.stop.return_value = True
        self.assertTrue(self.runtime.shutdown())
        self.assertEqual(self.runtime.last_shutdown_error, "")

    def test_resume_after_failed_apply_reruns_the_shutdown_chain(self) -> None:
        self.proxy.detach.return_value = True
        self.kernel.stop.return_value = True
        self.assertTrue(self.runtime.shutdown())
        self.assertTrue(self.runtime._shutdown)

        self.runtime.resume_after_failed_apply()
        self.assertFalse(self.runtime._shutdown)
        self.proxy.detach.reset_mock()
        self.kernel.stop.reset_mock()
        self.assertTrue(self.runtime.shutdown())
        self.proxy.detach.assert_called_once()
        self.kernel.stop.assert_called_once()
        self.assertTrue(self.runtime._shutdown)

    def test_recording_failure_keeps_the_kernel_alive_for_an_exit_retry(self) -> None:
        self.proxy.detach.return_value = True
        self.facade.stop_capture_recording.side_effect = OSError("disk full")
        with patch("ferret.core.runtime.log"):
            self.assertFalse(self.runtime.shutdown())
        self.kernel.stop.assert_not_called()
        self.assertIn("disk full", self.runtime.last_shutdown_error)
        self.assertFalse(self.runtime._shutdown)

        self.facade.stop_capture_recording.side_effect = None
        self.kernel.stop.return_value = True
        self.assertTrue(self.runtime.shutdown())
        self.assertEqual(self.facade.stop_capture_recording.call_count, 2)
        self.kernel.stop.assert_called_once()
        self.assertEqual(self.runtime.last_shutdown_error, "")


class WireGuardStartupRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = QCoreApplication.instance() or QCoreApplication([])
        previous = deepcopy(CONFIG.wireguard_devices.value)
        warnings = CONFIG.load_warnings[:]
        self.addCleanup(setattr, CONFIG.wireguard_devices, "value", previous)
        self.addCleanup(setattr, CONFIG, "load_warnings", warnings)

    def build(self):
        with (
            patch("ferret.core.runtime.MitmRuntime") as kernel,
            patch("ferret.core.runtime.MitmFacade"),
            patch("ferret.core.runtime.SystemProxyService"),
            patch("ferret.core.runtime.log"),
        ):
            ApplicationRuntime()
        return kernel.call_args.kwargs

    def test_legacy_and_explicit_empty_registries_remain_distinct_at_startup(
        self,
    ) -> None:
        CONFIG.wireguard_devices.value = None
        self.assertEqual(
            self.build()["wireguard_devices"], [WireGuardDevice("default", "")]
        )
        CONFIG.wireguard_devices.value = []
        self.assertEqual(self.build()["wireguard_devices"], [])

    def test_saved_devices_are_passed_to_kernel_with_stable_keys_and_ports(
        self,
    ) -> None:
        devices = [
            WireGuardDevice("phone-a", "A"),
            WireGuardDevice("phone-b", "B", 51821, False, 2),
        ]
        CONFIG.wireguard_devices.value = [device.to_dict() for device in devices]
        self.assertEqual(self.build()["wireguard_devices"], devices)

    def test_corrupt_registry_reports_failure_without_reviving_legacy_peer(
        self,
    ) -> None:
        CONFIG.wireguard_devices.value = [{"id": "phone-a"}]
        self.assertEqual(self.build()["wireguard_devices"], [])
        self.assertEqual(CONFIG.wireguard_devices.value, [])
        self.assertEqual(CONFIG.load_warnings[-1][0], "wireguard")
        self.assertIn("已停用设备接入", CONFIG.recovery_messages()[-1])
