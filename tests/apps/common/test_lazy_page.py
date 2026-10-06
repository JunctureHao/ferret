import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ferret.apps.common.lazy_page import LazyPage
from ferret.apps.common.search import is_searchable


class _SearchablePage(QWidget):
    """实现 titlebar 搜索三方法的最小页（is_searchable 的鸭子判定目标）。"""

    def search_placeholder(self) -> str:
        return "p"

    def apply_search(self, text: str) -> None:
        self._last = text

    def current_search_text(self) -> str:
        return "t"


def _nested_widget(page: LazyPage) -> QWidget | None:
    item = page._layout.itemAt(0)
    return item.widget() if item else None


class LazyPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_ensure_builds_once_and_nests_real_page(self) -> None:
        calls = []

        def factory() -> QWidget:
            calls.append(1)
            return QLabel()

        page = LazyPage(factory, "DemoInterface")
        self.assertEqual(page.objectName(), "DemoInterface")

        first = page.ensure()
        second = page.ensure()
        self.assertIs(first, second)
        self.assertEqual(calls, [1])
        self.assertIs(_nested_widget(page), first)
        page.close()

    def test_attributes_delegate_and_missing_raises(self) -> None:
        page = LazyPage(_SearchablePage, "DemoInterface")

        # 真实页未构造前委托属性不可用——is_searchable 等鸭子判定靠它落空。
        with self.assertRaises(AttributeError):
            page.search_placeholder  # noqa: B018

        page.ensure()
        self.assertEqual(page.search_placeholder(), "p")
        page.apply_search("x")
        with self.assertRaises(AttributeError):
            page.no_such_member  # noqa: B018
        page.close()

    def test_duck_typed_search_detection_follows_real_page(self) -> None:
        plain = LazyPage(QLabel, "PlainInterface")
        searchable = LazyPage(_SearchablePage, "SearchInterface")

        self.assertFalse(is_searchable(plain))
        self.assertFalse(is_searchable(searchable))
        searchable.ensure()
        self.assertTrue(is_searchable(searchable))
        plain.close()
        searchable.close()

    def test_on_ensure_hook_runs_once_after_build(self) -> None:
        seen = []
        page = LazyPage(QLabel, "DemoInterface", on_ensure=seen.append)
        page.ensure()
        page.ensure()
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], _nested_widget(page))
        page.close()


if __name__ == "__main__":
    unittest.main()
