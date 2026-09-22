"""证书页的确认与编辑对话框。"""

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    MessageBoxBase,
    PlainTextEdit,
    PushButton,
    RadioButton,
    SubtitleLabel,
)

from ferret.core.mitm import (
    CLIENT_CERTS_SCAN_LIMIT,
    ClientCertEntry,
    client_certs_error,
    client_certs_suggest_dir,
    inspect_client_certs,
    inspect_trusted_ca_files,
)

# 盘点防抖：`TrustedCaDialog` 每次 textChanged 全量重读，是因为那边的文件由用户
# 逐个列出、通常 1~3 个；这里一敲就可能扫一整个目录（上限
# `CLIENT_CERTS_SCAN_LIMIT` 张，每张都要解 X.509），不防抖会边打字边卡。
_SCAN_DEBOUNCE_MS = 250

# 与 `TrustedCaDialog.error_label` 同款警示色（不做主题分叉，两主题都读得清）。
_WARN_COLOR = "color: #c07000;"


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


class ClientCertsDialog(MessageBoxBase):
    """mTLS 客户端证书编辑框（.plans/mtls-client-certs.md §5.3）。

    页面上半部分讲「让别人信任 Ferret」、上游信任组讲「让 Ferret 信任别人」，
    这一个讲的是第三件事：**让上游相信 Ferret 是谁**。原生 `client_certs` 只有
    一个路径参数，形态由它指向文件还是目录决定 —— 所以这里的「出示方式」单选是
    **对话框内的状态**，不是从配置派生出来的量：进场按磁盘现状播种一次（目录 /
    单文件 / 路径已失效则退回目录），之后只跟用户走。

    过滤串自己写 `*.pem *.crt *.cer`，**不复用** `CertExportFormat.file_filter`
    （理由同 `TrustedCaDialog`：读别人的文件 vs 写我们生成的文件）。
    """

    def __init__(self, current: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(_SCAN_DEBOUNCE_MS)
        self.__init_widget(current)
        self.__init_layout()
        self._rescan()

    def __init_widget(self, current: str) -> None:
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("向服务器出示的客户端证书"))

        self.desc_label = BodyLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr(
                "上游服务器要求双向认证（mTLS）时，Ferret 用这里的证书向它证明身份。"
                "证书与私钥要拼在同一个 .pem 文件里；Ferret 只引用文件，不拷贝、不修改。"
            )
        )

        self.dir_radio = RadioButton(self.tr("按主机目录"), self)
        self.file_radio = RadioButton(self.tr("单文件"), self)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.dir_radio)
        self.mode_group.addButton(self.file_radio)
        # 形态播种：路径已失效（删了 / 挪了）也落到目录模式 —— 目录是推荐姿态，
        # 而「单文件 = 对所有上游出示同一张」是要用户主动选的风险动作。
        seed = Path(current.strip()).expanduser() if current.strip() else None
        self.file_radio.setChecked(bool(seed and seed.is_file()))
        self.dir_radio.setChecked(not self.file_radio.isChecked())

        self.path_edit = LineEdit(self)
        self.path_edit.setText(current.strip())
        self.path_edit.setClearButtonEnabled(True)

        self.browse_btn = PushButton(self.tr("浏览…"), self)
        self.suggest_btn = PushButton(self.tr("用推荐目录"), self)
        self.open_btn = PushButton(self.tr("打开目录"), self)

        self.preview = PlainTextEdit(self)
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(130)

        self.hint_label = CaptionLabel(self)
        self.hint_label.setWordWrap(True)

        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(_WARN_COLOR)
        self.error_label.setVisible(False)

        # 「重新加载」插进按钮行：证书续期时用户在原路径上换掉文件内容，路径一字
        # 未改，界面不重扫就看不出新有效期。按它只重扫盘点，真正让新证书生效的是
        # 「保存」（它会清掉 mitmproxy 缓存的 TLS 上下文，见 runtime 侧注释）。
        self.reload_btn = PushButton(self.tr("重新加载"), self.buttonGroup)
        self.buttonLayout.insertWidget(
            0, self.reload_btn, 1, Qt.AlignmentFlag.AlignVCenter
        )

        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))

        self.path_edit.textChanged.connect(self._timer.start)
        self._timer.timeout.connect(self._rescan)
        self.dir_radio.toggled.connect(self._rescan)
        self.browse_btn.clicked.connect(self._on_browse)
        self.suggest_btn.clicked.connect(self._on_suggest)
        self.open_btn.clicked.connect(self._on_open_dir)
        self.reload_btn.clicked.connect(self._rescan)

    def __init_layout(self) -> None:
        mode_row = QHBoxLayout()
        mode_row.setSpacing(16)
        mode_row.addWidget(self.dir_radio)
        mode_row.addWidget(self.file_radio)
        mode_row.addStretch(1)

        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_btn)
        path_row.addWidget(self.suggest_btn)
        path_row.addWidget(self.open_btn)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addLayout(mode_row)
        layout.addLayout(path_row)
        layout.addWidget(self.preview)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.error_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(560)

    # --- 状态 ---

    def get_path(self) -> str:
        """用户填的原样路径（只去首尾空白）。空串 = 清除。

        不替原生展开 `~`：`addons/core.py` 与 `tlsconfig.py` 两处自己展开，
        我们展开了反而让 CONFIG 与内核 options 存着两个不同的字符串。
        """
        return self.path_edit.text().strip()

    def _is_dir_mode(self) -> bool:
        return self.dir_radio.isChecked()

    # --- 盘点与校验 ---

    def _rescan(self) -> None:
        self._timer.stop()
        path = self.get_path()
        dir_mode = self._is_dir_mode()
        self.suggest_btn.setVisible(dir_mode)
        target = Path(path).expanduser() if path else None
        self.open_btn.setEnabled(bool(dir_mode and target and target.is_dir()))
        self.hint_label.setText(
            self.tr(
                "按 SNI 精确匹配目录下的 <主机名>.pem：主机名必须与地址栏完全一致，"
                "子域名各放一份，匹配不到的主机不出示任何证书。"
            )
            if dir_mode
            else self.tr(
                "这张证书会出示给每一个要求客户端证书的上游服务器，请在调试结束后清除。"
            )
        )
        self.preview.setPlainText(self._preview_text(path, dir_mode))
        self._validate(path, dir_mode)

    def _preview_text(self, path: str, dir_mode: bool) -> str:
        if not path:
            return self.tr("未设置：上游要求客户端证书时，握手会失败。")
        summary = inspect_client_certs(path)
        if not summary.exists:
            return self.tr("路径不存在：{}").format(path)
        if summary.is_dir != dir_mode:
            return (
                self.tr("这是一个文件，请选「单文件」。")
                if dir_mode
                else self.tr("这是一个目录，请选「按主机目录」。")
            )
        if not summary.entries:
            return self.tr("目录里没有 .pem 文件，不会出示任何证书。")
        lines = [self._entry_line(item) for item in summary.entries]
        if summary.truncated:
            lines.append(
                self.tr("（目录里的文件太多，只盘点了前 {} 个）").format(
                    CLIENT_CERTS_SCAN_LIMIT
                )
            )
        return "\n".join(lines)

    def _entry_line(self, entry: ClientCertEntry) -> str:
        if entry.error:
            return self.tr("✗ {} — {}").format(entry.name, entry.error)
        notafter = entry.notafter.strftime("%Y-%m-%d") if entry.notafter else "?"
        if entry.expired:
            return self.tr("⚠ {} — CN={}，已于 {} 过期").format(
                entry.name, entry.cn, notafter
            )
        return self.tr("✓ {} — CN={}，有效期至 {}").format(
            entry.name, entry.cn, notafter
        )

    def _validate(self, path: str, dir_mode: bool) -> None:
        """只有**会让保存失败**的情形才置灰，盘点里的坏文件不拦（判据见闸门）。"""
        reason = ""
        if path:
            target = Path(path).expanduser()
            if target.is_dir() and not dir_mode:
                reason = self.tr("选了「单文件」，但这个路径是一个目录。")
            elif target.is_file() and dir_mode:
                reason = self.tr("选了「按主机目录」，但这个路径是一个文件。")
            else:
                reason = client_certs_error(path)
        self.error_label.setText(reason)
        self.error_label.setVisible(bool(reason))
        self.yesButton.setEnabled(not reason)

    # --- 按钮 ---

    def _on_browse(self) -> None:
        start = self.get_path() or str(client_certs_suggest_dir())
        if self._is_dir_mode():
            target = QFileDialog.getExistingDirectory(
                self, self.tr("选择客户端证书目录"), start
            )
        else:
            target, _ = QFileDialog.getOpenFileName(
                self,
                self.tr("选择客户端证书文件"),
                start,
                self.tr("客户端证书 (*.pem *.crt *.cer);;所有文件 (*)"),
            )
        if target:
            self.path_edit.setText(target)
            self._rescan()

    def _on_suggest(self) -> None:
        """建出推荐目录并填进输入框 —— 「该往哪放」是这功能最大的摩擦点。

        建不出来（目录只读之类）就只填路径，让后面的闸门去说「路径不存在」。
        """
        target = client_certs_suggest_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.path_edit.setText(str(target))
        self._rescan()

    def _on_open_dir(self) -> None:
        target = Path(self.get_path()).expanduser()
        if target.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
