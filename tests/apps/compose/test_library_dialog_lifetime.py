"""真实请求库菜单点击回归：用子进程隔离原生崩溃，并限制卡死的等待时间。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import MessageBox, RoundMenu

from ferret.apps.compose import library
from ferret.apps.compose.models import SavedRequest
from ferret.apps.compose.store import ComposeRequestStore


def _click_and_close(operation: str, close_action: str) -> None:
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    events: list[str] = []
    dialogs = []
    modalities = []
    outcomes = []

    with tempfile.TemporaryDirectory() as directory:
        store = ComposeRequestStore(path=Path(directory) / "requests.json")
        target = SavedRequest.create(
            name="请求",
            method="GET",
            url="https://example.test/",
            folder="文件夹" if operation in ("folder_delete", "folder_rename") else "",
        )
        other = SavedRequest.create(
            name="其他请求", method="POST", url="https://example.test/other"
        )
        store.upsert(target)
        store.upsert(other)
        panel = library.ComposeLibraryPanel(store)
        panel.resize(800, 600)
        panel.show()

        dialog_type = (
            MessageBox if operation.endswith("delete") else library._NameDialog
        )

        def create_dialog(*args, **kwargs):
            events.append("dialog opened")
            dialog = dialog_type(*args, **kwargs)
            dialogs.append(dialog)
            if not isinstance(dialog, MessageBox):
                dialog.name_edit.setText("新名称")

            def close_dialog():
                modalities.append(dialog.windowModality())
                if close_action == "accept":
                    dialog.yesButton.click()
                elif close_action == "cancel":
                    dialog.cancelButton.click()
                else:
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)

            dialog.destroyed.connect(lambda: events.append("dialog destroyed"))
            dialog.destroyed.connect(lambda: QTimer.singleShot(0, finish))
            # 等淡入完成，再走真实按钮或 Esc 关闭路径。
            QTimer.singleShot(300, close_dialog)
            return dialog

        def finish():
            events.append("responsive after close")
            outcomes.append((store.get(target.id), store.folders()))
            assert store.get(other.id) == other
            assert store.flush()
            reloaded = ComposeRequestStore(path=store.path)
            assert reloaded.requests() == store.requests()
            reloaded.deleteLater()
            panel.close()
            panel.deleteLater()
            store.deleteLater()
            QTimer.singleShot(0, app.quit)

        def click_menu(menu):
            action_text = {
                "request_delete": "删除",
                "folder_delete": "删除文件夹",
                "request_rename": "重命名",
                "folder_rename": "重命名",
                "folder_new": "新建文件夹",
            }[operation]
            action = next(a for a in menu.menuActions() if a.text() == action_text)
            item = action.property("item")
            QTest.mouseClick(
                menu.view.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                menu.view.visualItemRect(item).center(),
            )
            events.append("click returned")

        def open_menu():
            item = panel.tree.topLevelItem(0)
            assert item is not None
            pos = (
                QPoint(20, panel.tree.viewport().height() - 20)
                if operation == "folder_new"
                else panel.tree.visualItemRect(item).center()
            )
            panel._on_context_menu(pos)
            menu = next(m for m in panel.findChildren(RoundMenu) if m.isVisible())
            menu.destroyed.connect(lambda: events.append("menu destroyed"))
            QTimer.singleShot(300, lambda: click_menu(menu))

        QTimer.singleShot(0, open_menu)
        with patch.object(library, dialog_type.__name__, side_effect=create_dialog):
            app.exec()

    assert events.index("click returned") < events.index("dialog destroyed"), events
    assert events.index("click returned") < events.index("menu destroyed"), events
    assert events.index("dialog destroyed") < events.index("menu destroyed"), events
    assert "responsive after close" in events, events
    assert modalities == [Qt.WindowModality.ApplicationModal], modalities
    assert len(dialogs) == 1
    assert not shiboken6.isValid(dialogs[0])
    assert not shiboken6.isValid(panel)
    assert len(outcomes) == 1, outcomes
    request, folders = outcomes[0]
    accepted = close_action == "accept"
    if operation == "request_delete":
        assert request == (None if accepted else target), outcomes
    elif operation == "folder_delete":
        assert request is not None
        assert request.folder == ("" if accepted else target.folder), outcomes
        assert folders == ([] if accepted else [target.folder]), outcomes
    elif operation == "request_rename":
        assert request is not None and request.name == "新名称", outcomes
    else:
        assert folders == ["新名称"], outcomes


class LibraryDialogLifetimeTests(unittest.TestCase):
    def test_real_menu_click_dialog_close_and_window_teardown(self):
        cases = [
            (operation, close_action)
            for operation in ("request_delete", "folder_delete")
            for close_action in ("accept", "cancel", "escape")
        ] + [
            (operation, "accept")
            for operation in ("request_rename", "folder_rename", "folder_new")
        ]
        for operation, close_action in cases:
            with self.subTest(operation=operation, close_action=close_action):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-X",
                        "faulthandler",
                        "-m",
                        "tests.apps.compose.test_library_dialog_lifetime",
                        operation,
                        close_action,
                    ],
                    capture_output=True,
                    text=True,
                    # 与 test_column_dialog_lifetime 同一处境：子进程几乎零 CPU、纯 IO
                    # （导入 PySide6/qfluentwidgets/mitmproxy），全量套件满载时页缓存
                    # 紧张可拉长一个数量级；30s 会误杀，90s 兜底防挂死。
                    timeout=90,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    _click_and_close(sys.argv[1], sys.argv[2])
