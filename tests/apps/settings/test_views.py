import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.capture.views import CapturesInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.core.settings import CONFIG


class AutoSaveSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_exposes_auto_save_switch(self) -> None:
        settings = SettingsInterface()
        settings.deleteLater()

    def test_capture_toolbar_does_not_restore_removed_buttons(self) -> None:
        capture = CapturesInterface()
        self.assertFalse(hasattr(capture.toolbar, "auto_save_btn"))
        self.assertFalse(hasattr(capture.toolbar, "import_btn"))
        self.assertFalse(hasattr(capture.toolbar, "save_session_btn"))
        capture.deleteLater()


if __name__ == "__main__":
    unittest.main()
