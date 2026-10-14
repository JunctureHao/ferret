from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.search import SearchHost, merge_history


class MergeHistoryTests(unittest.TestCase):
    def test_new_text_prepends(self) -> None:
        self.assertEqual(merge_history(["b", "a"], "c"), ["c", "b", "a"])

    def test_duplicate_moves_to_front(self) -> None:
        self.assertEqual(merge_history(["c", "b", "a"], "b"), ["b", "c", "a"])

    def test_blank_text_keeps_history(self) -> None:
        self.assertEqual(merge_history(["a"], "  "), ["a"])

    def test_text_is_stripped(self) -> None:
        self.assertEqual(merge_history([], "  ~m GET  "), ["~m GET"])

    def test_limit_truncates_oldest(self) -> None:
        items = [f"k{i}" for i in range(20)]
        merged = merge_history(items, "new")
        self.assertEqual(len(merged), 20)
        self.assertEqual(merged[0], "new")
        self.assertNotIn("k19", merged)

    def test_always_returns_a_new_list(self) -> None:
        items = ["a"]
        merged = merge_history(items, "")
        self.assertIsNot(merged, items)


class SearchHostHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_set_history_replaces_completer_model(self) -> None:
        host = SearchHost()
        self.addCleanup(host.deleteLater)
        host.set_history(["~m GET", "~d example.com"])
        self.assertEqual(host._history_model.stringList(), ["~m GET", "~d example.com"])
        host.set_history(["~m POST"])
        self.assertEqual(host._history_model.stringList(), ["~m POST"])

    def test_history_selection_goes_through_search_requested(self) -> None:
        host = SearchHost()
        self.addCleanup(host.deleteLater)
        host.set_history(["~m GET"])
        received: list[str] = []
        host.search_requested.connect(received.append)
        host.edit.setText("~m GET")
        self.assertEqual(received, ["~m GET"])


if __name__ == "__main__":
    unittest.main()
