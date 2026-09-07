from datetime import datetime

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
)


class SessionNameDialog(MessageBoxBase):
    """会话命名对话框（保存/重命名共用）"""

    def __init__(
        self,
        title: str,
        default_name: str | None = None,
        flow_count: int | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._flow_count = flow_count
        self.__init_widget(title, default_name)
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self, title: str, default_name: str | None):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title)

        self.name_edit = LineEdit(self)
        name = default_name or datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        self.name_edit.setText(name)
        self.name_edit.selectAll()

        self.hint_label = CaptionLabel(self)
        if self._flow_count is not None:
            self.hint_label.setText(
                self.tr("当前包含 {} 条 HTTP 流量").format(self._flow_count)
            )
        else:
            self.hint_label.setText("")

        self.yesButton.setText(self.tr("保存"))
        self.yesButton.setEnabled(False)
        self._validate_name()

        QTimer.singleShot(0, self.name_edit.setFocus)

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.name_edit)
        layout.addWidget(self.hint_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(380)

    def __connect_signal_to_slot(self):
        self.name_edit.textChanged.connect(self._validate_name)

    def _validate_name(self):
        text = self.name_edit.text().strip()
        self.yesButton.setEnabled(bool(text))

    def get_name(self) -> str:
        return self.name_edit.text().strip()


class SessionDeleteDialog(MessageBoxBase):
    """删除会话确认对话框"""

    def __init__(self, session_name: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget(session_name)
        self.__init_layout()

    def __init_widget(self, session_name: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr('删除"{}"？').format(session_name))

        self.desc_label = BodyLabel(self)
        self.desc_label.setText(self.tr("该操作会永久删除本地 Flow 文件，无法撤销。"))
        self.desc_label.setWordWrap(True)

        self.yesButton.setText(self.tr("删除"))
        self.cancelButton.setText(self.tr("取消"))

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(380)
