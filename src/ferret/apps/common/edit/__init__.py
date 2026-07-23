"""编辑 / 高亮 / 键值对面板子包。

对外保持与旧模块一致的导入路径：
    from ferret.views.common.edit import KVDualPanel, ToolPlainTextEdit

内部按职责拆分：
    - highlighter.py : UniversalHighlighter / HTTPHighlighter / HeadersHighlighter / JSONHighlighter
    - editor.py      : CodeEditor / LineNumberArea
    - widgets.py     : ToolWidget / KVTableWidget / KVTableToolWidget / ToolPlainTextEdit / KVDualPanel / SortState / SORT_TRANSITION
"""

from .editor import CodeEditor, LineNumberArea
from .highlighter import (
    HeadersHighlighter,
    HTTPHighlighter,
    JSONHighlighter,
    UniversalHighlighter,
)
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
    CodeEditor,
    LineNumberArea,
    UniversalHighlighter,
    HTTPHighlighter,
    HeadersHighlighter,
    JSONHighlighter,
    ToolWidget,
    ItemTableWidget,
    SortState,
    SORT_TRANSITION,
    ItemTableToolWidget,
    ToolPlainTextEdit,
    ItemDualPanel,
    JsonTreeWidget,
    JsonTreePanel,
    JsonDualPanel,
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

    from ferret.code import resources_rc  # noqa: F401

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
