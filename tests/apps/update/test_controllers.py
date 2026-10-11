"""UpdateController 的信号链测试：FunctionTask 后台编排 + 防重入
（docs/design.md#update）。core.update 的三步在这里一律换成同步假实现，
验证的是「任务 → 信号 → 主线程」这条链，不是 velopack 本身（那归
tests/core/test_update.py）。
"""

from __future__ import annotations

import gc
import os
import threading
import time
import unittest
import weakref

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from ferret.apps.common.tasks import FunctionTask
from ferret.apps.update.controllers import UpdateController
from ferret.core import update as update_core
from tests.core.mitm._qt import wait_until


def _brief() -> update_core.UpdateBrief:
    return update_core.UpdateBrief(
        current="1.0.0",
        target="1.2.3",
        size=1024,
        notes_markdown="",
        release_page="https://example.test/releases/tag/v1.2.3",
    )


class UpdateControllerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.controller = UpdateController()
        self.addCleanup(self.controller.deleteLater)

    def patch_core(self, **overrides) -> None:
        for name, value in overrides.items():
            original = getattr(update_core, name)
            self.addCleanup(setattr, update_core, name, original)
            setattr(update_core, name, value)


class CheckChainTests(UpdateControllerTestBase):
    def test_check_uses_its_own_worker_and_keeps_gui_responsive(self) -> None:
        global_pool = QThreadPool.globalInstance()
        original_max = global_pool.maxThreadCount()
        global_started = threading.Event()
        global_release = threading.Event()
        check_started = threading.Event()
        check_release = threading.Event()
        worker_threads: list[int] = []
        result_threads: list[int] = []
        ticks: list[bool] = []
        gui_thread = threading.get_ident()

        def occupy_global_pool() -> None:
            global_started.set()
            global_release.wait(5)

        def slow_check() -> None:
            worker_threads.append(threading.get_ident())
            check_started.set()
            check_release.wait(5)

        self.patch_core(check=slow_check)
        self.controller.no_update.connect(
            lambda: result_threads.append(threading.get_ident())
        )
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(True))
        blocker = FunctionTask(occupy_global_pool)
        global_pool.setMaxThreadCount(1)
        try:
            global_pool.start(blocker)
            self.assertTrue(wait_until(global_started.is_set))
            timer.start(10)
            self.controller.check()
            self.assertTrue(wait_until(check_started.is_set, timeout_ms=1000))
            self.assertTrue(wait_until(lambda: len(ticks) >= 3, timeout_ms=1000))
            self.assertTrue(self.controller.busy)
            self.assertNotEqual(worker_threads, [gui_thread])
            check_release.set()
            self.assertTrue(wait_until(lambda: result_threads, timeout_ms=1000))
            self.assertEqual(result_threads, [gui_thread])
            self.assertFalse(global_release.is_set())
        finally:
            timer.stop()
            check_release.set()
            global_release.set()
            self.assertTrue(wait_until(lambda: not self.controller.busy))
            self.assertTrue(wait_until(lambda: global_pool.activeThreadCount() == 0))
            global_pool.setMaxThreadCount(original_max)

    def test_finished_tasks_are_collectible(self) -> None:
        self.patch_core(check=lambda: None)
        refs = []
        for _ in range(4):
            self.controller.check()
            refs.extend(weakref.ref(task) for task in self.controller._tasks)
            self.assertTrue(wait_until(lambda: not self.controller.busy))
        gc.collect()
        self.assertTrue(refs)
        self.assertTrue(all(ref() is None for ref in refs))

    def test_update_available_carries_handle_and_brief(self) -> None:
        handle = object()
        self.patch_core(check=lambda: (handle, _brief()))
        seen: list[tuple] = []
        self.controller.update_available.connect(lambda *args: seen.append(args))

        self.controller.check()

        self.assertTrue(wait_until(lambda: seen))
        self.assertIs(seen[0][0], handle)
        self.assertEqual(seen[0][1].target, "1.2.3")

    def test_no_update_emits_no_update(self) -> None:
        self.patch_core(check=lambda: None)
        seen: list = []
        self.controller.no_update.connect(lambda: seen.append(True))

        self.controller.check()

        self.assertTrue(wait_until(lambda: seen))

    def test_result_slot_can_start_download_after_check_cleanup(self) -> None:
        handle = object()
        downloads: list[object] = []
        finished: list[object] = []
        check_finishes: list[bool] = []
        self.patch_core(
            check=lambda: (handle, _brief()),
            download=lambda info, on_progress: downloads.append(info),
        )
        self.controller.update_available.connect(
            lambda info, brief: self.controller.download(info)
        )
        self.controller.download_finished.connect(finished.append)
        self.controller.check_finished.connect(lambda: check_finishes.append(True))

        self.controller.check()

        self.assertTrue(wait_until(lambda: finished))
        self.assertEqual(downloads, [handle])
        self.assertEqual(finished, [handle])
        self.assertEqual(check_finishes, [True])
        self.assertFalse(self.controller.busy)
        self.assertFalse(self.controller._tasks)

    def test_failure_slot_can_retry_immediately(self) -> None:
        calls: list[bool] = []
        success: list[bool] = []
        finishes: list[bool] = []

        def check() -> None:
            calls.append(True)
            if len(calls) == 1:
                raise update_core.UpdateError("network down")

        self.patch_core(check=check)
        self.controller.check_failed.connect(lambda message: self.controller.check())
        self.controller.no_update.connect(lambda: success.append(True))
        self.controller.check_finished.connect(lambda: finishes.append(True))

        with self.assertLogs("ferret.tasks", "ERROR"):
            self.controller.check()
            self.assertTrue(wait_until(lambda: success))

        self.assertEqual(calls, [True, True])
        self.assertEqual(finishes, [True, True])
        self.assertFalse(self.controller.busy)
        self.assertFalse(self.controller._tasks)

    def test_failure_emits_message_and_recovers(self) -> None:
        def _boom():
            raise update_core.UpdateError("检查更新失败：network down")

        self.patch_core(check=_boom)
        seen: list[str] = []
        self.controller.check_failed.connect(seen.append)

        with self.assertLogs("ferret.tasks", "ERROR") as logs:
            self.controller.check()

            self.assertTrue(wait_until(lambda: seen))
            # 失败后 busy 必须复位，否则后续检查全被防重入吞掉。
            self.assertTrue(wait_until(lambda: not self.controller.busy))
        self.assertIn("后台任务失败", logs.output[0])
        self.assertIn("检查更新失败：network down", logs.output[0])
        self.assertIn("network down", seen[0])

    def test_reentrant_check_is_ignored(self) -> None:
        calls: list = []

        def _slow_check():
            calls.append(True)
            time.sleep(0.2)

        self.patch_core(check=_slow_check)

        self.controller.check()
        self.controller.check()  # 任务在飞 → 直接忽略
        # 等慢任务收尾再断言：提前放行会让测试结束时 worker 还在跑，
        # teardown 阶段向已删除的 signals 发信号（stderr 噪音）。
        self.assertTrue(wait_until(lambda: not self.controller.busy))
        self.assertEqual(len(calls), 1)


