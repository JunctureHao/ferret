"""MultiSelectionComboBox 的复刻（qfw Pro 组件在免费版的替代实现）。

外观与交互对齐 Pro 版：框内以**可删除芯片**展示已选项（附一枚可手输的小
输入框），右侧下拉按钮弹出**勾选面板**——顶部搜索 + 图标条目，勾选即增删
芯片，面板保持打开、点外部关闭。qfw 的菜单点击条目即关闭
（`RoundMenu._onItemClicked` → `_hideMenu`），撑不起连续勾选，所以面板用
`Qt.Popup` 自绘而非 CheckableMenu。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, FlowLayout, FluentIcon, TransparentToolButton

_FRAME_QSS = (
    "#MultiSelectionComboBox { border: 1px solid rgba(127, 127, 127, 0.35);"
    " border-radius: 6px; background: transparent; }"
    "#MultiSelectionComboBox:focus { border-color: rgba(0, 120, 212, 0.8); }"
)
_CHIP_QSS = "#tokenChip { background: rgba(127, 127, 127, 0.14); border-radius: 4px; }"
_POPUP_QSS = (
    "#checklistCard { border: 1px solid rgba(127, 127, 127, 0.35);"
    " border-radius: 8px; background: palette(base); }"
)
_CHIP_HEIGHT = 24
_ROW_HEIGHT = 33
_POPUP_MAX_ROWS = 9


class _TokenChip(QFrame):
    """一枚可删除的已选标签。"""

    removed = Signal(str)

    def __init__(self, token: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.token = token
        self.setObjectName("tokenChip")
        self.setStyleSheet(_CHIP_QSS)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(2)
        layout.addWidget(QLabel(token, self), 0, Qt.AlignmentFlag.AlignVCenter)
        button = TransparentToolButton(FluentIcon.CLOSE, self)
        button.setFixedSize(18, 18)
        button.clicked.connect(lambda: self.removed.emit(self.token))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(_CHIP_HEIGHT)


class _ChecklistPopup(QDialog):
    """进程勾选面板：顶部搜索 + 图标条目。点外部关闭，勾选状态随时可读。"""

    def __init__(
        self,
        anchor: QWidget,
        items: list[tuple[str, QIcon | None, bool]],
    ):
        """`items` 为 ``(标签, 图标, 初始勾选)``，顺序即展示顺序。"""
        super().__init__(anchor, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(_POPUP_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        card = QFrame(self)
        card.setObjectName("checklistCard")
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(8, 8, 8, 8)
        inner.setSpacing(6)

        self._search = QLineEdit(card)
        self._search.setPlaceholderText(self.tr("Search"))
        self._search.setClearButtonEnabled(True)
        inner.addWidget(self._search)

        self._list = QListWidget(card)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        for label, icon, checked in items:
            item = QListWidgetItem(label)
            if icon is not None:
                item.setIcon(icon)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            self._list.addItem(item)
        self._list.itemClicked.connect(self._toggle_item)
        inner.addWidget(self._list)

        self._search.textChanged.connect(self._filter)
        width = max(anchor.width(), 260)
        rows = min(len(items), _POPUP_MAX_ROWS)
        self.resize(width, 96 + rows * _ROW_HEIGHT)

    def _toggle_item(self, item: QListWidgetItem) -> None:
        # 点行任意处切换勾选，与 Pro 版一致；面板保持打开。
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def _filter(self, query: str) -> None:
        needle = query.strip().lower()
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def checked_labels(self) -> list[str]:
        return [
            self._list.item(row).text()
            for row in range(self._list.count())
            if self._list.item(row).checkState() == Qt.CheckState.Checked
        ]


class MultiSelectionComboBox(QFrame):
    """多选下拉框：已选项以可删除芯片展示，下拉为勾选面板（含搜索）。

    `tokens()` 是唯一事实来源；候选条目由 `set_items` 提供
    （``(标签, 图标 | None)`` 列表）。勾选 / 删芯片 / 手输回车都会更新
    tokens 并发 `tokensChanged`。
    """

    tokensChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("MultiSelectionComboBox")
        self.setStyleSheet(_FRAME_QSS)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._tokens: list[str] = []
        self._items: list[tuple[str, QIcon | None]] | None = None
        self._icons: dict[str, QIcon] = {}

        body = QHBoxLayout(self)
        body.setContentsMargins(8, 3, 4, 3)
        body.setSpacing(4)

        self._chip_area = QFrame(self)
        self._flow = FlowLayout(self._chip_area, needAni=False, isTight=True)
        self._flow.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self._chip_area, 1)

        self._placeholder = CaptionLabel("", self._chip_area)
        self._placeholder.setVisible(False)

        self._add_edit = QLineEdit(self._chip_area)
        self._add_edit.setPlaceholderText(self.tr("Add"))
        self._add_edit.setFixedWidth(64)
        self._add_edit.setFixedHeight(_CHIP_HEIGHT)
        self._add_edit.returnPressed.connect(self._commit_manual)

        self._button = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self._button.setFixedSize(29, 28)
        self._button.clicked.connect(self._open_popup)
        body.addWidget(self._button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._rebuild()

    # —— 对外 API ——

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def set_tokens(self, tokens: list[str]) -> None:
        self._tokens = [token.strip() for token in tokens if token.strip()]
        self._rebuild()

    def set_items(self, items: list[tuple[str, QIcon | None]]) -> None:
        """设置下拉面板的候选条目（标签 + 图标）。"""
        self._items = list(items)

    # —— 芯片区 ——

    def _rebuild(self) -> None:
        # qfw FlowLayout 的 takeAt 直接返回 widget；takeAllWidgets 顺手 deleteLater。
        self._flow.takeAllWidgets()
        for token in self._tokens:
            chip = _TokenChip(token, self._chip_area)
            chip.removed.connect(self.remove_token)
            self._flow.addWidget(chip)
        self._flow.addWidget(self._add_edit)
        self._relayout()
        self.tokensChanged.emit(self.tokens())

    def _commit_manual(self) -> None:
        token = self._add_edit.text().strip()
        if token and token.lower() not in {t.lower() for t in self._tokens}:
            self._tokens.append(token)
            self._rebuild()
        self._add_edit.clear()

    def remove_token(self, token: str) -> None:
        self._tokens = [t for t in self._tokens if t != token]
        self._rebuild()

    def _relayout(self) -> None:
        width = max(self._chip_area.width(), 120)
        height = max(int(self._flow.heightForWidth(width)), _CHIP_HEIGHT)
        self._chip_area.setFixedHeight(height)
        self.setFixedHeight(height + 8)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    # —— 下拉面板 ——

    def _open_popup(self) -> None:
        if self._items is None:
            return
        checked = {token.lower() for token in self._tokens}
        item_labels = {label.lower() for label, _ in self._items}
        entries = [
            (label, icon, label.lower() in checked) for label, icon in self._items
        ]
        popup = _ChecklistPopup(self, entries)
        popup.move(self.mapToGlobal(QPoint(0, self.height())))
        popup.exec()
        picked = {label.lower() for label in popup.checked_labels()}
        # 手输 token 与候选对不上的原样保留（如 !pid 排除项）；与候选对得上的
        # 由面板勾选结果决定去留。
        manual = [t for t in self._tokens if t.lower() not in item_labels]
        self._tokens = manual + [label for label, _ in self._items if label.lower() in picked]
        self._rebuild()
