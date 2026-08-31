"""聊天式气泡流：WS 帧 / SSE 事件共用的消息展示组件。

设计目标是一条**通用**的气泡流，不认识 WsFrame 也不认识 SseEvent —— 调用方
（`messages.py`）负责把值对象翻译成 `Bubble` + 一个可排序的键。将来 SSE 实时
推送落地时直接复用这条管道，不用再动这里。

行为约定（`messages.py` 的测试钉着这些语义）：

* **增量追加 O(1)**：实时流每秒几十帧，整流重建会把选中和滚动位置一起清掉。
* **贴边追新**：正序模式下只有滚动条贴底时才跟着滚（正翻旧消息的人不该被拽走）；
  逆序模式反过来 —— 新气泡插在顶部，贴顶（``value() <= 4``）才算「在看最新」。
* **超限逐出**：显示列表超过上限时从最旧一端挤掉一条，防止长连接监控把内存吃穿。
* **过滤**：大小写不敏感子串匹配 `search_text`，不匹配的气泡 `setVisible(False)`
  （不销毁），清空过滤词即全显 —— 过滤态下追加 / 逐出 / 排序都照常工作。
* **清空**：`clear()` 只清显示，调用方手里的数据源不动；之后照常追加。
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ScrollArea,
    isDarkTheme,
    qconfig,
)

# 气泡内容区的收起高度上限（像素）。超长内容截断显示，点击气泡原地展开 —— 一帧
# 几十 KB 的推流直接铺开会把这个区域变成一整条不可读的长墙。
MAX_COLLAPSED_HEIGHT = 120

# 气泡宽度策略：按视口可用宽度的比例给**固定宽**（聊天气泡的惯例形态），窄面板
# 兜底到全宽。宽高必须都定死 —— 词折行标签的 sizeHint 是出了名的不稳定，宽高
# 任一项交给 sizeHint 都会让布局在两次计算间振荡（卡片的字被剪成一条缝）。
BUBBLE_WIDTH_RATIO = 0.72
BUBBLE_MIN_WIDTH = 180

# 判定「贴着最新一端」的滚动条容差（像素）。
EDGE_SLACK = 4


class Bubble(CardWidget):
    """一枚聊天气泡：一张只摆**消息内容**的 qfw 卡片。

    方向不写在文字里 —— 发过来靠左、发过去靠右，对齐本身就是方向。点击**原地**
    展开 / 收起长内容，同时把自己标记为选中（选中态换强调底色，调用方据此知道
    复制目标是谁）。二进制内容由调用方预先转成 hex 文本再进来 —— 气泡只管摆字，
    不认识字节。

    底色/hover/主题热切换全部走 `CardWidget`（`BackgroundAnimationWidget` 已接
    `qconfig.themeChanged`），选中态只需覆写 `_normalBackgroundColor` 再触发一次
    重算；子类不再自绘任何样式。
    """

    #: 点击（无论展开还是收起）都会带上键发一次；选中跟着点击走。
    #: 不能覆写 `CardWidget.clicked`（无参信号），这里另起名字。
    activated = Signal(object)

    # 选中态的强调底色（浅色 / 深色）。
    _SELECTED_LIGHT = QColor(0, 0, 0, 28)
    _SELECTED_DARK = QColor(255, 255, 255, 38)

    # qfw 的 BackgroundColorObject 在基类构造期就会回调 `_normalBackgroundColor`，
    # 那时实例属性还没来得及赋 —— 选中标志必须给类级默认。
    _selected = False

    def __init__(
        self,
        content: str,
        *,
        key: object = None,
        search_text: str = "",
        align_right: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.search_text = search_text.lower()
        self._align_right = align_right
        self._expanded = False
        self._selected = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setBorderRadius(8)

        self.content_label = BodyLabel(content, self)
        self.content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.content_label.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        layout.addWidget(self.content_label, 1)

    # —— 状态 ——

    @property
    def is_expanded(self) -> bool:
        return self._expanded

    @property
    def align_right(self) -> bool:
        return self._align_right

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self._updateBackgroundColor()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle_expanded()
            self.activated.emit(self.key)
            return
        super().mouseReleaseEvent(e)

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self._update_geometry()

    def set_content(self, text: str) -> None:
        """换内容并按当前宽度重算高度（二进制帧收起预览 ↔ 展开态 hex dump 用）。"""
        self.content_label.setText(text)
        self._update_geometry()

    def set_bubble_width(self, width: int) -> None:
        """容器给定的固定宽；`resizeEvent` 会随之重算高度。"""
        if width > 0 and width != self.width():
            self.setFixedWidth(width)

    # —— 样式 ——

    def _normalBackgroundColor(self) -> QColor:
        """选中态给强调底色；qfw 的背景动画在主题切换/点击/hover 时都会重读这里。"""
        if self._selected:
            return self._SELECTED_DARK if isDarkTheme() else self._SELECTED_LIGHT
        return super()._normalBackgroundColor()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 拿到真实宽度才知道折几行。
        self._update_geometry()

    def _update_geometry(self) -> None:
        """把折行高度手动喂进布局链。

        词折行标签的 `minimumSizeHint` 高恒为一行，布局按 sizeHint（单行）分配
        高度 —— 长内容被垂直剪成一条缝、maxHeight 也封不住（sizeHint 比 max
        小得多时轮不到它）。所以收起 / 展开都把**气泡整体钉死**：高度 = 内容在
        当前宽度下的 `heightForWidth`（收起态与 `MAX_COLLAPSED_HEIGHT` 取小）+
        边距，宽度变化时 resizeEvent 重算。
        """
        layout = self.layout()
        margins = (
            layout.contentsMargins() if layout is not None else QMargins(12, 8, 12, 8)
        )
        width = self.width() - margins.left() - margins.right()
        if width <= 0:
            return
        height = self.content_label.heightForWidth(width)
        if height < 0:
            return
        if not self._expanded:
            height = min(height, MAX_COLLAPSED_HEIGHT)
        self.setFixedHeight(height + margins.top() + margins.bottom())


class SystemNote(QFrame):
    """居中灰条：连接关闭、心跳块这类「不是任何一方说的话」的流内事件。"""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatSystemNote")
        self.label = CaptionLabel(text, self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.addWidget(self.label)
        self.apply_theme()

    def apply_theme(self) -> None:
        """重算主题相关样式（主题热切换时由 `ChatStream` 统一广播）。"""
        dark = isDarkTheme()
        bg = QColor(0, 0, 0, 8) if not dark else QColor(255, 255, 255, 13)
        self.setStyleSheet(
            "QFrame#ChatSystemNote {"
            f"background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
            "border-radius: 6px;"
            "}"
            "QFrame#ChatSystemNote QLabel {"
            "background: transparent;"
            "border: none;"
            "}"
        )


class ChatStream(ScrollArea):
    """垂直气泡流。

    用 qfw 的 `ScrollArea` 而不是原生 `QScrollArea`：原生视口画的是调色板白底，
    深色主题下气泡里的白字直接隐形；`enableTransparentBackground()` 让视口透出
    宿主的主题底色，滚动条也走 qfw 的主题样式。追加用 ``insertWidget`` 而不是
    「全清重排」：实时流一秒几十帧，重建会把选中和滚动位置一起清掉。排序切换是
    唯一走全量重建的路径（用户显式动作，一次可接受）。
    """

    #: 选中变化（键，或 None）。调用方用它决定复制目标。
    selectionChanged = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._descending = False
        self._filter = ""
        self._selected_key: object = None

        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._host = QWidget(self)
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(8, 8, 8, 8)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)
        self.setWidget(self._host)
        # 须在 setWidget 之后：这个助手连 host 的透明样式一起设。
        self.enableTransparentBackground()

        # 自绘的系统条不会自己跟随主题热切换，这里统一广播；气泡走 CardWidget
        # 的内置主题跟随。
        qconfig.themeChanged.connect(self._broadcast_theme)

    # —— 查询 ——

    @property
    def descending(self) -> bool:
        return self._descending

    @property
    def filter_text(self) -> str:
        return self._filter

    @property
    def selected_key(self) -> object:
        return self._selected_key

    def bubbles(self) -> list[Bubble]:
        """当前流里的全部气泡（含被过滤隐藏的）。测试与逐出逻辑用。"""
        return [w for w in self._widgets() if isinstance(w, Bubble)]

    # —— 修改 ——

    def add(self, bubble: Bubble) -> None:
        """追加一枚气泡（正序插尾、逆序插顶），按当前过滤词摆显隐。"""
        self._insert(bubble)
        self._apply_filter_to(bubble)

    def add_note(self, note: SystemNote) -> None:
        """追加一条居中系统条（同样遵循正 / 逆序位置）。"""
        align = Qt.AlignmentFlag.AlignCenter
        if self._descending:
            self._layout.insertWidget(0, note, 0, align)
        else:
            self._layout.insertWidget(self._layout.count() - 1, note, 0, align)

    def evict_oldest(self) -> None:
        """从最旧一端移除一枚气泡（正序=头部，逆序=stretch 前的最后一枚）。"""
        candidates = self.bubbles()
        if not candidates:
            return
        oldest = candidates[0] if not self._descending else candidates[-1]
        if oldest.key == self._selected_key:
            self._selected_key = None
            self.selectionChanged.emit(None)
        oldest.setParent(None)
        oldest.deleteLater()

    def set_descending(self, descending: bool) -> None:
        """切换正 / 逆序：按现有子项全量重建一次。选中按键恢复。"""
        if descending == self._descending:
            return
        self._descending = descending
        widgets = self._take_all()
        for widget in widgets:
            self._reinsert(widget)
        self._restore_selection()

    def set_filter(self, text: str) -> None:
        """过滤词变化：对全部子项重摆显隐（不销毁、不重建）。"""
        self._filter = text.strip().lower()
        for widget in self._widgets():
            self._apply_filter_to(widget)

    def clear(self) -> None:
        """清空显示。调用方数据源不动；选中归零。"""
        for bubble in self.bubbles():
            bubble.activated.disconnect()
        for widget in self._take_all():
            widget.setParent(None)
            widget.deleteLater()
        self._selected_key = None
        self.selectionChanged.emit(None)

    # —— 滚动 ——

    def at_newest_edge(self) -> bool:
        """是否贴着「最新」一端：正序看底、逆序看顶。"""
        bar = self.verticalScrollBar()
        if self._descending:
            return bar.value() <= EDGE_SLACK
        return bar.value() >= bar.maximum() - EDGE_SLACK

    def scroll_to_newest(self) -> None:
        if self._descending:
            self.verticalScrollBar().setValue(0)
        else:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    # —— 内部 ——

    def _broadcast_theme(self) -> None:
        """主题热切换：让自绘的系统条重算样式（气泡由 CardWidget 自己跟）。"""
        for widget in self._widgets():
            if isinstance(widget, SystemNote):
                widget.apply_theme()

    def _insert(self, bubble: Bubble) -> None:
        bubble.set_bubble_width(self.__bubble_width())
        align = (
            Qt.AlignmentFlag.AlignRight
            if bubble.align_right
            else Qt.AlignmentFlag.AlignLeft
        )
        if self._descending:
            self._layout.insertWidget(0, bubble, 0, align)
        else:
            # stretch 占着末位，插在它前面。
            self._layout.insertWidget(self._layout.count() - 1, bubble, 0, align)
        bubble.activated.connect(self._on_bubble_clicked)

    def __bubble_width(self) -> int:
        """一枚气泡该多宽：视口可用宽的 72%，窄面板兜底（比例宽不足最小宽给
        最小宽，可用宽连最小宽都不够就全宽）。"""
        margins = self._layout.contentsMargins()
        usable = self.viewport().width() - margins.left() - margins.right()
        if usable <= BUBBLE_MIN_WIDTH:
            return usable
        return max(BUBBLE_MIN_WIDTH, min(int(usable * BUBBLE_WIDTH_RATIO), usable))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 视口宽度变了，已存在的气泡跟着换固定宽（高度各自在 resizeEvent 重算）。
        width = self.__bubble_width()
        for bubble in self.bubbles():
            bubble.set_bubble_width(width)

    def _reinsert(self, widget: QWidget) -> None:
        """排序重建时按新方向放回，保留各自的连接与状态。"""
        if isinstance(widget, Bubble):
            self._insert(widget)
        else:
            self.add_note(widget) if isinstance(widget, SystemNote) else None

    def _on_bubble_clicked(self, key: object) -> None:
        sender = self.sender()
        if isinstance(sender, Bubble):
            self._select(sender)

    def _select(self, bubble: Bubble) -> None:
        for widget in self._widgets():
            if isinstance(widget, Bubble):
                widget.set_selected(widget is bubble)
        self._selected_key = bubble.key
        self.selectionChanged.emit(bubble.key)

    def _restore_selection(self) -> None:
        """排序重建后按键找回选中；找不到就算了（可能已被过滤 / 清空）。"""
        if self._selected_key is None:
            return
        for bubble in self.bubbles():
            if bubble.key == self._selected_key:
                bubble.set_selected(True)
                return

    def _widgets(self) -> list[QWidget]:
        widgets: list[QWidget] = []
        for i in range(self._layout.count()):
            item = self._layout.itemAt(i)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widgets.append(widget)
        return widgets

    def _take_all(self) -> list[QWidget]:
        widgets = []
        while self._layout.count() > 1:  # 留住末尾的 stretch
            item = self._layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widgets.append(widget)
        return widgets

    def _apply_filter_to(self, widget: QWidget) -> None:
        if not isinstance(widget, Bubble) or not self._filter:
            widget.show()
            return
        widget.setVisible(self._filter in widget.search_text)
