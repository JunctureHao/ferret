import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sysproxy import (
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
            # 模拟上一进程崩溃：OS 关句柄、锁随进程死亡自动释放。
            first._release_ownership()

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
            first._release_ownership()

            recovered = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(recovered.recover())
            self.assertEqual(backend.current, "user-change")
            self.assertFalse(journal.exists())

    def test_recover_skipped_while_another_instance_owns_the_proxy(self) -> None:
        """#73：所有者还活着时，第二个实例不得把它挂着的代理恢复掉。"""
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)

            second = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(second.recover())
            self.assertEqual(backend.current, "127.0.0.1:8080")
            self.assertTrue(journal.exists())
            # 第一实例对自己的代理一无所知，detach 语义不受影响。
            self.assertTrue(first.is_attached)
            self.assertTrue(first.detach())
            self.assertEqual(backend.current, "original")
            self.assertFalse(journal.exists())

    def test_attach_rejected_while_another_instance_owns_the_proxy(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)

            second = SystemProxyService(backend, journal_path=journal)
            with self.assertRaises(RuntimeError):
                second.attach("127.0.0.1", 8081)
            self.assertEqual(backend.current, "127.0.0.1:8080")
            self.assertIsNone(second.endpoint)

            self.assertTrue(first.detach())
            # 所有者退场后锁已释放，后续实例可以正常接管。
            second.attach("127.0.0.1", 8081)
            self.assertEqual(backend.current, "127.0.0.1:8081")
            self.assertTrue(second.detach())

    def test_recover_failure_then_attach_keeps_original_snapshot(self) -> None:
        """#72：recover 失败后再 attach，journal 里的必须是用户原快照。

        修复前：attach 对「当前系统代理」（= 上一进程残留的死地址）重新快照并
        覆写 journal，用户原配置彻底丢失——detach 后系统仍指向死代理。
        """
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            # #72 场景的前提就是上一进程已崩溃：锁随进程死亡释放。
            first._release_ownership()

            backend.fail_restore = True
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())
            self.assertTrue(journal.exists())

            backend.fail_restore = False
            second.attach("127.0.0.1", 8081)
            self.assertEqual(backend.current, "127.0.0.1:8081")
            self.assertTrue(second.detach())
            self.assertEqual(backend.current, "original")
            self.assertFalse(journal.exists())

    def test_attach_with_restore_still_failing_carries_original_forward(self) -> None:
        """#72：残留快照一直恢复不下来时，journal 也必须继续带原快照。"""
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            first._release_ownership()

            backend.fail_restore = True
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())
            second.attach("127.0.0.1", 8081)
            self.assertEqual(backend.current, "127.0.0.1:8081")
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["snapshot"]["current"], "original")

            backend.fail_restore = False
            self.assertTrue(second.detach())
            self.assertEqual(backend.current, "original")

    def test_set_failure_with_rollback_failure_keeps_original_in_journal(self) -> None:
        """#72：set 失败且回滚也失败时，原快照同样不能被新 attach 覆盖。"""
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            first._release_ownership()

            backend.fail_restore = True
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())

            backend.fail_set = True
            with self.assertRaises(RuntimeError):
                second.attach("127.0.0.1", 8081)
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["snapshot"]["current"], "original")
            self.assertFalse(second.is_attached)

            backend.fail_set = False
            backend.fail_restore = False
            second.attach("127.0.0.1", 8081)
            self.assertTrue(second.detach())
            self.assertEqual(backend.current, "original")

    def test_service_does_not_import_the_host_or_qt(self) -> None:
        """零依赖是这个包存在的意义：源码里不许出现宿主与 Qt 的 import。"""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "sysproxy"
        banned = ("ferret", "PySide6")
        for path in root.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    for name in banned:
                        self.assertNotIn(
                            f"{name}.",
                            stripped,
                            f"{path.name} imports {name}: {stripped}",
                        )
                        self.assertNotIn(
                            f"import {name}",
                            stripped,
                            f"{path.name} imports {name}: {stripped}",
                        )


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
