from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import MessageBoxBase, SubtitleLabel, TextBrowser
from qfluentwidgets.components.widgets.line_edit import PlainTextEdit


class TextCopyDialog(MessageBoxBase):
    def __init__(self, content: str, title: str, parent: QWidget):
        super().__init__(parent)
        self.content = content  # 保存原始内容
        self.title = title

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.title)

        self.edit = TextBrowser(self)
        self.edit.setText(self.content)

        self.yesButton.setText(self.tr("Copy"))

    def __init_layout(self):
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(600)

    def __connect_signal_to_slot(self):
        self.yesButton.clicked.connect(
            lambda: QApplication.clipboard().setText(self.content)
        )

    def showEvent(self, e):
        super().showEvent(e)
        self.edit.setFocus()


class CommentDialog(MessageBoxBase):
    """Edit a flow's annotation (``flow.comment``)."""

    def __init__(self, initial: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = SubtitleLabel(self.tr("Edit comment"), self)
        self.edit = PlainTextEdit(self)
        self.edit.setPlainText(initial)
        self.edit.setPlaceholderText(
            self.tr("Add a note to help you identify this flow")
        )
        self.edit.setFixedHeight(120)

        self.yesButton.setText(self.tr("Save"))
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(520)

    def comment(self) -> str:
        return self.edit.toPlainText()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self.edit.setFocus()
