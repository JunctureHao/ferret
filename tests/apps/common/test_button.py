import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from qfluentwidgets import ToolTipFilter

from ferret.apps.common.button import TransparentTooltipButton


class TooltipFilterTests(unittest.TestCase):
    """#56：tooltip filter 只装一次，重设 tooltip 不许线性累积 filter。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 最早注册（LIFO 最后跑）：控件 deleteLater 之后把事件清干净。
        self.addCleanup(self.app.processEvents)

    def test_repeated_setToolTip_installs_exactly_one_filter(self) -> None:
        button = TransparentTooltipButton()
        self.addCleanup(button.deleteLater)

        for text in ("旧提示", "新提示", "排序：时间"):
            button.setToolTip(text)

        filters = button.findChildren(ToolTipFilter)
        self.assertEqual(len(filters), 1)
        self.assertEqual(button.toolTip(), "排序：时间")

    def test_the_first_setToolTip_still_installs_the_filter(self) -> None:
        button = TransparentTooltipButton()
        self.addCleanup(button.deleteLater)

        button.setToolTip("一次就够")

        self.assertEqual(len(button.findChildren(ToolTipFilter)), 1)
