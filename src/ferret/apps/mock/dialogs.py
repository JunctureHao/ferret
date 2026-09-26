"""字符串列表编辑框：忽略 Query 参数 / 匹配请求头两个高级匹配项共用。"""

from PySide6.QtWidgets import QWidget
from qfluentwidgets import MessageBoxBase, PlainTextEdit, SubtitleLabel


class StringListDialog(MessageBoxBase):
    """一行一条的字符串列表编辑。空行忽略、不去重（原生命令按原文匹配）。"""

    def __init__(
        self,
        title: str,
        hint: str,
        values: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.title_label = SubtitleLabel(title, self)
        self.edit = PlainTextEdit(self)
        self.edit.setPlainText("\n".join(values))
        self.edit.setPlaceholderText(hint)
        self.edit.setFixedHeight(160)

        self.yesButton.setText(self.tr("保存"))
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.edit)
        self.widget.setMinimumWidth(520)

    def items(self) -> list[str]:
        return [
            line.strip() for line in self.edit.toPlainText().splitlines() if line.strip()
        ]