class DownloadChainTests(UpdateControllerTestBase):
    def test_progress_and_finish(self) -> None:
        def _fake_download(info, on_progress=None):
            if on_progress:
                for pct in (0, 50, 100):
                    on_progress(pct)

        self.patch_core(download=_fake_download)
        progress: list[int] = []
        finished: list = []
        self.controller.download_progress.connect(progress.append)
        self.controller.download_finished.connect(lambda info: finished.append(info))

        handle = object()
        self.controller.download(handle)

        self.assertTrue(wait_until(lambda: finished))
        self.assertIs(finished[0], handle)
        self.assertEqual(progress, [0, 50, 100])

    def test_download_result_can_check_again_without_extra_check_finished(self) -> None:
        self.patch_core(check=lambda: None, download=lambda info, on_progress: None)
        finished: list[bool] = []
        self.controller.download_finished.connect(lambda info: self.controller.check())
        self.controller.check_finished.connect(lambda: finished.append(True))

        self.controller.download(object())

        self.assertTrue(wait_until(lambda: finished))
        self.assertEqual(finished, [True])
        self.assertFalse(self.controller.busy)
        self.assertFalse(self.controller._tasks)

    def test_download_failure_can_retry_with_its_new_handle(self) -> None:
        first, second = object(), object()
        downloads: list[object] = []
        finished: list[object] = []
        check_finishes: list[bool] = []

        def download(info, on_progress) -> None:
            downloads.append(info)
            if info is first:
                raise update_core.UpdateError("connection lost")

        self.patch_core(download=download)
        self.controller.download_failed.connect(
            lambda message: self.controller.download(second)
        )
        self.controller.download_finished.connect(finished.append)
        self.controller.check_finished.connect(lambda: check_finishes.append(True))

        with self.assertLogs("ferret.tasks", "ERROR"):
            self.controller.download(first)
            self.assertTrue(wait_until(lambda: finished))

        self.assertEqual(downloads, [first, second])
        self.assertEqual(finished, [second])
        self.assertEqual(check_finishes, [])
        self.assertFalse(self.controller.busy)
        self.assertFalse(self.controller._tasks)

    def test_apply_failure_emits_apply_failed(self) -> None:
        def _boom(info):
            raise update_core.UpdateError("应用更新失败：denied")

        self.patch_core(apply_and_restart=_boom)
        seen: list[str] = []
        self.controller.apply_failed.connect(seen.append)

        with self.assertLogs("ferret.settings", "WARNING") as logs:
            self.controller.apply_and_restart(object())

        self.assertEqual(
            logs.output,
            ["WARNING:ferret.settings:应用更新失败：应用更新失败：denied"],
        )
        self.assertEqual(seen, ["应用更新失败：denied"])


class BusyStateTests(UpdateControllerTestBase):
    def test_check_and_download_publish_their_busy_state(self) -> None:
        self.patch_core(check=lambda: None, download=lambda info, on_progress: None)
        states: list[tuple[bool, bool]] = []
        self.controller.busy_changed.connect(
            lambda busy: states.append((busy, self.controller.busy))
        )

        self.controller.check()
        self.assertTrue(wait_until(lambda: not self.controller.busy))
        self.controller.download(object())
        self.assertTrue(wait_until(lambda: not self.controller.busy))

        self.assertEqual(
            states, [(True, True), (False, False), (True, True), (False, False)]
        )


if __name__ == "__main__":
    unittest.main()
