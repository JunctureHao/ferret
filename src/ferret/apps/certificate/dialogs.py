"""证书页的确认与编辑对话框。"""

from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    MessageBoxBase,
    PlainTextEdit,
    PushButton,
    SubtitleLabel,
)

from ferret.core.mitm import inspect_trusted_ca_files


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
        self.title_label.setText(self.tr("重新生成 CA 证书？"))

        self.desc_label = BodyLabel(self)
        self.desc_label.setText(
            self.tr("会删除现有的私钥与证书并生成一套全新的，操作无法撤销。")
        )
        self.desc_label.setWordWrap(True)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setText(
            self.tr(
                "生成后需要重新安装到系统信任库；其他已导入证书的设备也要重新导入。"
            )
        )
        self.hint_label.setWordWrap(True)

        self.yesButton.setText(self.tr("重新生成"))
        self.cancelButton.setText(self.tr("取消"))

    def __init_layout(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.hint_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(400)


class TrustedCaDialog(MessageBoxBase):
    """额外信任的上游 CA 证书编辑框（.plans/upstream-tls.md §5）。

    结构照 `DnsServersDialog`：一个 `PlainTextEdit`、每行一个路径、行内校验挡住
    「保存」。刻意**不**做「每行一个带减号按钮的列表控件」—— 路径是可粘贴、可批量
    改的文本，纯文本框比自造列表少一半代码，删一行也就是删一行。另配一个「添加
    文件…」按钮走 `QFileDialog`，省得用户手敲长路径。

    过滤串自己写 `*.pem *.crt *.cer`，**不复用** `CertExportFormat.file_filter`：
    那边是「写我们自己生成的文件」，这边是「读别人手上的文件」，后者常带
    `.crt` / `.cer` 后缀。
    """

    def __init__(self, current: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.__init_widget(current)
        self.__init_layout()

    def __init_widget(self, current: list[str]) -> None:
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("信任额外的 CA 证书"))

        self.desc_label = BodyLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr(
                "把测试环境的自签根证书加进 Ferret 的上游信任库。公共根证书会一并"
                "保留（Ferret 自动合并），正常站点不受影响。每行一个 .pem / .crt "
                "文件路径。"
            )
        )

        self.editor = PlainTextEdit(self)
        self.editor.setPlaceholderText("D:/test-ca/internal-root.pem")
        self.editor.setFixedHeight(140)
        if current:
            self.editor.setPlainText("\n".join(current))

        self.browse_btn = PushButton(self.tr("添加文件…"), self)

        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        # 警示色与 `DnsServersDialog.error_label` 同款（不做主题分叉，两主题可读）。
        self.error_label.setStyleSheet("color: #c07000;")
        self.error_label.setVisible(False)

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.editor.textChanged.connect(self._validate)
        self.browse_btn.clicked.connect(self._on_browse)
        self._validate()

    def __init_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.editor)
        layout.addWidget(self.browse_btn)
        layout.addWidget(self.error_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(460)

    def _on_browse(self) -> None:
        target, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 CA 证书文件"),
            str(Path.home()),
            self.tr("CA 证书 (*.pem *.crt *.cer);;所有文件 (*)"),
        )
        if not target:
            return
        existing = self.editor.toPlainText().rstrip("\n")
        self.editor.setPlainText(f"{existing}\n{target}" if existing else target)

    def _first_bad_file(self) -> tuple[int, str] | None:
        """返回第一个解不出证书的 (序号, 路径)，全部可用返回 None。

        逐个走 `inspect_trusted_ca_files` 而不是整批丢进去：整批只回「哪些坏」，
        这里要的是「第几个坏」—— 用户据这个序号回编辑器找行。文件都不大，
        每次 textChanged 全量重读实测无感（与 DNS 那边逐行校验同一姿态）。
        """
        for index, path in enumerate(self.get_files(), 1):
            if not inspect_trusted_ca_files([path]).good:
                return index, path
        return None

    def _validate(self) -> None:
        bad = self._first_bad_file()
        if bad is None:
            self.error_label.setVisible(False)
            self.yesButton.setEnabled(True)
        else:
            index, path = bad
            self.error_label.setText(
                self.tr("第 {} 个文件里没有证书：{}").format(index, path)
            )
            self.error_label.setVisible(True)
            self.yesButton.setEnabled(False)

    def get_files(self) -> list[str]:
        return [
            line.strip()
            for line in self.editor.toPlainText().splitlines()
            if line.strip()
        ]
