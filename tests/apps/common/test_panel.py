"""延后标签创建不改变 Pivot/stack 的顺序与切换行为。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.panel import TabPanel


class LazyTabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.panel = TabPanel()
        self.addCleanup(self.panel.deleteLater)

    def test_registration_and_inspection_do_not_construct_content(self) -> None:
        first = Mock(side_effect=QWidget)
        second = Mock(side_effect=QWidget)
        self.panel.addLazyTab("first", first, "First")
        self.panel.addLazyTab("second", second, "Second")
        self.assertEqual(self.panel.pivot.currentRouteKey(), "first")
        self.assertIsNone(self.panel.tabWidget("first"))
        first.assert_not_called()
        second.assert_not_called()
        widget = self.panel.activateCurrentTab()
        self.assertIs(widget, self.panel.tabWidget("first"))
        self.panel.setCurrentTab("second")
        self.panel.setCurrentTab("first")
        self.assertIs(widget, self.panel.stacked.currentWidget())
        self.assertEqual(self.panel.stacked.count(), 2)
        first.assert_called_once()
        second.assert_called_once()

    def test_click_after_replacing_a_placeholder_selects_the_cached_widget(
        self,
    ) -> None:
        self.panel.addLazyTab("first", QWidget, "First")
        self.panel.addLazyTab("second", QWidget, "Second")
        self.panel.pivot.items["second"].click()
        second = self.panel.tabWidget("second")
        self.panel.pivot.items["first"].click()
        self.panel.pivot.items["second"].click()
        self.assertIs(second, self.panel.stacked.currentWidget())
        self.panel.setTabVisible("second", False)
        self.assertEqual(self.panel.pivot.currentRouteKey(), "first")

    def test_eager_tabs_keep_their_existing_selection_behavior(self) -> None:
        first, second = QWidget(), QWidget()
        self.panel.addTab("first", first, "First")
        self.panel.addTab("second", second, "Second")
        self.panel.setCurrentTab("second")
        self.assertIs(second, self.panel.stacked.currentWidget())


if __name__ == "__main__":
    unittest.main()
