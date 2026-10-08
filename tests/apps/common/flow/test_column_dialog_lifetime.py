"""真实菜单点击的生命周期回归：原生崩溃隔离在子进程，超时也不挂住测试套件。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from qfluentwidgets import RoundMenu

from ferret.apps.common.flow import views
from ferret.apps.common.flow.column_settings import ColumnSettingsDialog
from ferret.apps.common.flow.columns import default_layout, logical_index


def _click_and_close(grouping: str, close_action: str) -> None:
    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    events: list[str] = []
    dialogs: list[ColumnSettingsDialog] = []
    outcomes: list[tuple[bool, bool]] = []
    modalities: list[Qt.WindowModality] = []

    with (
        patch.object(views, "load_layout", return_value=default_layout()),
        patch.object(views, "save_layout") as save,
    ):
        pane = views.FlowViewerPane()
        pane.set_grouping_mode(grouping)
        view = pane.tree if grouping == "conn" else pane.table
        assert view is not None
        pane.resize(1200, 700)
        # 空数据默认显示占位页；菜单交互必须来自实际显示的表头。
        pane.table_stack.setCurrentWidget(view)
        pane.show()

        def create_dialog(*args):
            events.append("dialog opened")
            dialog = ColumnSettingsDialog(*args)
            dialogs.append(dialog)
            # 确定保存、取消和 Esc 丢弃，均要走实际按钮/按键关闭路径。
            for row in range(dialog.list_widget.count()):
                item = dialog.list_widget.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == "size":
                    item.setCheckState(Qt.CheckState.Unchecked)

            def close_dialog():
                modalities.append(dialog.windowModality())
                if close_action == "accept":
                    dialog.yesButton.click()
                elif close_action == "cancel":
                    dialog.cancelButton.click()
                else:
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)

            dialog.destroyed.connect(lambda: events.append("dialog destroyed"))
            # 弹窗释放后再退出宿主，覆盖完整关闭和窗口销毁链。
            dialog.destroyed.connect(lambda: QTimer.singleShot(0, finish))
            # 淡入结束后再关闭，覆盖正常交互而非仅仅构造后立即 reject。
            QTimer.singleShot(300, close_dialog)
            return dialog

        def finish():
            events.append("responsive after close")
            outcomes.append((view.isColumnHidden(logical_index("size")), save.called))
            pane.close()
            pane.deleteLater()
            QTimer.singleShot(0, app.quit)

        def click_menu(menu):
            item = menu.view.item(0)
            save.reset_mock()
            QTest.mouseClick(
                menu.view.viewport(),
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
                menu.view.visualItemRect(item).center(),
            )
            events.append("click returned")

        def open_menu():
            view._on_header_menu(QPoint(10, 10))
            menu = next(m for m in view.findChildren(RoundMenu) if m.isVisible())
            menu.destroyed.connect(lambda: events.append("menu destroyed"))
            QTimer.singleShot(300, lambda: click_menu(menu))

        # 保留真实应用事件循环及鼠标分发栈；直接调用 action.trigger 或弹窗入口
        # 无法覆盖 QListWidget 鼠标事件在 exec 内被销毁后继续退栈的崩溃。
        QTimer.singleShot(0, open_menu)
        with patch.object(views, "ColumnSettingsDialog", side_effect=create_dialog):
            app.exec()

    assert events.index("click returned") < events.index("dialog destroyed"), events
    assert events.index("dialog destroyed") < events.index("menu destroyed"), events
    assert "responsive after close" in events, events
    assert outcomes == [(close_action == "accept", close_action == "accept")], outcomes
    assert modalities == [Qt.WindowModality.ApplicationModal]
    assert len(dialogs) == 1
    assert not shiboken6.isValid(dialogs[0])
    assert not shiboken6.isValid(pane)


class ColumnDialogLifetimeTests(unittest.TestCase):
    def test_menu_click_dialog_close_and_window_teardown(self):
        for grouping in ("flat", "conn"):
            for close_action in ("accept", "cancel", "escape"):
                with self.subTest(grouping=grouping, close_action=close_action):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-X",
                            "faulthandler",
                            "-m",
                            "tests.apps.common.flow.test_column_dialog_lifetime",
                            grouping,
                            close_action,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )


if __name__ == "__main__":
    _click_and_close(sys.argv[1], sys.argv[2])
