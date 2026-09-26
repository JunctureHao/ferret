"""Mock 响应池的表格模型与文案标记。"""

from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QCoreApplication,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from ferret.utils.i18n import QT_TRANSLATE_NOOP


class MockPoolTableModel(QAbstractTableModel):
    """池条目表。行 = facade `mock_snapshot()` 的一条纯数据 dict，
    `id` 即池条目身份（= 来源流量行 id），删除按它走。"""

    HEADERS: ClassVar[list[str]] = [
        QT_TRANSLATE_NOOP("MockPoolTableModel", "方法"),
        QT_TRANSLATE_NOOP("MockPoolTableModel", "URL"),
        QT_TRANSLATE_NOOP("MockPoolTableModel", "状态"),
        QT_TRANSLATE_NOOP("MockPoolTableModel", "大小"),
    ]

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._entries: list[dict[str, Any]] = []

    def set_entries(self, entries: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._entries = list(entries)
        self.endResetModel()

    def entry_at(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._entries):
            return self._entries[row]
        return None

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if (
            role == Qt.ItemDataRole.DisplayRole
            and orientation == Qt.Orientation.Horizontal
        ):
            return QCoreApplication.translate(
                "MockPoolTableModel", self.HEADERS[section]
            )
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._entries)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self.HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid():
            return None
        entry = self.entry_at(index.row())
        if entry is None:
            return None
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return entry.get("method", "")
            if col == 1:
                return entry.get("url", "")
            if col == 2:
                return entry.get("status", "") or ""
            if col == 3:
                return entry.get("size", "")
            return None
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == 2:
                return Qt.AlignmentFlag.AlignCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        if role == Qt.ItemDataRole.UserRole:
            return entry
        return None


class MockPoolFilterProxyModel(QSortFilterProxyModel):
    """按方法 / URL / 状态做文字过滤（大小列不参与）。"""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter_text = ""

    def set_filter_text(self, text: str) -> None:
        self.beginFilterChange()
        self._filter_text = (text or "").strip().lower()
        self.endFilterChange()

    def filterAcceptsRow(
        self, source_row: int, source_parent: QModelIndex | QPersistentModelIndex
    ) -> bool:
        if not self._filter_text:
            return True
        model = self.sourceModel()
        if not isinstance(model, MockPoolTableModel):
            return True
        entry = model.entry_at(source_row)
        if entry is None:
            return True
        haystack = " ".join(
            (
                str(entry.get("method", "")),
                str(entry.get("url", "")),
                str(entry.get("status", "")),
            )
        ).lower()
        return self._filter_text in haystack
