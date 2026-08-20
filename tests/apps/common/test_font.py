import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.font import FontManager


class CodeFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_code_font_requests_exactly_one_family(self) -> None:
        """单族是省下 45MB WS / 20MB Private 的机械前提，见 code_font 的注释。

        断言必须看 `families()`：`family()` 对多族请求也只返回第一项，
        用它检测不出「有人把 fallback 族加回来」这个回归。
        """
        font = FontManager.code_font(10)
        self.assertEqual(font.families(), ["JetBrains Mono"])

    def test_code_font_keeps_monospace_metrics(self) -> None:
        font = FontManager.code_font(13)
        self.assertEqual(font.pointSize(), 13)
        self.assertTrue(font.fixedPitch())


if __name__ == "__main__":
    unittest.main()
