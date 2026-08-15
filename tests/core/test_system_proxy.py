import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ferret.core.system_proxy import (
    ProxyEndpoint,
    ProxySnapshot,
    SystemProxyBackend,
    SystemProxyService,
    WindowsSystemProxyBackend,
)


class FakeBackend(SystemProxyBackend):
    def __init__(self) -> None:
        self.current = "original"
        self.restore_calls = 0
        self.fail_set = False
        self.fail_restore = False

    def snapshot(self) -> ProxySnapshot:
        return ProxySnapshot({"current": self.current})

    def set(self, endpoint: ProxyEndpoint) -> bool:
        self.current = endpoint.address
        return not self.fail_set

    def restore(self, snapshot: ProxySnapshot) -> bool:
        self.restore_calls += 1
        if self.fail_restore:
            return False
        self.current = snapshot.values["current"]
        return True

    def owns(self, endpoint: ProxyEndpoint) -> bool:
        return self.current == endpoint.address


class SystemProxyServiceTests(unittest.TestCase):
    def test_attach_and_detach_restore_original_proxy(self) -> None:
        backend = FakeBackend()
        service = SystemProxyService(backend, journal_path=None)

        service.attach("127.0.0.1", 8080)
        self.assertEqual(backend.current, "127.0.0.1:8080")
        self.assertTrue(service.detach())
        self.assertEqual(backend.current, "original")

    def test_external_change_is_not_overwritten(self) -> None:
        backend = FakeBackend()
        service = SystemProxyService(backend, journal_path=None)
        service.attach("127.0.0.1", 8080)
        backend.current = "user-change"

        self.assertTrue(service.detach())
        self.assertEqual(backend.current, "user-change")
        self.assertEqual(backend.restore_calls, 0)

    def test_partial_apply_failure_rolls_back_snapshot(self) -> None:
        backend = FakeBackend()
        backend.fail_set = True
        service = SystemProxyService(backend, journal_path=None)

        with self.assertRaises(RuntimeError):
            service.attach("127.0.0.1", 8080)

        self.assertEqual(backend.current, "original")

    def test_restore_failure_can_be_retried(self) -> None:
        backend = FakeBackend()
        service = SystemProxyService(backend, journal_path=None)
        service.attach("127.0.0.1", 8080)
        backend.fail_restore = True

        self.assertFalse(service.detach())
        self.assertTrue(service.is_attached)
        backend.fail_restore = False
        self.assertTrue(service.detach())
        self.assertFalse(service.is_attached)

    def test_recover_restores_proxy_left_by_previous_process(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)

            recovered = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(recovered.recover())
            self.assertEqual(backend.current, "original")
            self.assertFalse(journal.exists())

    def test_recover_does_not_overwrite_external_proxy_change(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            backend.current = "user-change"

            recovered = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(recovered.recover())
            self.assertEqual(backend.current, "user-change")
            self.assertFalse(journal.exists())


class FakeWinreg:
    HKEY_CURRENT_USER = object()

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def OpenKey(self, *_args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def QueryValueEx(self, _key, name: str):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 0


class WindowsSystemProxyBackendTests(unittest.TestCase):
    def test_missing_auto_detect_value_is_treated_as_disabled(self) -> None:
        backend = WindowsSystemProxyBackend()
        winreg = FakeWinreg(
            {
                "ProxyEnable": 1,
                "ProxyServer": "127.0.0.1:8080",
                "ProxyOverride": "<-loopback>",
            }
        )

        with patch.object(backend, "_winreg", return_value=winreg):
            self.assertTrue(backend.owns(ProxyEndpoint("127.0.0.1", 8080)))


if __name__ == "__main__":
    unittest.main()
