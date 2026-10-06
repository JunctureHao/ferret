"""Titlebar 全局搜索：唯一搜索框 + 页面协议（docs/design.md#ui v3）。

全局只有一个搜索框（`SearchHost.edit`，所有页面共用，固定宽 400）：六个纯文本页
实现 `SearchablePage` 三方法，键入经 MainWindow 路由转发给当前页。捕获页**不换
控件**——通过可选的 `search_actions()` 钩子把自己的独特动作（帮助 / 高亮）注入框内
trailing 位，文本仍由它自己的 flowfilter 引擎解释（"用自己的搜索引擎"，
规格 §5.1 v3，2026-09-27 用户修正：原"换控件"方案废弃）。
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget
from qfluentwidgets import FluentIcon, LineEdit, qconfig


class SearchablePage(Protocol):
    """可搜索页面的鸭子类型协议（规格 §4.2）。

    可选钩子（鸭子类型，不进必选集）：
    - ``search_actions()``：返回要注入框内 trailing 位的 QAction 列表（捕获页的
      帮助 / 高亮）；切页时由宿主换入换出。
    - ``search_focus_target()``：Esc 后焦点归还目标（通常是页面表格）；缺省回落
      当前页容器。
    """

    def search_placeholder(self) -> str: ...
    def apply_search(self, text: str) -> None: ...
    def current_search_text(self) -> str: ...


def is_searchable(page: QWidget | None) -> bool:
    """运行时鸭子判定：三个协议方法齐全才算可搜索页。"""
    return page is not None and all(
        callable(getattr(page, name, None))
        for name in ("search_placeholder", "apply_search", "current_search_text")
    )


class SearchHost(QWidget):
    """titlebar 搜索槽的宿主：持有唯一搜索框 + 页面动作的换入换出。

    框固定宽 400（2026-09-27 用户决议），所有可搜索页共用同一控件——没有控件级
    替换，只有占位文案 / 文本回填 / 内嵌动作的切换。回填用 QSignalBlocker 抑制
    textChanged，避免「回填又触发一次过滤」。
    """

    search_requested = Signal(str)  # 框文本变化（→ MainWindow 路由给当前页）
    escape_pressed = Signal()  # Esc → 焦点归还当前页

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.edit = LineEdit(self)
        self.edit.setClearButtonEnabled(True)
        self.edit.setFixedHeight(32)
        self.edit.setFixedWidth(400)
        # 左侧搜索图标（leading 装饰性 action；过滤随键入 live 生效，点击无动作）。
        self._search_icon = QAction(FluentIcon.SEARCH.icon(), self.tr("搜索"), self)
        # 主题切换后重上色：FluentIconEngine 在 icon() 时烘焙颜色，不重设就停留旧色。
        qconfig.themeChangedFinished.connect(self._refresh_theme_icon)
        self.edit.addAction(self._search_icon, QLineEdit.ActionPosition.LeadingPosition)
        self.edit.installEventFilter(self)
        self._page_actions: list[QAction] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        self.edit.textChanged.connect(self.search_requested)
        self.hide()

    def _refresh_theme_icon(self) -> None:
        """主题切换完成后重上色（qconfig.themeChangedFinished，规格 §4.1）。"""
        self._search_icon.setIcon(FluentIcon.SEARCH.icon())

    def set_actions(self, actions: list[QAction]) -> None:
        """换入当前页的独特动作（捕获页：帮助 / 高亮）；先拆旧动作。

        qfw `LineEdit` 重写了 `addAction`（把 QAction 包装成 LineEditButton 塞进
        内部布局）但**没有重写 `removeAction`**——原生 removeAction 只清 action
        列表，包装按钮会永久残留并随切页累积（实测 bug：右侧按钮越切越多）。
        因此这里手动拆按钮：qfw 把包装按钮登记在 `leftButtons`/`rightButtons`
        公开列表里，逐个 setParent(None) 移除即可；左侧搜索图标按钮常驻保留。
        """
        edit = self.edit
        for attr in ("leftButtons", "rightButtons"):
            buttons = getattr(edit, attr, None)
            if buttons is None:
                continue
            keep = []
            for btn in list(buttons):
                if btn.action() is self._search_icon:
                    keep.append(btn)  # 左侧搜索图标常驻
                    continue
                btn.setParent(None)
                btn.deleteLater()
            buttons.clear()
            buttons.extend(keep)
        self._page_actions = list(actions)
        trailing = QLineEdit.ActionPosition.TrailingPosition
        for action in self._page_actions:
            edit.addAction(action, trailing)

    def show_box(self, placeholder: str, text: str) -> None:
        """协议页：设占位文案并回填该页文本（不发 textChanged）。"""
        self.edit.setPlaceholderText(placeholder)
        with QSignalBlocker(self.edit):
            self.edit.setText(text)
        self.setVisible(True)

    def hide_all(self) -> None:
        """无搜索页：整个槽位隐藏并清掉页面动作，titlebar 回归纯标题（规格 §4.5）。"""
        self.set_actions([])
        self.setVisible(False)

    def clear_search(self) -> None:
        """清空框；textChanged 自然回流 → 路由层 apply_search("")（规格 §6）。"""
        self.edit.clear()

    def focus_current(self) -> None:
        """全局 Ctrl+F：聚焦搜索框（规格 §4.4）。"""
        if self.isVisible():
            self.edit.setFocus()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape  # ty: ignore[unresolved-attribute]
        ):
            # Esc 清空（与 clear 按钮等价）后失焦；消费掉事件，不做两级 Esc。
            self.edit.clear()
            self.escape_pressed.emit()
            return True
        return super().eventFilter(watched, event)
