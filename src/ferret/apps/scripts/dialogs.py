"""Dialogs for creating and removing user scripts."""

from pathlib import Path

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    LineEdit,
    MessageBoxBase,
    SubtitleLabel,
)

from ferret.apps.scripts.models import trust_warning

# Windows 文件名禁用字符；路径分隔符也在内，顺手拦住「往别处写」。
_ILLEGAL_CHARS = set(r'<>:"/\|?*')

# Windows 保留设备名：`con.py` 这种文件建不出来，提前拦比 OSError 好看。
_RESERVED_STEMS = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_DEFAULT_STEM = "script"


class NewScriptDialog(MessageBoxBase):
    """新建脚本：只要一个文件名，落盘目录由应用托管（§3.4）。"""

    def __init__(
        self,
        directory: Path,
        *,
        title: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._directory = directory
        self.__init_widget(title)
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self, title: str):
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(title or self.tr("新建脚本"))

        self.name_edit = LineEdit(self)
        self.name_edit.setText(f"{_DEFAULT_STEM}.py")
        # 只选中主名：接着打字就换掉名字、`.py` 留着（后缀是必填校验项）。
        self.name_edit.setSelection(0, len(_DEFAULT_STEM))

        self.dir_label = CaptionLabel(self)
        self.dir_label.setText(self.tr("保存到：{}").format(self._directory))
        self.dir_label.setWordWrap(True)

        self.warning_label = CaptionLabel(self)
        self.warning_label.setText(trust_warning())
        self.warning_label.setWordWrap(True)

        self.error_label = CaptionLabel(self)
        self.error_label.setText("")
        self.error_label.setWordWrap(True)

        self.yesButton.setText(self.tr("创建"))
        self.cancelButton.setText(self.tr("取消"))
        self._validate()

        QTimer.singleShot(0, self.name_edit.setFocus)

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.name_edit)
        layout.addWidget(self.error_label)
        layout.addWidget(self.dir_label)
        layout.addWidget(self.warning_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(420)

    def __connect_signal_to_slot(self):
        self.name_edit.textChanged.connect(self._validate)

    def get_filename(self) -> str:
        return self.name_edit.text().strip()

    def _check(self, name: str) -> str:
        """返回第一条不通过的理由，全通过返回空串。"""
        if not name:
            return self.tr("请输入文件名")
        if not name.lower().endswith(".py"):
            return self.tr("文件名需以 .py 结尾")
        bad = sorted(_ILLEGAL_CHARS & set(name))
        if bad:
            return self.tr("文件名不能包含 {}").format(" ".join(bad))
        stem = name[:-3]
        if not stem.strip() or stem.strip(".") == "":
            return self.tr("请输入文件名")
        if stem.lower() in _RESERVED_STEMS:
            return self.tr("{} 是系统保留名").format(stem)
        if (self._directory / name).exists():
            return self.tr("同名文件已存在")
        return ""

    @Slot()
    def _validate(self):
        reason = self._check(self.get_filename())
        self.error_label.setText(reason)
        self.yesButton.setEnabled(not reason)


class ScriptRemoveDialog(MessageBoxBase):
    """移除确认：只在选中里有 new 条目时才弹（§3.4 那张表）。

    勾选框默认不勾 —— 「移除条目」与「删文件」是两件事，后者不可撤销。
    """

    def __init__(self, names: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget(names)
        self.__init_layout()

    def __init_widget(self, names: list[str]):
        self.title_label = SubtitleLabel(self)
        if len(names) == 1:
            self.title_label.setText(self.tr('移除"{}"？').format(names[0]))
        else:
            self.title_label.setText(self.tr("移除 {} 个脚本？").format(len(names)))

        self.desc_label = BodyLabel(self)
        self.desc_label.setText(self.tr("默认只从列表移除，脚本文件保留在磁盘上。"))
        self.desc_label.setWordWrap(True)

        self.delete_check = CheckBox(self.tr("同时删除脚本文件（不可撤销）"), self)
        self.delete_check.setChecked(False)
        # 导入的脚本指向用户自己的文件，任何情况下都不删（控制器同样兜一层）。
        self.delete_check.setToolTip(self.tr("只删除应用内新建的脚本文件"))

        self.yesButton.setText(self.tr("移除"))
        self.cancelButton.setText(self.tr("取消"))

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.delete_check)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(400)

    def delete_files(self) -> bool:
        return self.delete_check.isChecked()
