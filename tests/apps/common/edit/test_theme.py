"""``edit/theme.py`` 的取色测试。

``EditorPalette`` 是编辑器配色的唯一出处，重构前颜色散在四个文件里且互相打架
（highlighter 按 hex 字符串比对给暗色表打补丁、editor 每帧现算 QColor、
查找命中色写死在 widgets）。这里锁住三件事：

1. ``Token.Text`` 不设前景色——报文里绝大多数字符是 Text，必须继承 widget
   的主题色，一旦设死就会在浅色主题下白底白字。
2. 格式缓存以 ``(是否暗色, token)`` 为键，主题切换后自动拿到新表，
   不依赖任何人记得先调 ``invalidate()``。
3. 查找命中色**不**随明暗反转（黄/橙是"命中"的通用语义）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextFormat
from PySide6.QtWidgets import QApplication
from qfluentwidgets import Theme, isDarkTheme, setTheme

from ferret.apps.common.edit.syntax import (
    Binary,
    Comment,
    MaterialLightStyle,
    MaterialStyle,
    Number,
    StringKey,
    Text,
    Token,
)
from ferret.apps.common.edit.theme import EditorPalette


class PaletteTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._original = Theme.DARK if isDarkTheme() else Theme.LIGHT
        EditorPalette.invalidate()

    def tearDown(self) -> None:
        setTheme(self._original, save=False)
        EditorPalette.invalidate()


class StyleSelectionTests(PaletteTestCase):
    def test_style_follows_theme(self) -> None:
        setTheme(Theme.DARK, save=False)
        self.assertIs(EditorPalette.style(), MaterialStyle)
        setTheme(Theme.LIGHT, save=False)
        self.assertIs(EditorPalette.style(), MaterialLightStyle)


class TokenFormatTests(PaletteTestCase):
    def test_text_token_has_no_foreground(self) -> None:
        """Text 必须继承 widget 前景色，否则浅色主题下正文会白底白字。"""
        for theme in (Theme.DARK, Theme.LIGHT):
            with self.subTest(theme=theme):
                setTheme(theme, save=False)
                fmt = EditorPalette.token_format(Text)
                self.assertFalse(
                    fmt.hasProperty(QTextFormat.Property.ForegroundBrush)
                )

    def test_text_subtypes_also_inherit(self) -> None:
        setTheme(Theme.DARK, save=False)
        fmt = EditorPalette.token_format(Token.Text.Whitespace)
        self.assertFalse(fmt.hasProperty(QTextFormat.Property.ForegroundBrush))

    def test_colored_token_uses_theme_palette(self) -> None:
        setTheme(Theme.DARK, save=False)
        dark = EditorPalette.token_format(StringKey).foreground().color().name()
        setTheme(Theme.LIGHT, save=False)
        light = EditorPalette.token_format(StringKey).foreground().color().name()

        self.assertEqual(dark.upper(), MaterialStyle.blue.upper())
        self.assertEqual(light.upper(), MaterialLightStyle.blue.upper())
        self.assertNotEqual(dark, light)

    def test_font_flags_are_applied(self) -> None:
        setTheme(Theme.DARK, save=False)
        self.assertTrue(EditorPalette.token_format(Comment).fontItalic())
        self.assertTrue(EditorPalette.token_format(Binary).fontItalic())
        self.assertFalse(EditorPalette.token_format(Number).fontItalic())

    def test_format_is_cached_and_shared(self) -> None:
        """缓存全进程共享：原实现每个 highlighter 各持一份，一个详情页 4 份重复。"""
        setTheme(Theme.DARK, save=False)
        first = EditorPalette.token_format(Number)
        second = EditorPalette.token_format(Number)
        self.assertIs(first, second)

    def test_theme_switch_does_not_need_invalidate(self) -> None:
        """缓存键含主题，所以切换后必然拿到新对象——不依赖谁记得先 invalidate()。"""
        setTheme(Theme.DARK, save=False)
        dark = EditorPalette.token_format(Number)
        setTheme(Theme.LIGHT, save=False)
        light = EditorPalette.token_format(Number)
        self.assertIsNot(dark, light)
        self.assertNotEqual(
            dark.foreground().color().name(), light.foreground().color().name()
        )

    def test_invalidate_clears_cache(self) -> None:
        setTheme(Theme.DARK, save=False)
        first = EditorPalette.token_format(Number)
        EditorPalette.invalidate()
        self.assertIsNot(EditorPalette.token_format(Number), first)


class DecorationColorTests(PaletteTestCase):
    def test_mono_decorations_follow_theme(self) -> None:
        """自绘装饰走"暗色叠半透明白 / 亮色叠半透明黑"，不写死色值。"""
        setTheme(Theme.DARK, save=False)
        self.assertEqual(EditorPalette.line_number_normal().red(), 255)
        setTheme(Theme.LIGHT, save=False)
        self.assertEqual(EditorPalette.line_number_normal().red(), 0)

    def test_active_line_number_is_stronger_than_normal(self) -> None:
        for theme in (Theme.DARK, Theme.LIGHT):
            with self.subTest(theme=theme):
                setTheme(theme, save=False)
                self.assertGreater(
                    EditorPalette.line_number_active().alpha(),
                    EditorPalette.line_number_normal().alpha(),
                )

    def test_current_line_and_divider_are_translucent(self) -> None:
        """当前行底色必须半透明，否则会盖掉卡片底色/语法色。"""
        for theme in (Theme.DARK, Theme.LIGHT):
            with self.subTest(theme=theme):
                setTheme(theme, save=False)
                for color in (
                    EditorPalette.current_line(),
                    EditorPalette.gutter_divider(),
                ):
                    self.assertGreater(color.alpha(), 0)
                    self.assertLess(color.alpha(), 255)

    def test_current_line_is_fainter_than_divider(self) -> None:
        setTheme(Theme.DARK, save=False)
        self.assertLess(
            EditorPalette.current_line().alpha(),
            EditorPalette.gutter_divider().alpha(),
        )


class SearchColorTests(PaletteTestCase):
    def test_match_colors_stay_warm_in_both_themes(self) -> None:
        """黄/橙是"命中"的通用语义，反转成蓝紫会读不懂——两个主题都必须偏暖。"""
        for theme in (Theme.DARK, Theme.LIGHT):
            setTheme(theme, save=False)
            for name, color in (
                ("match", EditorPalette.search_match()),
                ("current", EditorPalette.search_current()),
            ):
                with self.subTest(theme=theme, which=name):
                    self.assertGreater(color.red(), color.blue())
                    self.assertGreater(color.green(), color.blue())

    def test_current_match_is_more_opaque_than_others(self) -> None:
        for theme in (Theme.DARK, Theme.LIGHT):
            with self.subTest(theme=theme):
                setTheme(theme, save=False)
                self.assertGreater(
                    EditorPalette.search_current().alpha(),
                    EditorPalette.search_match().alpha(),
                )

    def test_match_colors_are_translucent(self) -> None:
        """命中色是铺在正文上的 ExtraSelection，全不透明会盖掉文字。"""
        setTheme(Theme.DARK, save=False)
        self.assertLess(EditorPalette.search_match().alpha(), 255)
        self.assertLess(EditorPalette.search_current().alpha(), 255)


class TreeCountColorTests(PaletteTestCase):
    def test_tree_count_is_a_valid_color_per_theme(self) -> None:
        setTheme(Theme.DARK, save=False)
        dark = EditorPalette.tree_count()
        setTheme(Theme.LIGHT, save=False)
        light = EditorPalette.tree_count()

        self.assertTrue(dark.isValid())
        self.assertTrue(light.isValid())
        self.assertEqual(dark.name().upper(), MaterialStyle.gray.upper())
        self.assertEqual(light.name().upper(), MaterialLightStyle.gray.upper())


if __name__ == "__main__":
    unittest.main()
