"""证书页的确认对话框。"""

from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, MessageBoxBase, SubtitleLabel


class RegenerateCertDialog(MessageBoxBase):
    """重新生成 CA 的确认框。

    这是本页唯一一个不可撤销的操作：新私钥一出，系统里已信任的旧 CA 立刻失效，
    所有已导入过证书的设备都得重新导入，所以必须先说清代价。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("Regenerate the CA certificate?"))

        self.desc_label = BodyLabel(self)
        self.desc_label.setText(
            self.tr(
                "This deletes the existing private key and certificate and generates a "
                "brand new pair. It cannot be undone."
            )
        )
        self.desc_label.setWordWrap(True)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setText(
            self.tr(
                "After generating, reinstall it into the system trust store; other devices "
                "that imported the certificate have to import it again."
            )
        )
        self.hint_label.setWordWrap(True)

        self.yesButton.setText(self.tr("Regenerate"))
        self.cancelButton.setText(self.tr("Cancel"))

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.hint_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(400)
