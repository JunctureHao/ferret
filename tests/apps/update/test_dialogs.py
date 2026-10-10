"""UpdateDialog 的就绪态行为测试。

重点验证「下载完毕主动弹回前台」：MaskDialogBase 是主窗口的子部件，下载期间
用户若点关闭把主窗口收进托盘（minimize_to_tray → hide），本框会跟着藏起来；
``set_ready`` 必须把主窗口唤回前台，否则更新下载完一直缩在托盘里不弹出。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.update.dialogs import UpdateDialog
from ferret.core import update as update_core


def _brief() -> update_core.UpdateBrief:
    return update_core.UpdateBrief(
        "1.0.0", "1.0.1", 128, "", "https://example.test/releases"
    )


class UpdateDialogRevealTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.addCleanup(
            QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete
        )
        self.window = QWidget()
        self.addCleanup(self.window.deleteLater)
        self.window.show()
        self.dialog = UpdateDialog(_brief(), self.window)
        self.addCleanup(self.dialog.deleteLater)

    def test_dialog_lives_inside_the_host_window(self) -> None:
        # 弹回前台的前提：本框是子部件，window() 指向承载它的主窗口而非自身。
        self.assertIs(self.dialog.window(), self.window)

    def test_set_ready_pulls_a_tray_hidden_window_back_to_front(self) -> None:
        self.window.hide()  # closeEvent → minimize_to_tray → hide
        self.assertFalse(self.window.isVisible())

        self.dialog.set_ready()

        self.assertTrue(self.dialog.ready)
        self.assertTrue(self.window.isVisible())


if __name__ == "__main__":
    unittest.main()
