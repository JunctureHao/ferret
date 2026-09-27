"""全局搜索框与捕获页 flowfilter 动作的联动。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.filter import CaptureFilterActions, FlowFilterErrorPanel
from ferret.apps.common.search import SearchHost
from tests.core.mitm._qt import wait_for_signal


class CaptureFilterActionsTests(unittest.TestCase):
    """输入仍由全局框持有，捕获页负责防抖与过滤/高亮模式。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.host = SearchHost()
        self.actions = CaptureFilterActions(self.host)
        self.host.search_requested.connect(self.actions.feed)
        self.host.set_actions(self.actions.actions)
        self.actions.bind_anchor(self.host.edit)

    def tearDown(self) -> None:
        self.host.close()
        self.host.deleteLater()
        self.app.processEvents()

    def test_raw_expression_round_trips_without_stripping(self) -> None:
        self.host.edit.setText('  ~u "api/.*"  ')
        self.assertEqual(self.actions.raw_text(), '  ~u "api/.*"  ')

    def test_empty_or_whitespace_expression_means_no_active_filter(self) -> None:
        self.assertFalse(self.actions.has_active_filter())
        self.host.edit.setText("   ")
        self.assertFalse(self.actions.has_active_filter())

    def test_a_typed_expression_activates_the_filter(self) -> None:
        self.host.edit.setText("~m GET")
        self.assertTrue(self.actions.has_active_filter())

    def test_global_clear_empties_the_expression(self) -> None:
        self.host.edit.setText("~m GET")
        self.host.clear_search()
        self.assertEqual(self.actions.raw_text(), "")
        self.assertFalse(self.actions.has_active_filter())

    def test_text_changes_notify_once_after_debounce(self) -> None:
        changed = Mock()
        self.actions.conditionsChanged.connect(changed)
        self.host.edit.setText("~m G")
        self.host.edit.setText("~m GET")
        changed.assert_not_called()
        self.assertEqual(
            wait_for_signal(self.actions.conditionsChanged, timeout_ms=1000), [()]
        )
        changed.assert_called_once_with()
        self.assertEqual(self.actions.raw_text(), "~m GET")

    def test_highlight_action_toggles_the_mode(self) -> None:
        self.assertFalse(self.actions.is_highlight_mode())
        self.actions.highlight_action.trigger()
        self.assertTrue(self.actions.is_highlight_mode())
        self.actions.highlight_action.trigger()
        self.assertFalse(self.actions.is_highlight_mode())

    def test_toggling_highlight_notifies_immediately(self) -> None:
        """过滤与高亮走不同下发路径，切换不能等待输入防抖。"""
        changed = Mock()
        self.actions.conditionsChanged.connect(changed)
        self.actions.highlight_action.trigger()
        changed.assert_called_once_with()

    def test_clearing_the_expression_preserves_highlight_mode(self) -> None:
        self.actions.highlight_action.setChecked(True)
        self.host.edit.setText("~m GET")
        self.host.clear_search()
        self.assertEqual(self.actions.raw_text(), "")
        self.assertTrue(self.actions.is_highlight_mode())

    def test_refilling_the_global_box_does_not_reapply_the_filter(self) -> None:
        self.host.edit.setText("~m GET")
        requested = Mock()
        self.host.search_requested.connect(requested)
        self.host.show_box("flowfilter", self.actions.raw_text())
        requested.assert_not_called()
        self.assertEqual(self.host.edit.text(), "~m GET")

    def test_switching_page_actions_keeps_the_expression_and_highlight_mode(
        self,
    ) -> None:
        self.host.edit.setText("~m GET")
        self.actions.highlight_action.setChecked(True)
        self.host.set_actions([])
        self.host.set_actions(self.actions.actions)
        self.assertEqual(self.host.edit.text(), "~m GET")
        self.assertEqual(self.actions.raw_text(), "~m GET")
        self.assertTrue(self.actions.is_highlight_mode())


class FlowFilterErrorPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.panel = FlowFilterErrorPanel()

    def tearDown(self) -> None:
        self.panel.deleteLater()
        self.app.processEvents()

    def test_error_message_round_trips(self) -> None:
        message = "Expected & or |, found 'x'"
        self.panel.set_raw_error(message)
        self.assertEqual(self.panel.message.text(), message)
        self.panel.set_raw_error("")
        self.assertEqual(self.panel.message.text(), "")

    def test_clear_button_requests_the_host_to_clear_the_expression(self) -> None:
        clear_requested = Mock()
        self.panel.clearRequested.connect(clear_requested)
        self.panel.clear_btn.click()
        clear_requested.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
