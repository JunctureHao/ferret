import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.core.application import UI_FONT_FAMILY, Application
from ferret.core.settings import CONFIG

# qfluentwidgets 上游默认值，也是本优化要消掉的那个三族列表。
UPSTREAM_DEFAULT = ["Segoe UI", "Microsoft YaHei", "PingFang SC"]


class InitFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # ConfigItem 是 QConfig 的共享类属性，改了会漏给同批次其他用例。
        self._original = CONFIG.get(CONFIG.fontFamilies)

    def tearDown(self) -> None:
        CONFIG.set(CONFIG.fontFamilies, self._original, save=False)

    def test_collapses_ui_font_to_single_family(self) -> None:
        CONFIG.set(CONFIG.fontFamilies, UPSTREAM_DEFAULT, save=False)
        Application()._init_font()
        self.assertEqual(CONFIG.get(CONFIG.fontFamilies), [UI_FONT_FAMILY])

    def test_does_not_write_config_file(self) -> None:
        """`save=False` 是刻意的：字体策略不落盘，方便以后改默认值。

        先塞一个不同的值，否则 `QConfig.set` 开头的 `if item.value == value: return`
        会让这条用例空跑通过。
        """
        CONFIG.set(CONFIG.fontFamilies, UPSTREAM_DEFAULT, save=False)
        with patch.object(CONFIG, "save") as save:
            Application()._init_font()
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
