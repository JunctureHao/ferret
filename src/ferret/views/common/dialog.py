from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import MessageBoxBase, SubtitleLabel, TextBrowser


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

        self.yesButton.setText(self.tr("复制"))

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
