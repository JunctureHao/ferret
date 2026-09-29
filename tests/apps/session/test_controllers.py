"""会话控制器测试：批量导出。

FunctionTask 在写池线程上跑，结果经排队信号回主线程 —— 用轮询
processEvents 等信号落袋（AGENTS §1：等信号用轮询原语，不写嵌套事件循环）。
"""

from __future__ import annotations

import os
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


if __name__ == "__main__":
    unittest.main()
