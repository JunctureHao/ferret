"""会话控制器测试：批量导出。

FunctionTask 在写池线程上跑，结果经排队信号回主线程 —— 用轮询
processEvents 等信号落袋（AGENTS §1：等信号用轮询原语，不写嵌套事件循环）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication

from ferret.apps.session.controllers import SessionController
from ferret.apps.session.repository import SessionRepository

app = QApplication.instance() or QApplication([])


class ExportSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.out = tmp / "out"
        self.out.mkdir()
        self.repo = SessionRepository(root=tmp / "repo")
        self.controller = SessionController(repository=self.repo)
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.controller.operation_succeeded.connect(self.succeeded.append)
        self.controller.operation_failed.connect(
            lambda title, detail: self.failed.append((title, detail))
        )

    def _make_sessions(self, *names: str):
        for name in names:
            self.repo.create(name, [tflow.tflow(resp=True)])
        return self.repo.list_all()

    def _wait_signals(self, count: int) -> None:
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and (len(self.succeeded) + len(self.failed)) < count
        ):
            app.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            app.processEvents()

    def test_batch_export_writes_one_flow_per_session(self) -> None:
        metas = self._make_sessions("甲", "乙")

        self.controller.export_sessions(metas, self.out)
        self._wait_signals(1)

        # 每个会话按自己的名字落一个 .flow；会话名在仓库内唯一，同批互不覆盖。
        self.assertEqual(
            sorted(p.name for p in self.out.glob("*.flow")), ["乙.flow", "甲.flow"]
        )
        self.assertEqual(self.failed, [])
        self.assertEqual(len(self.succeeded), 1)

    def test_a_missing_session_does_not_sink_the_batch(self) -> None:
        self._make_sessions("甲", "乙")
        metas = self.repo.list_all()
        bad = next(m for m in metas if m.name == "甲")
        bad.path.unlink()

        self.controller.export_sessions(metas, self.out)
        self._wait_signals(1)

        # 幸存者照常落盘；失败的会话逐条报进 operation_failed，不整批失败。
        self.assertEqual([p.name for p in self.out.glob("*.flow")], ["乙.flow"])
        self.assertEqual(self.succeeded, [])
        self.assertEqual(len(self.failed), 1)
        self.assertIn("甲", self.failed[0][1])


class OpenSessionTests(unittest.TestCase):
    """打开会话：旧 vc 不挂在长期存活的 SessionController 上（controllers.py:217）。

    挂 parent=self 会让 Qt 父-子所有权把整份流量副本钉到进程退出；
    这里用弱引用钉住「连续开两个会话后，第一个 vc 已可被 GC 回收」。
    """

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.repo = SessionRepository(root=tmp / "repo")
        self.controller = SessionController(repository=self.repo)
        self.opened: list = []
        self.controller.session_opened.connect(
            lambda meta, vc: self.opened.append((meta, vc))
        )

    def _open(self, name: str):
        self.repo.create(name, [tflow.tflow(resp=True)])
        sid = self.repo.list_all()[0].session_id
        self.controller.open_session(sid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not self.opened:
            app.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            app.processEvents()
        self.assertEqual(len(self.opened), 1)
        return self.opened.pop()

    def test_reopening_releases_previous_view_controller(self) -> None:
        import gc
        import weakref

        _, vc1 = self._open("会话甲")
        ref1 = weakref.ref(vc1)
        self.assertIsNotNone(ref1())

        _, vc2 = self._open("会话乙")
        self.assertIsNot(vc1, vc2)

        # SessionViewerPage 没接上时，vc1 除弱引用外已无人持有，须可被回收。
        del vc1, vc2
        gc.collect()
        self.assertIsNone(ref1())


class DuplicateIdRecoveryTests(unittest.TestCase):
    """#79：录制文件可能含同 id 的未完成+完成两条（录制中导入历史文件等场景）。

    原生 View.add 只收首次出现的 id，不去重的话打开会话展示的永远是没响应的
    那条 —— load_flows 按 id 取**最后**一条（最完整），位置仍按首次出现排。
    """

    def test_duplicate_ids_keep_the_last_and_most_complete_entry(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = SessionRepository(root=tmp / "repo")

        premature = tflow.tflow()  # 在途时被冲进文件的那条：无响应
        complete = tflow.tflow(resp=True)  # 收尾后的完整条
        complete.id = premature.id
        other = tflow.tflow(resp=True)
        other.request.path = "/other"
        repo.create("双条", [premature, complete, other])

        flows = repo.load_flows(repo.list_all()[0].session_id)
        self.assertEqual([f.id for f in flows], [premature.id, other.id])
        self.assertIsNotNone(flows[0].response)

    def test_a_file_without_duplicates_is_returned_as_is(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        repo = SessionRepository(root=tmp / "repo")
        flows_in = [tflow.tflow(resp=True), tflow.tflow(resp=True)]
        repo.create("普通", flows_in)

        flows = repo.load_flows(repo.list_all()[0].session_id)
        self.assertEqual([f.id for f in flows], [f.id for f in flows_in])


class TaskLifecycleTests(unittest.TestCase):
    """异步任务对象随完成整体释放。

    FunctionTask 的 signals 上挂着持 task 的闭包，task → signals → Qt 连接 → 闭包
    → task 是横跨 C++ 边界的引用环，gc 收不掉；_run 在完成回调里断开连接拆环，
    run() 收尾时放开入参引用（save_capture 的整份流量副本就靠这一步释放）。
    """

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.repo = SessionRepository(root=tmp / "repo")
        self.controller = SessionController(repository=self.repo)

    def _pump(self, seconds: float = 0.2) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        for _ in range(20):
            app.processEvents()

    def test_task_objects_are_released_after_open(self) -> None:
        import gc

        from ferret.apps.common.tasks import FunctionTask, WorkerSignals

        self.repo.create("会话甲", [tflow.tflow(resp=True)])
        sid = self.repo.list_all()[0].session_id

        # 全量跑套件时别的模块可能留着自己的任务对象，只关心本次新增的是否归零。
        gc.collect()
        before_tasks = {id(o) for o in gc.get_objects() if isinstance(o, FunctionTask)}
        before_signals = {
            id(o) for o in gc.get_objects() if isinstance(o, WorkerSignals)
        }

        done: list = []
        self.controller.session_opened.connect(lambda meta, vc: done.append(vc))
        self.controller.open_session(sid)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not done:
            app.processEvents()
            time.sleep(0.01)
        self._pump()
        gc.collect()

        new_tasks = [
            o
            for o in gc.get_objects()
            if isinstance(o, FunctionTask) and id(o) not in before_tasks
        ]
        new_signals = [
            o
            for o in gc.get_objects()
            if isinstance(o, WorkerSignals) and id(o) not in before_signals
        ]
        self.assertEqual(new_tasks, [])
        self.assertEqual(new_signals, [])

    def test_save_does_not_pin_flows_after_completion(self) -> None:
        import gc
        import weakref

        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.content = b"x" * 1024
        ref = weakref.ref(flow)

        created: list = []
        self.controller.session_created.connect(created.append)
        self.controller.save_capture("会话甲", [flow])
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not created:
            app.processEvents()
            time.sleep(0.01)
        self._pump()

        del flow
        gc.collect()
        self.assertIsNone(ref())


if __name__ == "__main__":
    unittest.main()
