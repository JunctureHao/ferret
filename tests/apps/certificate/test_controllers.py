"""Tests for `CertificateController`.

替身服务代替真实的证书业务：不生成证书、不执行 certutil、不碰系统信任库。
每个用例都把后台线程池抽干后再断言，顺带证明活儿确实没在主线程上跑。
"""

import os
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.models import CertificateState
from ferret.core.mitm import (
    CertificateCancelled,
    MitmFacade,
    MitmRuntime,
    SystemCertificateService,
    TrustState,
)

app = QApplication.instance() or QApplication([])


class FakeService:
    """记录调用的替身证书服务。`errors` 用来让指定方法抛异常。"""

    def __init__(self, trust: TrustState = TrustState.ABSENT) -> None:
        self.trust = trust
        self.info: object | None = None
        self.calls: list[str] = []
        self.errors: dict[str, Exception] = {}
        self.certs_dir = Path("C:/ferret-certs")
        self.exported: list[tuple[object, object]] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        error = self.errors.get(name)
        if error is not None:
            raise error

    def trust_state(self) -> TrustState:
        self._record("trust_state")
        return self.trust

    def load(self) -> object | None:
        self.calls.append("load")
        return self.info

    def install(self) -> None:
        self._record("install")
        self.trust = TrustState.TRUSTED

    def uninstall(self) -> None:
        self._record("uninstall")
        self.trust = TrustState.ABSENT

    def regenerate(self) -> None:
        self._record("regenerate")
        self.trust = TrustState.STALE

    def export(self, fmt: object, target: object) -> Path:
        self._record("export")
        self.exported.append((fmt, target))
        return Path(str(target))


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.facade = MitmFacade(MitmRuntime())
        self.controller = CertificateController(
            mitm=self.facade,
            service=cast(SystemCertificateService, self.service),
        )
        self.states: list[CertificateState] = []
        self.busy: list[bool] = []
        self.failures: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.controller.state_changed.connect(self.states.append)
        self.controller.busy_changed.connect(self.busy.append)
        self.controller.operation_failed.connect(
            lambda title, detail: self.failures.append((title, detail))
        )
        self.controller.operation_succeeded.connect(self.messages.append)

    def drain(self, timeout_ms: int = 5000) -> None:
        waited = 0
        while self.controller.busy and waited < timeout_ms:
            app.processEvents()
            QThread.msleep(5)
            waited += 5
        app.processEvents()
        self.assertFalse(self.controller.busy, "后台任务没有在超时前结束")


class RefreshTests(ControllerTestCase):
    def test_starts_out_missing_without_touching_the_service(self) -> None:
        self.assertIs(self.controller.state.trust, TrustState.MISSING)
        self.assertEqual(self.service.calls, [])

    def test_refresh_publishes_the_detected_state(self) -> None:
        self.service.trust = TrustState.TRUSTED
        self.controller.refresh()
        self.drain()
        self.assertIs(self.controller.state.trust, TrustState.TRUSTED)
        self.assertEqual([state.trust for state in self.states], [TrustState.TRUSTED])

    def test_detection_runs_off_the_main_thread(self) -> None:
        self.controller.refresh()
        # 尚未 drain：慢的 certutil 查询还在后台，界面这边状态没变。
        self.assertTrue(self.controller.busy)
        self.assertEqual(self.states, [])
        self.drain()
        self.assertEqual(len(self.states), 1)

    def test_busy_toggles_once_per_operation(self) -> None:
        self.controller.refresh()
        self.drain()
        self.assertEqual(self.busy, [True, False])

    def test_failed_detection_does_not_retry_forever(self) -> None:
        self.service.errors["trust_state"] = OSError("boom")
        with self.assertLogs("ferret.tasks", "ERROR"):
            self.controller.refresh()
            self.drain()
        self.assertEqual(self.service.calls.count("trust_state"), 1)
        self.assertEqual(len(self.failures), 1)
        self.assertIn("boom", self.failures[0][1])

    def test_certs_dir_comes_from_the_service(self) -> None:
        self.assertEqual(self.controller.certs_dir, self.service.certs_dir)


