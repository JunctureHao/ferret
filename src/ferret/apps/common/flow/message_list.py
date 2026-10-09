"""按行呈现消息；协议相关的文案与详情由调用方提供。"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractListModel,
    QItemSelectionModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSignalBlocker,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyleOptionViewItem, QWidget
from qfluentwidgets import (
    FluentIcon,
    ListItemDelegate,
    ListView,
    getFont,
    isDarkTheme,
)

_ROW_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_SEQUENCE_ROLE = _ROW_ROLE + 1
_ROW_HEIGHT = 40
_EDGE_SLACK = 4


@dataclass(frozen=True, slots=True)
class MessageRow:
    key: object
    icon: FluentIcon
    metadata: str
    preview: str
    search_text: str
    payload: object


class _MessageModel(QAbstractListModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.rows: list[MessageRow] = []
        self._first_sequence = 0

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.preview
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{row.metadata}\n{row.preview}"
        if role == _ROW_ROLE:
            return row
        if role == _SEQUENCE_ROLE:
            # 时间戳、事件 id 都可能重复；排序只认实际的插入先后。
            return self._first_sequence + index.row()
        return None

    def set_rows(self, rows: list[MessageRow]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        self._first_sequence = 0
        self.endResetModel()

    def add(self, row: MessageRow) -> None:
        position = len(self.rows)
        self.beginInsertRows(QModelIndex(), position, position)
        self.rows.append(row)
        self.endInsertRows()

    def evict_oldest(self, count: int = 1) -> None:
        self.beginRemoveRows(QModelIndex(), 0, count - 1)
        del self.rows[:count]
        self._first_sequence += count
        self.endRemoveRows()


class _MessageFilter(QSortFilterProxyModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.needle = ""
        self.setSortRole(_SEQUENCE_ROLE)

    def set_filter(self, text: str) -> None:
        self.beginFilterChange()
        self.needle = text
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        if not self.needle:
            return True
        model = self.sourceModel()
        assert isinstance(model, _MessageModel)
        return self.needle in model.rows[source_row].search_text.casefold()


class _MessageDelegate(ListItemDelegate):
    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:
        super().initStyleOption(option, index)
        # 基类仍绘制 Fluent 选中/悬停背景，箭头与正文由下面的单行布局绘制。
        option.text = ""

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:
        return QSize(160, _ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        row = index.data(_ROW_ROLE)
        if not isinstance(row, MessageRow):
            return
        super().paint(painter, QStyleOptionViewItem(option), index)
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect
        row.icon.render(
            painter, QRect(rect.x() + 16, rect.y() + (rect.height() - 12) // 2, 12, 12)
        )
        width = max(0, rect.width() - 56)
        painter.setFont(getFont(13))
        painter.setPen(QColor("#FFFFFF" if isDarkTheme() else "#202020"))
        # QPainter.drawText 始终是纯文本，<b> 等协议原文不会变成富文本。
        text = row.preview.replace("\r", " ").replace("\n", " ").replace("\t", " ")
        text = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, width
        )
        painter.drawText(
            QRect(rect.x() + 42, rect.y(), width, rect.height()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )
        painter.restore()


class MessageList(ListView):
    """固定行高的消息列表，维护过滤、选择与实时追加时的视口。"""

    messageSelected = Signal(object)
    messageActivated = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._descending = False
        self._selected: MessageRow | None = None
        self._source = _MessageModel(self)
        self._proxy = _MessageFilter(self)
        self._proxy.setSourceModel(self._source)
        self._proxy.sort(0, Qt.SortOrder.AscendingOrder)
        self.setModel(self._proxy)
        self.setItemDelegate(_MessageDelegate(self))
        self.setAlternatingRowColors(True)
        self.setUniformItemSizes(True)
        self.setWordWrap(False)
        self.setResizeMode(ListView.ResizeMode.Adjust)
        self.setSelectionMode(ListView.SelectionMode.SingleSelection)
        self.setEditTriggers(ListView.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(ListView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        selection = self.selectionModel()
        assert selection is not None
        selection.selectionChanged.connect(self._publish_selection)
        self.clicked.connect(self._activate_message)
        self.activated.connect(self._activate_message)

    @property
    def descending(self) -> bool:
        return self._descending

    @property
    def filter_text(self) -> str:
        return self._proxy.needle

    def message_count(self) -> int:
        return len(self._source.rows)

    def visible_count(self) -> int:
        return self._proxy.rowCount()

    def rows(self) -> list[MessageRow]:
        """返回按插入先后排列的全部行，包含被过滤的行。"""
        return list(self._source.rows)

    def selected_row(self) -> MessageRow | None:
        return self._selected

    def set_rows(self, rows: list[MessageRow]) -> None:
        selection = self.selectionModel()
        assert selection is not None
        with QSignalBlocker(selection):
            self._source.set_rows(rows)
            selection.clear()
        self._setHoverRow(-1)
        self._setPressedRow(-1)
        self._publish_selection(force=True)
        self.scroll_to_newest()

    def clear(self) -> None:
        self.set_rows([])

    def add(self, row: MessageRow) -> None:
        with self._preserve_state(follow_newest=True):
            self._source.add(row)

    def evict_oldest(self, count: int = 1) -> None:
        count = min(count, len(self._source.rows))
        if count > 0:
            with self._preserve_state(follow_newest=True):
                self._source.evict_oldest(count)

    def set_filter(self, text: str) -> None:
        text = text.strip().casefold()
        if text != self.filter_text:
            with self._preserve_state():
                self._proxy.set_filter(text)

    def set_descending(self, descending: bool) -> None:
        if descending != self._descending:
            with self._preserve_state(follow_newest=True):
                self._descending = descending
                order = (
                    Qt.SortOrder.DescendingOrder
                    if descending
                    else Qt.SortOrder.AscendingOrder
                )
                self._proxy.sort(0, order)

    def at_newest_edge(self) -> bool:
        bar = self.verticalScrollBar()
        if self._descending:
            return bar.value() <= bar.minimum() + _EDGE_SLACK
        return bar.value() >= bar.maximum() - _EDGE_SLACK

    def scroll_to_newest(self) -> None:
        self.doItemsLayout()
        bar = self.verticalScrollBar()
        bar.setValue(bar.minimum() if self._descending else bar.maximum())

    def _publish_selection(self, *_args, force: bool = False) -> None:
        indexes = self.selectedIndexes()
        row = indexes[0].data(_ROW_ROLE) if indexes else None
        self.updateSelectedRows()
        if force or row is not self._selected:
            self._selected = row
            self.messageSelected.emit(row)

    def _activate_message(self, index: QModelIndex) -> None:
        row = index.data(_ROW_ROLE)
        if isinstance(row, MessageRow):
            # 点击已经选中的行也要再次打开；上下键只更新选择，不触发激活。
            self.messageActivated.emit(row)

    @contextmanager
    def _preserve_state(self, *, follow_newest: bool = False) -> Generator[None]:
        follow = follow_newest and self.at_newest_edge()
        indexes = self.selectedIndexes()
        selected = QPersistentModelIndex(
            self._proxy.mapToSource(indexes[0]) if indexes else QModelIndex()
        )
        top = self.indexAt(QPoint(self.viewport().width() // 2, 0))
        anchor = QPersistentModelIndex(self._proxy.mapToSource(top))
        offset = self.visualRect(top).top() if top.isValid() else 0
        selection = self.selectionModel()
        assert selection is not None
        # Qt 移除当前行时会临时改选邻行；这一过程不能泄漏到详情区。
        with QSignalBlocker(selection):
            yield
            current = self._proxy.mapFromSource(selected)
            if current.isValid():
                selection.setCurrentIndex(
                    current, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )
            else:
                selection.clear()
        self._setHoverRow(-1)
        self._setPressedRow(-1)
        self._publish_selection()
        self.doItemsLayout()
        if follow:
            self.scroll_to_newest()
        elif anchor.isValid():
            top = self._proxy.mapFromSource(anchor)
            if top.isValid():
                self.scrollTo(top, ListView.ScrollHint.PositionAtTop)
                bar = self.verticalScrollBar()
                bar.setValue(bar.value() - offset)
