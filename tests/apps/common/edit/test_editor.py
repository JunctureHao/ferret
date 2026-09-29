"""代码编辑器的全局主题订阅必须随控件销毁。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, qconfig

from ferret.apps.common.edit.editor import CodeEditor


class EditorLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_theme_change_does_not_call_a_destroyed_editor(self) -> None:
        editor = CodeEditor()
        destroyed: list[bool] = []
        editor.destroyed.connect(lambda: destroyed.append(True))
        editor.deleteLater()
        QCoreApplication.sendPostedEvents(editor, QEvent.Type.DeferredDelete)
        self.assertEqual(destroyed, [True])

        # Qt 槽里的异常走 sys.excepthook，不会从 Signal.emit 抛给调用方。
        with mock.patch("sys.excepthook") as exception_hook:
            qconfig.themeChanged.emit(Theme.DARK)
            qconfig.themeChanged.emit(Theme.LIGHT)

        exception_hook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