class InstallTests(ControllerTestCase):
    def test_install_then_refreshes_state(self) -> None:
        self.controller.install()
        self.drain()
        self.assertIn("install", self.service.calls)
        self.assertIs(self.controller.state.trust, TrustState.TRUSTED)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.failures, [])

    def test_uninstall_then_refreshes_state(self) -> None:
        self.service.trust = TrustState.TRUSTED
        self.controller.uninstall()
        self.drain()
        self.assertIn("uninstall", self.service.calls)
        self.assertIs(self.controller.state.trust, TrustState.ABSENT)
        self.assertEqual(len(self.messages), 1)

    def test_user_cancelled_install_is_silent(self) -> None:
        """安全警告里点「否」：不报错、不弹成功提示，只把真实状态查回来。"""
        self.service.errors["install"] = CertificateCancelled("安装证书已取消")
        with self.assertNoLogs("ferret.tasks", "ERROR"):
            self.controller.install()
            self.drain()
        self.assertEqual(self.failures, [])
        self.assertEqual(self.messages, [])
        self.assertEqual(self.service.calls.count("trust_state"), 1)
        self.assertEqual(len(self.states), 1)

    def test_user_cancelled_uninstall_is_silent(self) -> None:
        self.service.errors["uninstall"] = CertificateCancelled("卸载证书已取消")
        with self.assertNoLogs("ferret.tasks", "ERROR"):
            self.controller.uninstall()
            self.drain()
        self.assertEqual(self.failures, [])
        self.assertEqual(self.messages, [])
        self.assertEqual(self.service.calls.count("trust_state"), 1)

    def test_failure_reports_and_recovers_state_once(self) -> None:
        self.service.errors["install"] = RuntimeError("certutil 退出码 0x80090011")
        with self.assertLogs("ferret.tasks", "ERROR"):
            self.controller.install()
            self.drain()
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.failures[0][0], "安装失败")
        self.assertIn("0x80090011", self.failures[0][1])
        # 装了一半也要把真实状态查回来，且只补查一次。
        self.assertEqual(self.service.calls.count("trust_state"), 1)
        self.assertEqual(len(self.states), 1)
        self.assertEqual(self.messages, [])

    def test_operations_run_one_at_a_time_in_order(self) -> None:
        # 线程池限一个线程：「先卸旧、再装新」不会互相插队。
        self.service.trust = TrustState.TRUSTED
        self.controller.uninstall()
        self.controller.install()
        self.drain()
        done = [c for c in self.service.calls if c in ("install", "uninstall")]
        self.assertEqual(done, ["uninstall", "install"])


class RegenerateTests(ControllerTestCase):
    def test_regenerate_reloads_the_live_cert_store(self) -> None:
        with mock.patch.object(
            self.facade, "reload_certificate_store", return_value=True
        ) as reload:
            self.controller.regenerate()
            self.drain()
        reload.assert_called_once_with()
        self.assertIn("regenerate", self.service.calls)
        self.assertIs(self.controller.state.trust, TrustState.STALE)
        self.assertEqual(len(self.messages), 1)

    def test_a_failed_hot_reload_does_not_fail_the_operation(self) -> None:
        # 热加载失败无所谓：下次启动内核自然会读到新证书。
        # 只记日志、不弹错：这是「热加载失败无所谓」的唯一可观测证据。
        with (
            mock.patch.object(
                self.facade,
                "reload_certificate_store",
                side_effect=RuntimeError("kernel busy"),
            ),
            self.assertLogs("ferret.certificate", "WARNING"),
        ):
            self.controller.regenerate()
            self.drain()
        self.assertEqual(self.failures, [])
        self.assertEqual(len(self.messages), 1)
        self.assertIs(self.controller.state.trust, TrustState.STALE)

    def test_other_operations_leave_the_store_alone(self) -> None:
        with mock.patch.object(self.facade, "reload_certificate_store") as reload:
            self.controller.install()
            self.drain()
            self.controller.refresh()
            self.drain()
        reload.assert_not_called()


class ExportTests(ControllerTestCase):
    def test_export_passes_format_and_target_to_the_service(self) -> None:
        target = Path("D:/out/Ferret.pem")
        exported: list[object] = []
        self.controller.exported.connect(exported.append)
        self.controller.export("pem", target)
        self.drain()
        self.assertEqual(len(self.service.exported), 1)
        fmt, got_target = self.service.exported[0]
        self.assertEqual(getattr(fmt, "key", None), "pem")
        self.assertEqual(got_target, target)
        self.assertEqual(exported, [target])
        self.assertIn(str(target), self.messages[0])

    def test_unknown_format_fails_without_starting_a_task(self) -> None:
        self.controller.export("docx", Path("D:/out/x"))
        self.assertFalse(self.controller.busy)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(len(self.failures), 1)
        self.assertEqual(self.failures[0][0], "导出失败")

    def test_export_failure_is_reported(self) -> None:
        self.service.errors["export"] = OSError("目标磁盘只读")
        with self.assertLogs("ferret.tasks", "ERROR"):
            self.controller.export("cer", Path("D:/out/x.cer"))
            self.drain()
        self.assertEqual(self.failures[0][0], "导出失败")
        self.assertIn("只读", self.failures[0][1])
        self.assertEqual(self.messages, [])


if __name__ == "__main__":
    unittest.main()
