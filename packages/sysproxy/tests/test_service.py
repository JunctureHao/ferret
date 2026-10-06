from __future__ import annotations

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
        # set 在写入系统之前就失败（真实 Windows 的 OpenKey / 首次 SetValueEx
        # 失败）：current 不动。默认 FakeBackend.set 是「先改 current 再返回
        # 失败」，盖不住这条路径（#72 复核重开的正是它）。
        self.fail_set_before_write = False

    def snapshot(self) -> ProxySnapshot:
        return ProxySnapshot({"current": self.current})

    def set(self, endpoint: ProxyEndpoint) -> bool:
        if self.fail_set_before_write:
            return False
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
    def test_applied_journal_write_failure_still_allows_detach(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            service = SystemProxyService(backend, journal_path=journal)
            self.addCleanup(service._release_ownership)
            write = service._write_journal

            def fail_confirmation(endpoint, snapshot, *, applied):
                if applied:
                    raise PermissionError("confirmation denied")
                write(endpoint, snapshot, applied=applied)

            with (
                patch.object(service, "_write_journal", side_effect=fail_confirmation),
                self.assertRaises(RuntimeError),
            ):
                service.attach("127.0.0.1", 8080)
            self.assertTrue(service.is_attached)
            self.assertEqual(backend.current, "127.0.0.1:8080")
            self.assertFalse(json.loads(journal.read_text())["applied"])
            self.assertTrue(service.detach())
            self.assertEqual(backend.current, "original")
            self.assertEqual(backend.restore_calls, 1)
            self.assertFalse(journal.exists())

    def test_snapshot_and_initial_journal_errors_never_change_system_proxy(
        self,
    ) -> None:
        for failing_method in ("snapshot", "_write_journal", "_acquire_ownership"):
            with self.subTest(failing_method=failing_method):
                backend = FakeBackend()
                service = SystemProxyService(backend)
                target = backend if failing_method == "snapshot" else service
                with (
                    patch.object(target, failing_method, side_effect=PermissionError()),
                    patch.object(backend, "set", wraps=backend.set) as set_proxy,
                    self.assertRaises(RuntimeError),
                ):
                    service.attach("127.0.0.1", 8080)
                set_proxy.assert_not_called()
                self.assertFalse(service.is_attached)
                self.assertIsNone(service._lock_fd)

    def test_unreadable_journal_survives_recover_and_attach(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            first._release_ownership()
            original = journal.read_bytes()
            second = SystemProxyService(backend, journal_path=journal)
            with patch.object(Path, "read_text", side_effect=PermissionError()):
                self.assertFalse(second.recover())
                with self.assertRaises(RuntimeError):
                    second.attach("127.0.0.1", 8081)
            self.assertEqual(journal.read_bytes(), original)
            self.assertEqual(backend.current, "127.0.0.1:8080")
            self.assertIsNone(second._lock_fd)
            self.assertTrue(second.recover())

    def test_recovery_reads_the_journal_only_after_acquiring_ownership(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            self.addCleanup(first._release_ownership)
            first.attach("127.0.0.1", 8080)
            recovery = SystemProxyService(backend, journal_path=journal)
            acquire = recovery._acquire_ownership

            def replace_journal_before_acquiring():
                self.assertTrue(first.detach())
                backend.current = "new-user-proxy"
                replacement = SystemProxyService(backend, journal_path=journal)
                try:
                    replacement.attach("127.0.0.1", 8080)
                finally:
                    replacement._release_ownership()
                return acquire()

            with patch.object(
                recovery,
                "_acquire_ownership",
                side_effect=replace_journal_before_acquiring,
            ):
                self.assertTrue(recovery.recover())
            self.assertEqual(backend.current, "new-user-proxy")
            self.assertFalse(journal.exists())
            self.assertIsNone(recovery._lock_fd)

    def test_unlink_failure_preserves_detach_obligation_for_retry(self) -> None:
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            service = SystemProxyService(backend, journal_path=journal)
            service.attach("127.0.0.1", 8080)
            self.addCleanup(service._release_ownership)
            with patch.object(service, "_clear_journal", side_effect=PermissionError()):
                self.assertFalse(service.detach())
            self.assertTrue(service.is_attached)
            self.assertTrue(journal.exists())
            self.assertTrue(service.detach())
            self.assertFalse(service.is_attached)
            self.assertFalse(journal.exists())
            self.assertEqual(backend.current, "original")

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

    def test_set_failure_before_write_then_retry_keeps_original_snapshot(self) -> None:
        """#72 复核重开的主路径：set 未写入系统就失败、回滚也失败，重试不丢原快照。

        journal 在 set 前就以 pending 落盘（端点已是新端点）；set 失败后系统仍指
        旧端点。修复前下一次 attach 把 owns 不匹配当「外部改动」删 journal，唯一
        存着原配置的快照没了，残留死代理被 snapshot() 存成恢复目标——最终系统
        停在死代理上。
        """
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            first._release_ownership()

            backend.fail_restore = True
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())

            # set 未写入 + 回滚失败：journal 保持 pending（新端点 + 原快照）。
            backend.fail_set_before_write = True
            with self.assertRaises(RuntimeError):
                second.attach("127.0.0.1", 8081)
            self.assertEqual(backend.current, "127.0.0.1:8080")
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["snapshot"]["current"], "original")
            self.assertFalse(state["applied"])

            # 恢复权限后重试：系统必须回到 original，而不是 8080 那个死代理。
            backend.fail_restore = False
            backend.fail_set_before_write = False
            second.attach("127.0.0.1", 8081)
            self.assertTrue(second.detach())
            self.assertEqual(backend.current, "original")
            self.assertFalse(journal.exists())

    def test_recover_with_a_pending_journal_restores_or_keeps_it(self) -> None:
        """#72：pending journal（set 从未确认生效）不许按「外部改动」作废。

        恢复得下来 → 清掉；恢复不下来 → 保留 journal 返回 False，等权限回来
        再试。修复前 recover 对 owns 不匹配一律清 journal。
        """
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            backend.fail_restore = True
            backend.fail_set_before_write = True
            first = SystemProxyService(backend, journal_path=journal)
            with self.assertRaises(RuntimeError):
                first.attach("127.0.0.1", 8080)
            # journal pending（8080, original）；系统仍在 original —— set 没写进去。

            backend.fail_set_before_write = False
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())
            self.assertTrue(journal.exists())
            state = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(state["snapshot"]["current"], "original")

            backend.fail_restore = False
            third = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(third.recover())
            self.assertEqual(backend.current, "original")
            self.assertFalse(journal.exists())

    def test_a_legacy_journal_without_applied_keeps_external_change_semantics(
        self,
    ) -> None:
        """老版本写的 journal 没有 applied 键：按已生效读，外部改动作废语义不变。"""
        backend = FakeBackend()
        with TemporaryDirectory() as directory:
            journal = Path(directory) / "proxy.json"
            journal.write_text(
                json.dumps(
                    {
                        "endpoint": {"host": "127.0.0.1", "port": 8080},
                        "snapshot": {"current": "original"},
                    }
                ),
                encoding="utf-8",
            )
            backend.current = "user-change"

            recovered = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(recovered.recover())
            self.assertEqual(backend.current, "user-change")
            self.assertFalse(journal.exists())

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
    KEY_SET_VALUE = 2
    REG_DWORD = 4
    REG_SZ = 1

    def __init__(self, values: dict[str, object]) -> None:
        self.values = values
        self.fail_read = False
        self.fail_write: str | None = None

    def OpenKey(self, *_args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def QueryValueEx(self, _key, name: str):
        if self.fail_read:
            raise PermissionError(name)
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], 0

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        if self.fail_write == name:
            raise PermissionError(name)
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class WindowsSystemProxyBackendTests(unittest.TestCase):
    def test_recover_persists_a_partial_restore_across_another_restart(self) -> None:
        original = {
            "ProxyEnable": 0,
            "ProxyServer": "old",
            "ProxyOverride": "local-domain",
            "AutoConfigURL": "https://example.test/proxy.pac",
            "AutoDetect": 1,
        }
        backend = WindowsSystemProxyBackend()
        registry = FakeWinreg(dict(original))
        with (
            TemporaryDirectory() as directory,
            patch.object(backend, "_winreg", return_value=registry),
            patch.object(backend, "_refresh"),
        ):
            journal = Path(directory) / "proxy.json"
            first = SystemProxyService(backend, journal_path=journal)
            first.attach("127.0.0.1", 8080)
            first._release_ownership()
            registry.fail_write = "ProxyOverride"
            second = SystemProxyService(backend, journal_path=journal)
            self.assertFalse(second.recover())
            self.assertFalse(backend.owns(ProxyEndpoint("127.0.0.1", 8080)))
            self.assertTrue(journal.exists())
            registry.fail_write = None
            third = SystemProxyService(backend, journal_path=journal)
            self.assertTrue(third.recover())
            self.assertEqual(registry.values, original)
            self.assertFalse(journal.exists())

    def test_read_failure_does_not_discard_the_only_recovery_snapshot(self) -> None:
        for operation in ("detach", "recover", "attach"):
            with self.subTest(operation=operation), TemporaryDirectory() as directory:
                backend = WindowsSystemProxyBackend()
                registry = FakeWinreg({"ProxyEnable": 0, "ProxyServer": "old"})
                with (
                    patch.object(backend, "_winreg", return_value=registry),
                    patch.object(backend, "_refresh"),
                ):
                    journal = Path(directory) / "proxy.json"
                    service = SystemProxyService(backend, journal_path=journal)
                    service.attach("127.0.0.1", 8080)
                    self.addCleanup(service._release_ownership)
                    original = journal.read_bytes()
                    if operation != "detach":
                        service._release_ownership()
                        service = SystemProxyService(backend, journal_path=journal)
                    registry.fail_read = True
                    if operation == "attach":
                        with self.assertRaises(RuntimeError):
                            service.attach("127.0.0.1", 8081)
                    else:
                        self.assertFalse(getattr(service, operation)())
                    self.assertEqual(journal.read_bytes(), original)
                    self.assertEqual(registry.values["ProxyServer"], "127.0.0.1:8080")
                    registry.fail_read = False
                    self.assertTrue(
                        service.detach() if operation == "detach" else service.recover()
                    )

    def test_partial_restore_retries_all_values_even_after_endpoint_changed(
        self,
    ) -> None:
        for restart in (False, True):
            with self.subTest(restart=restart), TemporaryDirectory() as directory:
                original = {
                    "ProxyEnable": 1,
                    "ProxyServer": "old-proxy:80",
                    "ProxyOverride": "local-domain",
                    "AutoConfigURL": "https://example.test/proxy.pac",
                    "AutoDetect": 1,
                }
                backend = WindowsSystemProxyBackend()
                registry = FakeWinreg(dict(original))
                with (
                    patch.object(backend, "_winreg", return_value=registry),
                    patch.object(backend, "_refresh"),
                ):
                    journal = Path(directory) / "proxy.json"
                    service = SystemProxyService(backend, journal_path=journal)
                    service.attach("127.0.0.1", 8080)
                    self.addCleanup(service._release_ownership)
                    registry.fail_write = "ProxyOverride"
                    self.assertFalse(service.detach())
                    self.assertEqual(registry.values["ProxyServer"], "old-proxy:80")
                    self.assertNotEqual(registry.values, original)
                    self.assertFalse(json.loads(journal.read_text())["applied"])
                    registry.fail_write = None
                    if restart:
                        service._release_ownership()
                        service = SystemProxyService(backend, journal_path=journal)
                        self.assertTrue(service.recover())
                    else:
                        self.assertTrue(service.detach())
                    self.assertEqual(registry.values, original)
                    self.assertFalse(journal.exists())

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
