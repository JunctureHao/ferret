"""Keep unedited header bytes outside Qt's lossy Unicode conversion."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Slot
from PySide6.QtGui import QTextCursor, QTextFormat

from .widgets import ItemDualPanel, ItemSource, items_from_text, normalize_items

_HEADER_ID = int(QTextFormat.Property.UserProperty) + 101
_HEADER_END_ID = _HEADER_ID + 1


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
        self._loading_headers = False
        super().__init__(editable, parent)
        self.table.items_reset.connect(self._on_table_reset)

    @Slot(object)
    def _on_table_reset(self, pairs: ItemSource) -> None:
        if not self._loading_headers:
            # The read-only table's "original order" action rebuilds its items.
            # Reattach byte identities after that programmatic reset as well.
            self.set_items(pairs)

    def set_items(self, items: ItemSource):
        pairs = normalize_items(items)
        self._loading_headers = True
        try:
            super().set_items(pairs)
            self._text_headers.clear()
            self._table_headers.clear()
            table = self.table._table_widget
            block = self.text.code_widget.document().firstBlock()
            with QSignalBlocker(self.text.code_widget):
                for index, raw in enumerate(pairs, start=1):
                    key, value = table.item(index - 1, 0), table.item(index - 1, 1)
                    assert key is not None and value is not None
                    key.setData(_HEADER_ID, index)
                    self._table_headers[index] = (
                        (key.text().strip(), value.text()),
                        raw,
                    )
                    if block.isValid():
                        visible = items_from_text(block.text())
                        if visible:
                            self._text_headers[index] = (visible[0], raw)
                            cursor = QTextCursor(block)
                            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                            formatting = cursor.charFormat()
                            formatting.clearProperty(_HEADER_ID)
                            formatting.clearProperty(_HEADER_END_ID)
                            cursor.setCharFormat(formatting)
                            # Separate first/last-character anchors survive Qt undo.
                            # Plain-text insertion can inherit either surrounding
                            # format, but cannot create both anchors for a new row.
                            for edge, property_id in (
                                (QTextCursor.MoveOperation.StartOfBlock, _HEADER_ID),
                                (QTextCursor.MoveOperation.EndOfBlock, _HEADER_END_ID),
                            ):
                                cursor.movePosition(edge)
                                if property_id == _HEADER_END_ID:
                                    cursor.movePosition(
                                        QTextCursor.MoveOperation.PreviousCharacter
                                    )
                                cursor.movePosition(
                                    QTextCursor.MoveOperation.NextCharacter,
                                    QTextCursor.MoveMode.KeepAnchor,
                                )
                                formatting = cursor.charFormat()
                                formatting.setProperty(property_id, index)
                                cursor.setCharFormat(formatting)
                        block = block.next()
            # Loading/annotating a message is the undo baseline. Undo should
            # restore user edits, never remove the identity anchors themselves.
            self.text.code_widget.document().clearUndoRedoStacks()
        finally:
            self._loading_headers = False

    def items(self) -> list[tuple[str, str]]:
        if self._loading_headers:
            return super().items()
        result = []
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
                    result.extend(
                        origin[1] if origin and pair == origin[0] else pair
                        for pair in pairs
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
                origin = self._table_headers.get(key.data(_HEADER_ID))
                result.append(origin[1] if origin and pair == origin[0] else pair)
        return result

    @Slot()
    def _show_text_page(self):
        if self._editable and self.stack.currentWidget() is self.table:
            self.set_items(self.items())
        self.stack.setCurrentWidget(self.text)

    @Slot()
    def _show_table_page(self):
        if self._editable and self.stack.currentWidget() is self.text:
            self.set_items(self.items())
        self.stack.setCurrentWidget(self.table)
