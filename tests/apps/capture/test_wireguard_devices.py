"""Device management keeps core work off the GUI thread and preserves identity."""

from __future__ import annotations

import os
import threading
import unittest
from dataclasses import replace
from typing import cast
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem, QWidget

from ferret.apps.capture import wireguard
from ferret.apps.capture.views import ProxyPortDialog, WireGuardConfigDialog
from ferret.core.mitm import MitmFacade, WireGuardDevice
from ferret.core.network import ANY_HOST
from tests.core.mitm._qt import wait_until


class _Facade:
    def __init__(self) -> None:
        self.wireguard_devices = [
            WireGuardDevice("phone_a", "Phone A", 51820),
            WireGuardDevice("phone_b", "Phone B", 51821),
        ]
        self.channels_engaged = False
        self.use_wireguard = True
        self.health: dict[str, bool | str] = {}
        self.calls: list[tuple[str, object, int]] = []
        self.block: threading.Event | None = None
        self.save_error: Exception | None = None

    def wireguard_device_health(self) -> dict[str, bool | str]:
        self.calls.append(("health", None, threading.get_ident()))
        return dict(self.health)

    def set_wireguard_devices(self, devices: list[WireGuardDevice]) -> None:
        self.calls.append(("save", list(devices), threading.get_ident()))
        if self.block is not None:
            self.block.wait(5)
        if self.save_error is not None:
            raise self.save_error
        self.wireguard_devices = list(devices)

    def new_wireguard_device(self, name: str) -> WireGuardDevice:
        self.calls.append(("new", name, threading.get_ident()))
        return WireGuardDevice("phone_c", name, 51822)

    def rotate_wireguard_device(self, device_id: str) -> None:
        self.calls.append(("rotate", device_id, threading.get_ident()))
        self.wireguard_devices = [
            replace(device, key_revision=device.key_revision + 1)
            if device.id == device_id
            else device
            for device in self.wireguard_devices
        ]

    def wireguard_client_config(self, device_id: str) -> str:
        self.calls.append(("qr", device_id, threading.get_ident()))
        return f"[Interface]\nPrivateKey = {device_id}\n"


class WireGuardDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(1000, 800)
        self.host.show()
        self.facade = _Facade()
        self.gui_thread = threading.get_ident()
        self.config = mock.Mock()
        self.config.wireguard_devices = object()
        self.config.get.return_value = [{"old": "configuration"}]
        self.config_threads: list[int] = []
        self.config.set.side_effect = lambda *args, **kwargs: (
            self.config_threads.append(threading.get_ident())
        )
        patcher = mock.patch.object(wireguard, "CONFIG", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dialog = wireguard.WireGuardDevicesDialog(
            cast(MitmFacade, self.facade), self.host
        )
        self.dialog.show()
        self.assertTrue(
            wait_until(lambda: self.dialog._loaded and self.dialog._task is None)
        )
        self.dialog._timer.stop()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.facade.block is not None:
            self.facade.block.set()
        wait_until(lambda: self.dialog._task is None and self.dialog._poll is None)
        self.dialog.reject()
        self.dialog.deleteLater()
        self.host.close()
        self.host.deleteLater()
        self.app.processEvents()

    def _finish(self) -> None:
        self.assertTrue(
            wait_until(lambda: self.dialog._task is None and self.dialog._poll is None)
        )

    def _item(self, row: int, column: int) -> QTableWidgetItem:
        item = self.dialog.table.item(row, column)
        assert item is not None
        return item

    def test_opening_and_qr_do_not_engage_channels_and_choose_device(self) -> None:
        self.assertFalse(self.facade.channels_engaged)
        self.assertEqual(self._item(0, 3).text(), "未开始抓包")
        self.assertEqual(self._item(1, 3).text(), "未开始抓包")
        self.dialog.table.selectRow(1)
        with mock.patch("ferret.apps.capture.views.WireGuardConfigDialog") as preview:
            self.dialog._show_qr()
            self._finish()
        self.assertFalse(self.facade.channels_engaged)
        self.assertEqual(
            preview.call_args.args[0], "[Interface]\nPrivateKey = phone_b\n"
        )
        self.assertEqual(preview.call_args.kwargs["device_name"], "Phone B")
        self.assertEqual([item[0] for item in self.facade.calls], ["health", "qr"])
        self.assertTrue(all(item[2] != self.gui_thread for item in self.facade.calls))
        self.config.set.assert_not_called()

    def test_status_is_per_device_and_reports_listener_failure(self) -> None:
        self.facade.channels_engaged = True
        self.facade.health = {"phone_a": True, "phone_b": "UDP 51821 is already in use"}
        self.dialog.refresh()
        self._finish()
        self.assertEqual(self._item(0, 3).text(), "监听就绪")
        self.assertEqual(self._item(1, 3).text(), "监听失败")
        self.assertIn("51821", self._item(1, 3).toolTip())
        self.assertNotIn("在线", self._item(0, 3).text())

    def test_toggle_saves_on_gui_thread_and_keeps_other_identity(self) -> None:
        original = list(self.facade.wireguard_devices)
        self.dialog.table.selectRow(1)
        self.dialog._toggle_device()
        self._finish()
        self.assertEqual(self.facade.wireguard_devices[0], original[0])
        self.assertEqual(
            self.facade.wireguard_devices[1], replace(original[1], enabled=False)
        )
        self.assertEqual(self._item(1, 3).text(), "未监听")
        self.assertTrue(all(item[2] != self.gui_thread for item in self.facade.calls))
        self.assertEqual(self.config_threads, [self.gui_thread])
        self.assertFalse(self.facade.channels_engaged)

    def test_save_failure_rolls_back_runtime_and_in_memory_setting(self) -> None:
        original = list(self.facade.wireguard_devices)

        def save(*args, **kwargs) -> None:
            if kwargs.get("save") is not False:
                raise OSError("disk unavailable")

        self.config.set.side_effect = save
        self.dialog.table.selectRow(1)
        self.dialog._toggle_device()
        self._finish()
        self.assertEqual(self.facade.wireguard_devices, original)
        self.assertEqual(self.dialog._devices, original)
        self.config.set.assert_any_call(
            self.config.wireguard_devices, [{"old": "configuration"}], save=False
        )
        self.assertIn("disk unavailable", self.dialog.error_label.text())
        self.assertEqual(
            len([item for item in self.facade.calls if item[0] == "save"]), 2
        )

    def test_core_failure_does_not_persist_proposed_devices(self) -> None:
        self.facade.save_error = ValueError(
            "multiple devices cannot use sticky sessions"
        )
        original = list(self.facade.wireguard_devices)
        self.dialog._toggle_device()
        self._finish()
        self.config.set.assert_not_called()
        self.assertEqual(self.dialog._devices, original)
        self.assertIn("sticky sessions", self.dialog.error_label.text())

    def test_rotation_targets_selected_device_and_keeps_other_key_revision(
        self,
    ) -> None:
        original = list(self.facade.wireguard_devices)
        self.dialog.table.selectRow(1)
        with mock.patch.object(self.dialog, "_confirm", return_value=True):
            self.dialog._rotate_device()
            self._finish()
        self.assertEqual(self.facade.wireguard_devices[0], original[0])
        self.assertEqual(
            self.facade.wireguard_devices[1], replace(original[1], key_revision=1)
        )
        self.assertEqual(self.config.set.call_args.args[1][1]["key_revision"], 1)

    def test_cancelled_add_does_not_create_or_save_identity(self) -> None:
        with mock.patch.object(wireguard, "WireGuardDeviceDialog") as editor:
            editor.return_value.exec.return_value = False
            self.dialog._add_device()
            self._finish()
        self.assertEqual(len(self.facade.wireguard_devices), 2)
        self.config.set.assert_not_called()
        self.assertEqual([item[0] for item in self.facade.calls], ["health", "new"])

    def test_added_device_keeps_existing_records_and_uses_its_own_port(self) -> None:
        original = list(self.facade.wireguard_devices)
        new_device = WireGuardDevice("phone_c", "Phone C", 51822)
        with mock.patch.object(wireguard, "WireGuardDeviceDialog") as editor:
            editor.return_value.exec.return_value = True
            editor.return_value.device.return_value = new_device
            self.dialog._add_device()
            self._finish()
        self.assertEqual(self.facade.wireguard_devices, [*original, new_device])
        self.assertEqual(self.dialog._selected_device(), new_device)
        self.assertFalse(self.facade.channels_engaged)
        self.config.set.assert_called_once()

    def test_delete_removes_only_selected_device_and_persists_remaining_list(
        self,
    ) -> None:
        original = list(self.facade.wireguard_devices)
        self.dialog.table.selectRow(0)
        with mock.patch.object(self.dialog, "_confirm", return_value=True):
            self.dialog._delete_device()
            self._finish()
        self.assertEqual(self.facade.wireguard_devices, [original[1]])
        self.assertEqual(self.config.set.call_args.args[1][0]["id"], "phone_b")
        self.assertEqual(self.dialog.table.rowCount(), 1)

    def test_close_waits_for_active_device_update(self) -> None:
        self.facade.block = threading.Event()
        self.dialog._toggle_device()
        self.dialog.reject()
        self.assertIsNotNone(self.dialog._task)
        self.assertTrue(self.dialog._close_requested)
        self.assertTrue(self.dialog.isVisible())
        self.facade.block.set()
        self._finish()
        self.assertTrue(wait_until(lambda: not self.dialog.isVisible()))
        self.config.set.assert_called_once()

    def test_settings_manage_devices_without_applying_channel_checkbox(self) -> None:
        settings = ProxyPortDialog(
            8080,
            self.host,
            use_local=False,
            use_wireguard=False,
            wireguard_facade=cast(MitmFacade, self.facade),
        )
        self.addCleanup(settings.deleteLater)
        settings.show()
        self.app.processEvents()
        self.assertTrue(settings.wireguard_config_btn.isVisible())
        self.assertFalse(settings.get_use_wireguard())
        with mock.patch("ferret.apps.capture.views.WireGuardDevicesDialog") as manager:
            settings._show_wireguard_config()
        manager.assert_called_once_with(self.facade, settings.window())
        self.assertFalse(self.facade.channels_engaged)

    def test_reverse_conflict_uses_enabled_device_udp_ports(self) -> None:
        settings = ProxyPortDialog(
            8080,
            self.host,
            use_local=False,
            use_wireguard=True,
            use_reverse=True,
            reverse_target="https://example.com",
            reverse_port=51821,
            wireguard_facade=cast(MitmFacade, self.facade),
        )
        self.addCleanup(settings.deleteLater)
        settings.show()
        self.app.processEvents()
        self.assertIn("51821", settings.reverse_hint.text())
        self.assertTrue(settings.reverse_hint.isVisible())
        settings.reverse_target_edit.setText("http://example.com")
        settings._sync_exposure()
        self.assertFalse(settings.reverse_hint.isVisible())

    def test_empty_or_disabled_devices_do_not_suspend_source_limits_or_auth(
        self,
    ) -> None:
        original = list(self.facade.wireguard_devices)
        for devices in ([], [replace(device, enabled=False) for device in original]):
            with self.subTest(devices=len(devices)):
                self.facade.wireguard_devices = devices
                settings = ProxyPortDialog(
                    8080,
                    self.host,
                    listen_host=ANY_HOST,
                    use_local=False,
                    use_wireguard=True,
                    proxyauth_enabled=True,
                    wireguard_facade=cast(MitmFacade, self.facade),
                )
                self.addCleanup(settings.deleteLater)
                self.assertTrue(settings.block_private_check.isEnabled())
                self.assertTrue(settings.proxyauth_check.isEnabled())
                self.assertTrue(settings.proxyauth_cred_row.isEnabled())

    def test_qr_preview_names_device_and_explains_single_device_use(self) -> None:
        preview = WireGuardConfigDialog(
            "[Interface]\nPrivateKey = test\n", self.host, device_name="Phone B"
        )
        self.addCleanup(preview.deleteLater)
        self.assertIn("Phone B", preview.title_label.text())
        self.assertIn("仅供一台设备", preview.desc_label.text())
        self.assertIn("IPv4", preview.desc_label.text())
        self.assertFalse(preview.yesButton.isVisible())


if __name__ == "__main__":
    unittest.main()
