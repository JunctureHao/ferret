"""设置页的更新对话框：「发现新版本 → 下载 → 重启」三态（docs/design.md#update）。

对话框只做展示与用户意图回收，任务编排全在 ``UpdateController``：状态迁移由外部
调用 ``set_downloading`` / ``set_ready`` / ``set_failed`` 驱动。

「立即更新」为什么不连 clicked：MessageBoxBase 的 yesButton 默认走
``validate() → accept()``，点一下对话框就关了，下载就没法在框里进行。所以拦在
``validate()`` —— 首次点「立即更新」发 ``download_requested`` 并拒绝关闭；
进入就绪态后按钮变「重启应用」，validate 放行、对话框以 accepted 收场，由 view
据此发起 apply。
"""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    HyperlinkButton,
    MessageBoxBase,
    ProgressBar,
    SubtitleLabel,
)

from ferret.core.mitm import human
from ferret.core.update import UpdateBrief


class UpdateDialog(MessageBoxBase):
    download_requested = Signal()

    def __init__(self, brief: UpdateBrief, parent: QWidget | None = None):
        super().__init__(parent)
        self._brief = brief
        self._download_started = False
        self._ready = False
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self) -> None:
        brief = self._brief
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("发现新版本 v{}").format(brief.target))

        self.desc_label = BodyLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr("当前版本 v{}，新版本包大小 {}。").format(
                brief.current, human.pretty_size(brief.size)
            )
        )

        self.release_link = HyperlinkButton(self)
        self.release_link.setText(self.tr("查看更新内容"))
        self.release_link.setUrl(QUrl(brief.release_page))

        self.progress_bar = ProgressBar(self)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setVisible(False)

        self.error_label = CaptionLabel(self)
        self.error_label.setWordWrap(True)
        # 警示色与 DNS 对话框同款（不做主题分叉，两主题下可读）。
        self.error_label.setStyleSheet("color: #c07000;")
        self.error_label.setVisible(False)

        self.yesButton.setText(self.tr("立即更新"))
        self.cancelButton.setText(self.tr("稍后"))

    def __init_layout(self) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        layout.addWidget(self.release_link)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.error_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(420)

    @property
    def ready(self) -> bool:
        """下载是否已完成（view 据此决定 accept 后要不要发起 apply）。"""
        return self._ready

    def validate(self) -> bool:
        """yes 按钮的关闭闸门（机理见模块 docstring）。"""
        if self._ready:
            return True
        if not self._download_started:
            self._download_started = True
            self.download_requested.emit()
        return False

    def set_downloading(self) -> None:
        """进入下载态：进度条亮相，两个按钮都锁死（velopack 没有取消语义）。"""
        self.error_label.setVisible(False)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.yesButton.setEnabled(False)
        self.cancelButton.setEnabled(False)

    def set_progress(self, percent: int) -> None:
        self.progress_bar.setValue(max(0, min(100, int(percent))))

    def set_ready(self) -> None:
        """下载完成：按钮变「重启应用」，点击即应用更新并退出进程。"""
        self._ready = True
        self.progress_bar.setValue(100)
        self.desc_label.setText(self.tr("下载完成，重启后生效。"))
        self.yesButton.setText(self.tr("重启应用"))
        self.yesButton.setEnabled(True)
        self.cancelButton.setEnabled(True)

    def set_failed(self, message: str) -> None:
        """下载/应用失败：展示原因，放人走（择日再手动检查）。"""
        self.error_label.setText(message)
        self.error_label.setVisible(True)
        self.yesButton.setEnabled(False)
        self.cancelButton.setEnabled(True)
