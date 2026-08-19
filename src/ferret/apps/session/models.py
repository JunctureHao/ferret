from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from ferret.core.mitm import human


class SessionSource(StrEnum):
    CAPTURE = "capture"
    IMPORT = "import"


_SOURCE_LABELS: dict[SessionSource, str] = {
    SessionSource.CAPTURE: "抓包",
    SessionSource.IMPORT: "导入",
}


@dataclass(frozen=True, slots=True)
class SessionMeta:
    schema_version: int
    session_id: str
    name: str
    path: Path
    created_at: datetime
    modified_at: datetime
    flow_count: int
    file_size: int
    source: SessionSource


class SessionTableModel(QAbstractTableModel):
    HEADERS: ClassVar[list[str]] = ["名称", "修改时间", "流量数", "大小", "来源"]
    SORT_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._sessions: list[SessionMeta] = []

    def set_sessions(self, sessions: list[SessionMeta]) -> None:
        self.beginResetModel()
        self._sessions = list(sessions)
        self.endResetModel()

    def session_at(self, row: int) -> SessionMeta | None:
        if 0 <= row < len(self._sessions):
            return self._sessions[row]
        return None

    def session_by_id(self, session_id: str) -> SessionMeta | None:
        for s in self._sessions:
            if s.session_id == session_id:
                return s
        return None

    def remove_session(self, session_id: str) -> None:
        for i, s in enumerate(self._sessions):
            if s.session_id == session_id:
                self.beginRemoveRows(QModelIndex(), i, i)
                self._sessions.pop(i)
                self.endRemoveRows()
                return

    def update_session(self, old_session_id: str, session: SessionMeta) -> None:
        for i, s in enumerate(self._sessions):
            if s.session_id == old_session_id:
                self._sessions[i] = session
                idx_start = self.index(i, 0)
                idx_end = self.index(i, self.columnCount() - 1)
                self.dataChanged.emit(idx_start, idx_end)
                return
        self.beginInsertRows(QModelIndex(), len(self._sessions), len(self._sessions))
        self._sessions.append(session)
        self.endInsertRows()

    def add_session(self, session: SessionMeta) -> None:
        self.beginInsertRows(QModelIndex(), len(self._sessions), len(self._sessions))
        self._sessions.append(session)
        self.endInsertRows()

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
            return self.HEADERS[section]
        return None

    def rowCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self._sessions)

    def columnCount(
        self, parent: QModelIndex | QPersistentModelIndex | None = None
    ) -> int:
        return len(self.HEADERS)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if not index.isValid() or not self._sessions:
            return None

        row = index.row()
        if not (0 <= row < len(self._sessions)):
            return None

        session = self._sessions[row]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return session.name
            if col == 1:
                return session.modified_at.strftime("%Y-%m-%d %H:%M")
            if col == 2:
                return session.flow_count
            if col == 3:
                return human.pretty_size(session.file_size)
            if col == 4:
                return _SOURCE_LABELS.get(session.source, session.source.value)
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in (2, 3):
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.UserRole:
            return session

        if role == self.SORT_ROLE:
            values = (
                session.name.casefold(),
                session.modified_at.timestamp(),
                session.flow_count,
                session.file_size,
                session.source.value,
            )
            return values[col] if 0 <= col < len(values) else None

        return None


class SessionFilterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._filter_text: str = ""
        self.setDynamicSortFilter(True)
        self.setSortRole(SessionTableModel.SORT_ROLE)

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
        if not isinstance(model, SessionTableModel):
            return True
        session = model.session_at(source_row)
        if session is None:
            return True
        return (
            self._filter_text in session.name.lower()
            or self._filter_text in session.source.value.lower()
        )
