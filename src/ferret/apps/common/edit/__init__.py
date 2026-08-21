"""编辑 / 高亮 / 键值对面板子包。

对外保持与旧模块一致的导入路径：
    from ferret.apps.common.edit import ItemDualPanel, ToolPlainTextEdit

内部按职责拆分：
    - syntax.py      : Language / 各语言分词器 / MaterialStyle(暗) + MaterialLightStyle(亮)
    - theme.py       : EditorPalette —— 按主题取色，编辑器配色的唯一出处
    - highlighter.py : TokenHighlighter —— 唯一高亮器，语言可原地切换
    - editor.py      : CodeEditor / LineNumberArea
    - widgets.py     : ToolWidget / ItemTableWidget / ItemTableToolWidget /
                       ToolPlainTextEdit / ItemDualPanel / JsonTreeWidget /
                       JsonTreePanel / JsonDualPanel / SortState / SORT_TRANSITION

高亮器由四个类（``UniversalHighlighter``/``HTTPHighlighter``/``HeadersHighlighter``/
``JSONHighlighter``）收敛为一个 ``TokenHighlighter``：其中三个类的"覆写
``_generate_tokens`` 换词法器"钩子从来没有被调用过，``HeadersHighlighter`` 因此整个
是 no-op。换语言现在走 ``CodeEditor.set_language`` / ``TokenHighlighter.set_language``。
"""

from .editor import CodeEditor, LineNumberArea
from .highlighter import LEX_LIMIT, TokenHighlighter
from .syntax import Language
from .theme import EditorPalette
from .widgets import (
    SORT_TRANSITION,
    ItemDualPanel,
    ItemTableToolWidget,
    ItemTableWidget,
    JsonDualPanel,
    JsonTreePanel,
    JsonTreeWidget,
    SortState,
    ToolPlainTextEdit,
    ToolWidget,
)

__all__ = [
    "LEX_LIMIT",
    "SORT_TRANSITION",
    "CodeEditor",
    "EditorPalette",
    "ItemDualPanel",
    "ItemTableToolWidget",
    "ItemTableWidget",
    "JsonDualPanel",
    "JsonTreePanel",
    "JsonTreeWidget",
    "Language",
    "LineNumberArea",
    "SortState",
    "TokenHighlighter",
    "ToolPlainTextEdit",
    "ToolWidget",
]


if __name__ == "__main__":
    import sys

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QVBoxLayout
    from qfluentwidgets import (
        FluentIcon,
        Theme,
        TransparentToolButton,
        isDarkTheme,
        setTheme,
    )
    from qfluentwidgets.window.fluent_window import FluentWidget

    from ferret.core import resources_rc  # noqa: F401

    app = QApplication(sys.argv)
    setTheme(Theme.DARK)

    window = FluentWidget()
    window.setWindowTitle("KeyValueViewPanel Demo")

    btn_theme = TransparentToolButton(FluentIcon.CONSTRACT)
    btn_theme.setToolTip("切换主题")
    title_layout = window.titleBar.hBoxLayout
    title_layout.insertWidget(
        title_layout.count() - 1, btn_theme, 0, Qt.AlignmentFlag.AlignVCenter
    )

    @btn_theme.clicked.connect
    def _():
        setTheme(Theme.LIGHT if isDarkTheme() else Theme.DARK)

    layout = QVBoxLayout(window)
    title_height = window.titleBar.height()
    layout.setContentsMargins(12, title_height + 4, 12, 12)
    layout.setSpacing(8)

    editor = ItemDualPanel(True)
    editor.set_items(
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ91111111111111111111111111111111111111111111111111111111111111111111111111111111",
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Host": "api.example.com",
            "Connection": "keep-alive",
        }
    )
    layout.addWidget(editor, stretch=1)

    window.show()
    sys.exit(app.exec())
