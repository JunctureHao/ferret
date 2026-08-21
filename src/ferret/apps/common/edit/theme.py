"""编辑器配色的唯一出处：Qt 侧按主题取色，``syntax.py`` 侧只管调色板数据。

重构前颜色散在四个文件里且互相打架：

* ``highlighter.py`` 用 hex 字符串比对给**单一**暗色调色板打补丁
  （``c3e88d``→``388E3C``、``89ddff``→``00ACC1``），且不分主题一律改；
* ``highlighter.py`` 另一处又把所有字符串强制成 ``#107C10``（在死类里，永不生效）；
* ``editor.py::line_number_area_paint_event`` 每帧现算 ``QColor(255, 255, 255, 120)``；
* 查找命中色写死在 ``widgets.py::_apply_search_highlight``。

现在 ``syntax.py`` 提供 ``MaterialStyle``(暗) / ``MaterialLightStyle``(亮) 两套完整
调色板，本模块负责按 ``isDarkTheme()`` 选表、转 ``QColor`` / ``QTextCharFormat``。

``QTextCharFormat`` 按 ``(是否暗色, token)`` 缓存且**全进程共享**——原实现每个
highlighter 实例各持一份 ``format_cache``，一个 flow 详情页开 4 个编辑器就是 4 份
重复的格式对象。缓存键带主题，所以主题切换后拿到的必然是新表（无需谁记得先
``invalidate()``）；调用方只需 ``rehighlight()`` 让 Qt 重绘。
"""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtGui import QColor, QFont, QTextCharFormat
from qfluentwidgets import isDarkTheme

from .syntax import MaterialLightStyle, MaterialStyle, TokenType

# 自绘装饰统一走“暗色叠半透明白 / 亮色叠半透明黑”，与 splitter.py::BaseHandle 一致：
# 不写死具体色值，底图（卡片色、主题色）换了也不会突然对不上。
_LINE_NUMBER_ACTIVE_ALPHA = 255  # 当前行行号：全不透明
_LINE_NUMBER_NORMAL_ALPHA = 120  # 其余行行号：压暗，避免与正文抢注意力
_GUTTER_DIVIDER_ALPHA_DARK = 40
_GUTTER_DIVIDER_ALPHA_LIGHT = 30
_CURRENT_LINE_ALPHA_DARK = 15  # 当前行底色：极淡，只要能看出"光标在这行"
_CURRENT_LINE_ALPHA_LIGHT = 10

# 查找命中色是少数**不能**跟随明暗反转的颜色：黄/橙是“命中”的通用语义，
# 反转成蓝紫会读不懂。这里只按主题微调饱和度与明度。
_SEARCH_MATCH_DARK = (255, 235, 120, 120)
_SEARCH_MATCH_LIGHT = (255, 235, 0, 120)
_SEARCH_CURRENT_DARK = (255, 160, 40, 180)
_SEARCH_CURRENT_LIGHT = (255, 150, 0, 180)


class EditorPalette:
    """编辑器用到的全部颜色与字符格式。

    全为类方法：颜色只取决于“当前主题”这一个进程级状态，没有需要随实例变化的
    部分，也因此格式缓存可以在所有编辑器之间共享。
    """

    _format_cache: ClassVar[dict[tuple[bool, str], QTextCharFormat]] = {}

    # —— 调色板选择 ——

    @staticmethod
    def style() -> type[MaterialStyle]:
        """当前主题对应的 token 调色板。"""
        return MaterialStyle if isDarkTheme() else MaterialLightStyle

    @classmethod
    def invalidate(cls) -> None:
        """清空 ``QTextCharFormat`` 缓存。

        正常主题切换**不需要**调用它（缓存键已含主题）；留给测试与
        “改了调色板想立刻看效果”的场景。
        """
        cls._format_cache.clear()

    # —— token 字符格式 ——

    @classmethod
    def token_format(cls, ttype: TokenType) -> QTextCharFormat:
        """token 类型 → ``QTextCharFormat``（缓存）。"""
        is_dark = isDarkTheme()
        key = (is_dark, str(ttype))
        fmt = cls._format_cache.get(key)
        if fmt is not None:
            return fmt

        style = MaterialStyle if is_dark else MaterialLightStyle
        spec = style.style_for_token(ttype)
        fmt = QTextCharFormat()

        # Token.Text 及其子类不设前景色，继承 widget 默认色（暗色白字 / 亮色黑字）。
        # 报文里绝大多数字符都是 Token.Text，交给 widget 才能跟随 QFluentWidgets 主题。
        if spec["color"] and not str(ttype).startswith("Token.Text"):
            fmt.setForeground(QColor(f"#{spec['color']}"))
        if spec["bold"]:
            fmt.setFontWeight(QFont.Weight.Bold)
        if spec["italic"]:
            fmt.setFontItalic(True)
        if spec["underline"]:
            fmt.setFontUnderline(True)

        cls._format_cache[key] = fmt
        return fmt

    # —— 自绘装饰色 ——

    @staticmethod
    def _mono(alpha: int) -> QColor:
        """半透明白（暗色主题）/ 半透明黑（亮色主题）。"""
        return (
            QColor(255, 255, 255, alpha) if isDarkTheme() else QColor(0, 0, 0, alpha)
        )

    @classmethod
    def line_number_active(cls) -> QColor:
        """当前行的行号文字色。"""
        return cls._mono(_LINE_NUMBER_ACTIVE_ALPHA)

    @classmethod
    def line_number_normal(cls) -> QColor:
        """非当前行的行号文字色。"""
        return cls._mono(_LINE_NUMBER_NORMAL_ALPHA)

    @classmethod
    def gutter_divider(cls) -> QColor:
        """行号区与正文之间的竖分割线。"""
        return cls._mono(
            _GUTTER_DIVIDER_ALPHA_DARK
            if isDarkTheme()
            else _GUTTER_DIVIDER_ALPHA_LIGHT
        )

    @classmethod
    def current_line(cls) -> QColor:
        """当前行底色（正文与行号区共用，保证两侧对齐时色带连续）。"""
        return cls._mono(
            _CURRENT_LINE_ALPHA_DARK if isDarkTheme() else _CURRENT_LINE_ALPHA_LIGHT
        )

    @staticmethod
    def search_match() -> QColor:
        """查找命中（非当前项）底色。"""
        return QColor(*(_SEARCH_MATCH_DARK if isDarkTheme() else _SEARCH_MATCH_LIGHT))

    @staticmethod
    def search_current() -> QColor:
        """查找命中中的"当前项"底色。"""
        return QColor(
            *(_SEARCH_CURRENT_DARK if isDarkTheme() else _SEARCH_CURRENT_LIGHT)
        )

    @staticmethod
    def tree_count() -> QColor:
        """JSON 树里 ``Object(3)`` / ``Array(7)`` 这类计数标签的文字色。"""
        return QColor(EditorPalette.style().gray)


__all__ = ["EditorPalette"]
