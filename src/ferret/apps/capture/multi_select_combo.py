"""MultiSelectionComboBox 的复刻（qfw Pro 组件在免费版的替代实现）。

外观与交互对齐 Pro 版：框内以**可删除芯片**展示已选项（附一枚可手输的小
输入框），右侧下拉按钮弹出**勾选面板**——顶部搜索 + 图标条目，勾选即增删
芯片，面板保持打开、点外部关闭。qfw 的菜单点击条目即关闭
（`RoundMenu._onItemClicked` → `_hideMenu`），撑不起连续勾选，所以面板用
`Qt.Popup` 自绘而非 CheckableMenu。

视觉层全部取 qfw 官方参数（实测 dump LineEdit 官方 QSS 得到）：边框浅色
``rgba(0,0,0,13)`` / 深色 ``rgba(255,255,255,0.08)``，圆角 4px，焦点青
``#009faa``；条目复选框用 qfw `CheckBox`（自带 Fluent 青色勾），对齐
全应用视觉。浅深双套经 `setCustomStyleSheet` 挂载，主题切换自动跟随。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CheckBox,
    FlowLayout,
    FluentIcon,
    LineEdit,
    SearchLineEdit,
    SmoothScrollArea,
    TransparentToolButton,
    setCustomStyleSheet,
)

# qfw LineEdit 官方视觉参数（浅/深两套；实测 dump，勿手改）。
_BORDER_LIGHT = "rgba(0, 0, 0, 13)"
_BORDER_DARK = "rgba(255, 255, 255, 0.08)"
_BORDER_BOTTOM_LIGHT = "rgba(0, 0, 0, 46)"
_BORDER_BOTTOM_DARK = "rgba(255, 255, 255, 0.18)"
_FOCUS_COLOR = "#009faa"
_CHIP_BG_LIGHT = "rgba(0, 0, 0, 9)"
_CHIP_BG_DARK = "rgba(255, 255, 255, 0.0605)"
_CHIP_HOVER_LIGHT = "rgba(0, 0, 0, 14)"
_CHIP_HOVER_DARK = "rgba(255, 255, 255, 0.1)"

_CHIP_HEIGHT = 26
_FRAME_MIN_HEIGHT = 33
_ROW_HEIGHT = 33
_POPUP_MAX_ROWS = 9


def _frame_qss(bg: str, border: str, border_bottom: str) -> str:
    return (
        f"#MultiSelectionComboBox {{ border: 1px solid {border};"
        f" border-bottom: 1px solid {border_bottom}; border-radius: 4px;"
        f" background-color: {bg}; }}"
        f"#MultiSelectionComboBox:focus {{ border-bottom: 1px solid {_FOCUS_COLOR}; }}"
    )


def _chip_qss(bg: str, hover: str) -> str:
    return (
        f"#tokenChip {{ background-color: {bg}; border-radius: 4px; }}"
        f"#tokenChip:hover {{ background-color: {hover}; }}"
    )


class _TokenChip(QFrame):
    """一枚可删除的已选标签。"""

    removed = Signal(str)

    def __init__(self, token: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.token = token
        self.setObjectName("tokenChip")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(2)
        label = QLabel(token, self)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        button = TransparentToolButton(FluentIcon.CLOSE, self)
        button.setFixedSize(18, 18)
        button.clicked.connect(lambda: self.removed.emit(self.token))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(_CHIP_HEIGHT)


class _CheckRow(QFrame):
    """勾选面板的一行：图标 + qfw CheckBox。点行任意处即切换勾选。"""

    def __init__(
        self,
        label: str,
        icon: QIcon | None,
        checked: bool,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("checkRow")
        self.setFixedHeight(_ROW_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 0, 8, 0)
        layout.setSpacing(8)
        icon_label = QLabel(self)
        if icon is not None and not icon.isNull():
            icon_label.setPixmap(icon.pixmap(16, 16))
        icon_label.setFixedSize(20, _ROW_HEIGHT)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)
        self.box = CheckBox(label, self)
        self.box.setChecked(checked)
        layout.addWidget(self.box, 1)

    def mousePressEvent(self, event) -> None:
        self.box.toggle()
        super().mousePressEvent(event)


class _ChecklistPopup(QDialog):
    """进程勾选面板：搜索 + 图标 + qfw 勾选框。点外部关闭，勾选随时可读。"""

    def __init__(
        self,
        anchor: QWidget,
        items: list[tuple[str, QIcon | None, bool]],
    ):
        """`items` 为 ``(标签, 图标, 初始勾选)``，顺序即展示顺序。"""
        super().__init__(anchor, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        card = QFrame(self)
        card.setObjectName("checklistCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 4)
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        inner = QVBoxLayout(card)
        inner.setContentsMargins(8, 8, 8, 8)
        inner.setSpacing(6)

        self._search = SearchLineEdit(card)
        self._search.setPlaceholderText(self.tr("Search"))
        self._search.setClearButtonEnabled(True)
        inner.addWidget(self._search)

        self._list = QListWidget(card)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setStyleSheet("QListWidget { background: transparent; }")
        for label, icon, checked in items:
            item = QListWidgetItem(self._list)
            item.setSizeHint(QSize(0, _ROW_HEIGHT))
            row = _CheckRow(label, icon, checked, card)
            self._list.setItemWidget(item, row)
        area = SmoothScrollArea(card)
        area.setWidget(self._list)
        area.setWidgetResizable(True)
        area.setFixedHeight(min(len(items), _POPUP_MAX_ROWS) * _ROW_HEIGHT + 4)
        inner.addWidget(area)

        self._search.textChanged.connect(self._filter)
        width = max(anchor.width(), 260)
        self.resize(width, 96 + min(len(items), _POPUP_MAX_ROWS) * _ROW_HEIGHT)

    def _filter(self, query: str) -> None:
        needle = query.strip().lower()
        for row in range(self._list.count()):
            widget = self._list.itemWidget(self._list.item(row))
            if not isinstance(widget, _CheckRow):
                continue
            widget.setHidden(bool(needle) and needle not in widget.box.text().lower())

    def checked_labels(self) -> list[str]:
        labels = []
        for row in range(self._list.count()):
            widget = self._list.itemWidget(self._list.item(row))
            if isinstance(widget, _CheckRow) and widget.box.isChecked():
                labels.append(widget.box.text())
        return labels


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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        setCustomStyleSheet(
            self,
            _frame_qss("rgba(249, 249, 249, 0.3)", _BORDER_LIGHT, _BORDER_BOTTOM_LIGHT),
            _frame_qss("rgba(255, 255, 255, 0.0419)", _BORDER_DARK, _BORDER_BOTTOM_DARK),
        )

        self._tokens: list[str] = []
        self._items: list[tuple[str, QIcon | None]] | None = None
        self._icons: dict[str, QIcon] = {}

        body = QHBoxLayout(self)
        body.setContentsMargins(8, 3, 4, 3)
        body.setSpacing(4)

        self._chip_area = QFrame(self)
        self._flow = FlowLayout(self._chip_area, needAni=False, isTight=True)
        self._flow.setContentsMargins(0, 0, 0, 0)
        self._flow.setHorizontalSpacing(6)
        self._flow.setVerticalSpacing(4)
        body.addWidget(self._chip_area, 1)

        self._add_edit = LineEdit(self._chip_area)
        self._add_edit.setPlaceholderText(self.tr("Add"))
        self._add_edit.setFixedWidth(72)
        self._add_edit.setFixedHeight(_CHIP_HEIGHT)
        self._add_edit.returnPressed.connect(self._commit_manual)

        self._button = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self._button.setFixedSize(29, 28)
        self._button.clicked.connect(self._open_popup)
        body.addWidget(self._button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.setMinimumHeight(_FRAME_MIN_HEIGHT)
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
            chip.setStyleSheet(_chip_qss(_CHIP_BG_LIGHT, _CHIP_HOVER_LIGHT))
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
        self.setFixedHeight(max(height + 8, _FRAME_MIN_HEIGHT))

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
