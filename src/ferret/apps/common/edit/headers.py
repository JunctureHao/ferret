"""Keep unedited header bytes outside Qt's lossy Unicode conversion."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QSignalBlocker, Slot
from PySide6.QtGui import QTextBlock, QTextCharFormat, QTextCursor, QTextFormat

from .widgets import ItemDualPanel, ItemSource, items_from_text, normalize_items

_HEADER_ID = int(QTextFormat.Property.UserProperty) + 101
_HEADER_END_ID = _HEADER_ID + 1


@dataclass(frozen=True)
class _HeaderRow:
    pair: tuple[str, str]
    identity: int | None
    position: int


class HeaderDualPanel(ItemDualPanel):
    """Keep each original header associated with its text and table row.

    Two distinct byte values can have identical Qt text. Value matching cannot
    distinguish deleting one of these duplicate rows; character formats and
    item data carry an opaque row ID as the user edits or removes surrounding
    rows. Raw strings themselves never pass through QVariant/QString.
    """

    def __init__(self, editable=False, parent=None):
        self._text_headers: dict[int, tuple[tuple[str, str], tuple[str, str]]] = {}
        self._table_headers: dict[int, tuple[tuple[str, str], tuple[str, str]]] = {}
        self._managed_headers: set[int] = set()
        self._next_header_id = 0
        self._loading_headers = False
        super().__init__(editable, parent)
        self.table.items_reset.connect(self._on_table_reset)
        self.changed.connect(self._on_headers_edited)

    @Slot(object)
    def _on_table_reset(self, pairs: ItemSource) -> None:
        if not self._loading_headers:
            # The read-only table's "original order" action rebuilds its items.
            # Reattach byte identities after that programmatic reset as well.
            self.set_items(pairs)

    def set_items(self, items: ItemSource):
        """Load an external request; none of its headers belongs to the editor."""
        self._managed_headers.clear()
        self._load_rows(
            [
                _HeaderRow(pair, None, index)
                for index, pair in enumerate(normalize_items(items))
            ]
        )

    def _new_identity(self) -> int:
        self._next_header_id += 1
        return self._next_header_id

    def _load_rows(self, rows: list[_HeaderRow]) -> None:
        """Rebuild both views while retaining the identity of surviving rows."""
        self._loading_headers = True
        try:
            super().set_items([row.pair for row in rows])
            self._text_headers.clear()
            self._table_headers.clear()
            table = self.table._table_widget
            block = self.text.code_widget.document().firstBlock()
            with QSignalBlocker(self.text.code_widget), QSignalBlocker(table):
                for index, row in enumerate(rows):
                    identity = row.identity or self._new_identity()
                    key, value = table.item(index, 0), table.item(index, 1)
                    assert key is not None and value is not None
                    key.setData(_HEADER_ID, identity)
                    self._table_headers[identity] = (
                        (key.text().strip(), value.text()),
                        row.pair,
                    )
                    if block.isValid():
                        self._annotate_text_row(block, identity, row.pair)
                        block = block.next()
            # Loading/annotating a message is the undo baseline. Undo should
            # restore user edits, never remove the identity anchors themselves.
            self.text.code_widget.document().clearUndoRedoStacks()
        finally:
            self._loading_headers = False

    def _annotate_text_row(
        self, block: QTextBlock, identity: int, raw: tuple[str, str]
    ) -> None:
        visible = items_from_text(block.text())
        if not visible:
            return
        self._text_headers[identity] = (visible[0], raw)
        cursor = QTextCursor(block)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        formatting = cursor.charFormat()
        formatting.clearProperty(_HEADER_ID)
        formatting.clearProperty(_HEADER_END_ID)
        cursor.setCharFormat(formatting)
        # Separate first/last-character anchors survive Qt undo. Plain-text
        # insertion may inherit one anchor, but cannot create both for a new row.
        for edge, property_id in (
            (QTextCursor.MoveOperation.StartOfBlock, _HEADER_ID),
            (QTextCursor.MoveOperation.EndOfBlock, _HEADER_END_ID),
        ):
            cursor.movePosition(edge)
            if property_id == _HEADER_END_ID:
                cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
            cursor.movePosition(
                QTextCursor.MoveOperation.NextCharacter,
                QTextCursor.MoveMode.KeepAnchor,
            )
            formatting = cursor.charFormat()
            formatting.setProperty(property_id, identity)
            cursor.setCharFormat(formatting)

    def _rows(self) -> list[_HeaderRow]:
        result: list[_HeaderRow] = []
        if self.stack.currentWidget() is self.text:
            block = self.text.code_widget.document().firstBlock()
            while block.isValid():
                pairs = items_from_text(block.text())
                if pairs:
                    cursor = QTextCursor(block)
                    cursor.movePosition(
                        QTextCursor.MoveOperation.NextCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    origin = self._text_headers.get(
                        cursor.charFormat().property(_HEADER_ID)
                    )
                    origin_id = cursor.charFormat().property(_HEADER_ID)
                    cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                    cursor.movePosition(QTextCursor.MoveOperation.PreviousCharacter)
                    cursor.movePosition(
                        QTextCursor.MoveOperation.NextCharacter,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    if cursor.charFormat().property(_HEADER_END_ID) != origin_id:
                        origin = None
                    for pair in pairs:
                        unchanged = origin is not None and pair == origin[0]
                        result.append(
                            _HeaderRow(
                                origin[1] if unchanged else pair,
                                origin_id if unchanged else None,
                                block.position(),
                            )
                        )
                block = block.next()
        else:
            table = self.table._table_widget
            table.commit_active_editor()
            for row in range(table.rowCount()):
                key, value = table.item(row, 0), table.item(row, 1)
                if key is None or not key.text().strip():
                    continue
                pair = (key.text().strip(), value.text() if value is not None else "")
                identity = key.data(_HEADER_ID)
                origin = self._table_headers.get(identity)
                unchanged = origin is not None and pair == origin[0]
                result.append(
                    _HeaderRow(
                        origin[1] if unchanged else pair,
                        identity if unchanged else None,
                        row,
                    )
                )
        return result

    def items(self) -> list[tuple[str, str]]:
        if self._loading_headers:
            return super().items()
        return [row.pair for row in self._rows()]

    def saved_managed_rows(self) -> tuple[int, ...]:
        """Snapshot ownership as positions in items(), not ephemeral Qt row IDs."""
        return tuple(
            index
            for index, row in enumerate(self._rows())
            if row.identity in self._managed_headers
        )

    def restore_managed_rows(self, indices: Sequence[int]) -> None:
        """Reattach ownership after set_items has assigned fresh row identities."""
        rows = self._rows()
        self._managed_headers = {
            identity
            for index in indices
            if 0 <= index < len(rows) and (identity := rows[index].identity) is not None
        }

    @Slot()
    def _on_headers_edited(self) -> None:
        if not self._loading_headers and self._managed_headers:
            # Relinquish ownership on the first user edit or deletion. Retyping
            # the previous value must never make a handwritten row automatic.
            self._managed_headers.intersection_update(
                row.identity for row in self._rows() if row.identity is not None
            )

    def is_managed_header(self, name: str) -> bool:
        """Whether every current header with this name is still editor-owned."""
        rows = [
            row for row in self._rows() if row.pair[0].casefold() == name.casefold()
        ]
        return bool(rows) and all(row.identity in self._managed_headers for row in rows)

    def set_managed_header(self, name: str, value: str | None) -> bool:
        """Add/update owned headers, or remove only owned rows when value is None.

        Any handwritten duplicate prevents an automatic update. Removal remains
        row-specific so selecting no body never deletes a user's duplicate.
        """
        rows = [
            row for row in self._rows() if row.pair[0].casefold() == name.casefold()
        ]
        owned = [row for row in rows if row.identity in self._managed_headers]
        if value is not None and len(owned) != len(rows):
            return False
        if value is None and not owned:
            return False
        self._loading_headers = True
        try:
            if self.stack.currentWidget() is self.text:
                self._change_text_headers(name, value, owned)
            else:
                self._change_table_headers(name, value, owned)
        finally:
            self._loading_headers = False
        self.changed.emit()
        return True

    def _change_text_headers(
        self, name: str, value: str | None, rows: list[_HeaderRow]
    ) -> None:
        editor = self.text.code_widget
        document = editor.document()
        cursor = QTextCursor(document)
        with QSignalBlocker(editor):
            cursor.beginEditBlock()
            try:
                if value is not None and not rows:
                    identity = self._new_identity()
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    if cursor.block().text():
                        cursor.insertBlock()
                    cursor.insertText(f"{name}: {value}", QTextCharFormat())
                    self._annotate_text_row(cursor.block(), identity, (name, value))
                    self._managed_headers.add(identity)
                for row in reversed(rows):
                    assert row.identity is not None
                    block = document.findBlock(row.position)
                    cursor.setPosition(block.position())
                    if value is None:
                        if block.next().isValid():
                            cursor.setPosition(
                                block.next().position(), QTextCursor.MoveMode.KeepAnchor
                            )
                        else:
                            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
                            cursor.setPosition(
                                max(0, block.position() - 1),
                                QTextCursor.MoveMode.KeepAnchor,
                            )
                        cursor.removeSelectedText()
                        self._managed_headers.discard(row.identity)
                    elif value != row.pair[1]:
                        cursor.movePosition(
                            QTextCursor.MoveOperation.EndOfBlock,
                            QTextCursor.MoveMode.KeepAnchor,
                        )
                        pair = (row.pair[0], value)
                        cursor.insertText(f"{pair[0]}: {value}", QTextCharFormat())
                        self._annotate_text_row(cursor.block(), row.identity, pair)
            finally:
                cursor.endEditBlock()

    def _change_table_headers(
        self, name: str, value: str | None, rows: list[_HeaderRow]
    ) -> None:
        table = self.table._table_widget
        with QSignalBlocker(table):
            if value is not None and not rows:
                identity = self._new_identity()
                index = table.rowCount()
                table.insertRow(index)
                key, cell = table._make_item(name), table._make_item(value)
                key.setData(_HEADER_ID, identity)
                table.setItem(index, 0, key)
                table.setItem(index, 1, cell)
                self._table_headers[identity] = (
                    (key.text().strip(), cell.text()),
                    (name, value),
                )
                self._managed_headers.add(identity)
            for row in reversed(rows):
                assert row.identity is not None
                if value is None:
                    table.removeRow(row.position)
                    self._managed_headers.discard(row.identity)
                else:
                    cell = table.item(row.position, 1)
                    assert cell is not None
                    cell.setText(value)
                    pair = (row.pair[0], value)
                    self._table_headers[row.identity] = (pair, pair)

    @Slot()
    def _show_text_page(self):
        if self._editable and self.stack.currentWidget() is self.table:
            self._load_rows(self._rows())
        self.stack.setCurrentWidget(self.text)

    @Slot()
    def _show_table_page(self):
        if self._editable and self.stack.currentWidget() is self.text:
            self._load_rows(self._rows())
        self.stack.setCurrentWidget(self.table)
