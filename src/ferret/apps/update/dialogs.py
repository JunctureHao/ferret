"""版本与更新弹窗：展示协调器保存的状态，关闭弹窗不影响后台任务。

按钮只发出用户意图；协调器负责检查、下载、应用更新与对象生命周期。
``validate`` 拦住 MessageBoxBase 的自动 accept，任务状态更新不会关闭弹窗。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    HyperlinkButton,
    MessageBoxBase,
    ProgressBar,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchSettingCard,
)

from ferret.core.meta import REPO_URL
from ferret.core.mitm import human
from ferret.core.settings import APP_NAME, CONFIG
from ferret.core.update import UpdateBrief


class UpdateDialog(MessageBoxBase):
    check_requested = Signal()
    download_requested = Signal()
    restart_requested = Signal()

    def __init__(
        self,
        *,
        current_version: str,
        supported: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._supported = supported
        self._action = "check"
        self._ready = False
        self.__init_widget(current_version)
        self.__init_layout()
        self.render_state(
            status="idle" if supported else "unsupported",
            brief=None,
            last_checked=None,
            error="",
            progress=0,
            busy=False,
            ready=False,
        )

    def __init_widget(self, current_version: str) -> None:
        self.title_label = SubtitleLabel(self.tr("版本与更新"), self)
        self.version_label = StrongBodyLabel(self)
        self.version_label.setText(
            self.tr("{} · 当前版本 v{}").format(APP_NAME, current_version)
        )
        installation = (
            self.tr("安装版") if self._supported else self.tr("便携版 / 开发环境")
        )
        self.version_label.setToolTip(installation)
        self.version_label.setAccessibleDescription(installation)

        self.status_card = SimpleCardWidget(self)
        self.status_card.setBorderRadius(8)
        self.status_label = StrongBodyLabel(self)
        self.status_label.setWordWrap(True)
        self.last_checked_label = CaptionLabel(self)
        self.desc_label = BodyLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notes_label = BodyLabel(self)
        self.notes_label.setWordWrap(True)
        self.notes_label.setTextFormat(Qt.TextFormat.PlainText)

        self.release_link = HyperlinkButton(self)
        self.release_link.setText(self.tr("查看完整发布说明"))
        self.check_button = PushButton(self.tr("检查更新"), self)
        self.check_button.clicked.connect(self.check_requested)

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.error_label = BodyLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setStyleSheet("color: #c07000;")

        auto_update_hint = self.tr("启动后及每小时检测，发现新版时在菜单上提示")
        self.auto_update_card = SwitchSettingCard(
            FluentIcon.SYNC,
            self.tr("自动检查更新"),
            configItem=CONFIG.auto_check_update,
            parent=self,
        )
        self.auto_update_card.setEnabled(self._supported)
        self.auto_update_card.setToolTip(auto_update_hint)
        self.auto_update_card.setAccessibleDescription(auto_update_hint)
        self.cancelButton.setText(self.tr("关闭"))

    def __init_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(14)
        layout.addWidget(self.title_label)
        layout.addWidget(self.version_label)

        card_layout = QVBoxLayout(self.status_card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(10)
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.addWidget(self.status_label, 1)
        status_row.addWidget(self.last_checked_label, 0, Qt.AlignmentFlag.AlignRight)
        card_layout.addLayout(status_row)
        card_layout.addWidget(self.desc_label)
        card_layout.addWidget(self.notes_label)
        links = QHBoxLayout()
        links.setContentsMargins(0, 0, 0, 0)
        links.addWidget(self.release_link)
        links.addStretch(1)
        links.addWidget(self.check_button)
        card_layout.addLayout(links)
        card_layout.addWidget(self.progress_bar)
        card_layout.addWidget(self.error_label)
        layout.addWidget(self.status_card)
        layout.addWidget(self.auto_update_card)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(520)

    @property
    def ready(self) -> bool:
        return self._ready

    def validate(self) -> bool:
        if self.yesButton.isEnabled():
            if self._action == "restart":
                self.restart_requested.emit()
            elif self._action == "download":
                self.download_requested.emit()
            else:
                self.check_requested.emit()
        return False

    def render_state(
        self,
        *,
        status: str,
        brief: UpdateBrief | None,
        last_checked: datetime | None,
        error: str,
        progress: int,
        busy: bool,
        ready: bool,
    ) -> None:
        """一次刷新完整状态；弹窗在任务中途重建时也能恢复进度与操作。"""
        self._ready = ready
        self.status_label.setText(self._status_text(status))
        if last_checked is None:
            self.last_checked_label.clear()
            self.last_checked_label.hide()
        else:
            self.last_checked_label.setText(
                self.tr("最近检查：{}").format(
                    last_checked.astimezone().strftime("%Y-%m-%d %H:%M:%S")
                )
            )
            self.last_checked_label.show()

        self.desc_label.setVisible(brief is not None)
        if brief is not None:
            self.desc_label.setText(
                self.tr("新版本 v{}，预计下载 {}。").format(
                    brief.target, human.pretty_size(brief.size)
                )
            )
        notes = brief.notes_markdown.strip() if brief is not None else ""
        # 这里只放短摘要，长发布说明交给发布页，避免弹窗和常驻状态无界增长。
        excerpt = "\n".join(notes.splitlines()[:8])[:600]
        if len(excerpt) < len(notes):
            excerpt = excerpt.rstrip() + "…"
        self.notes_label.setText(excerpt)
        self.notes_label.setVisible(bool(excerpt))
        self.release_link.setUrl(
            QUrl(brief.release_page if brief is not None else f"{REPO_URL}/releases")
        )
        self.check_button.setVisible(
            self._supported and brief is not None and not ready
        )
        self.check_button.setEnabled(not busy)

        self.progress_bar.setValue(max(0, min(100, int(progress))))
        self.progress_bar.setVisible(status == "downloading" or ready)
        self.error_label.setText(error)
        self.error_label.setVisible(bool(error))
        self.yesButton.setEnabled(not busy)
        # Velopack 没有取消下载的接口；关闭只释放 UI，不取消后台任务。
        self.cancelButton.setEnabled(True)
        if ready:
            self._action = "restart"
            self.yesButton.setText(self.tr("重启更新"))
        elif brief is not None:
            self._action = "download"
            self.yesButton.setText(
                self.tr("重试下载")
                if status == "download_failed"
                else self.tr("下载更新")
            )
        else:
            self._action = "check"
            self.yesButton.setText(
                self.tr("检查更新") if self._supported else self.tr("打开发布页")
            )

    def _status_text(self, status: str) -> str:
        if status == "unsupported":
            return self.tr("当前运行方式不支持应用内更新，请前往发布页下载。")
        if status == "checking":
            return self.tr("正在检查更新…")
        if status == "latest":
            return self.tr("当前已是最新版本")
        if status == "available":
            return self.tr("发现新版本")
        if status == "downloading":
            return self.tr("正在下载更新，关闭此窗口后仍会继续。")
        if status == "ready":
            return self.tr("下载完成，重启后生效。")
        if status == "check_failed":
            return self.tr("检查更新失败")
        if status == "download_failed":
            return self.tr("下载更新失败，可以重试。")
        if status == "apply_failed":
            return self.tr("应用更新失败，可以重试。")
        return self.tr("尚未检查更新")
