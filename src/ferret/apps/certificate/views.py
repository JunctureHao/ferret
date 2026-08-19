"""证书页：状态 / 详情 / 导出 / 维护四组卡片。

版式照抄 `apps/settings/views.py`——同一套 ScrollArea 骨架、悬浮标题、36px 边距、
SettingCard 家族的卡片，两页看着才像同一个软件里的两页。有三处细节非照抄不可：

- `enableTransparentBackground()` **必须在 `setWidget()` 之后**调。它内部是
  `if self.widget(): self.widget().setStyleSheet(...)`，提前调等于没调，
  深色主题下内层 QWidget 会留着浅色底。
- 卡片装在 `ExpandLayout` 里，而它只按 `w.height()` 摆位、从不改高度，
  所以高度随内容变的卡片得自己 `setFixedHeight`（两个 `_sync_height`）。
- 长文本标签一律 `_shrinkable`：QLabel 拿整段文字的宽度当最小宽度，横向滚动条
  又是关掉的，一个 SHA-256 指纹就能把整页顶到 1500px 宽、把右侧按钮挤出视口。

界面只做展示与派活，所有阻塞操作都交给 `CertificateController` 的线程池，
所以切到本页、点安装、重新生成都不会卡住 UI。
"""

from collections.abc import Sequence
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QLabel,
    QPushButton,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ExpandLayout,
    FluentIcon,
    IndeterminateProgressRing,
    PrimaryPushSettingCard,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    SimpleCardWidget,
    SmoothMode,
    TitleLabel,
    TransparentToolButton,
)

from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.dialogs import RegenerateCertDialog
from ferret.apps.certificate.models import CertificateState, info_rows
from ferret.apps.common.info_bar import show_error, show_success
from ferret.core.mitm import EXPORT_FORMATS, CertExportFormat, TrustState

STATE_ICONS: dict[TrustState, FluentIcon] = {
    TrustState.MISSING: FluentIcon.INFO,
    TrustState.ABSENT: FluentIcon.CANCEL_MEDIUM,
    TrustState.TRUSTED: FluentIcon.ACCEPT_MEDIUM,
    TrustState.STALE: FluentIcon.UPDATE,
    TrustState.UNAVAILABLE: FluentIcon.HELP,
}


def _shrinkable(label: QLabel) -> QLabel:
    """让标签可以被压窄到任意宽度。

    显式的 minimumWidth 会盖掉 minimumSizeHint（Qt 的 `qSmartMinSize`），
    这是长文本不把整页顶宽的唯一办法——指纹、路径这类值没有空格可断行。
    """
    label.setMinimumWidth(1)
    return label


def _unify_button_widths(buttons: Sequence[QPushButton]) -> None:
    """把一页里的动作按钮拉成同一个宽度。

    不拉平的话同页按钮宽度是乱的，两处叠加所致（均已实测）：
    QPushButton 的 sizeHint 随字数走（每个汉字 +14px，「卸载」54 / 「安装证书」82），
    而 `PrimaryPushSettingCard` 又把按钮 objectName 设成 `primaryButton`，
    命中 setting_card.qss 里另一套规则，同样文字比 `PushSettingCard` 的按钮窄 48px
    （「安装证书」82 : 130）——所以「安装证书」比字更少的「卸载」还窄。

    取最大 sizeHint 当下限而不是 setFixedWidth：按钮列的左右两边都能对齐，
    以后文字变长也只是整列一起变宽，不会截字。
    """
    width = max(button.sizeHint().width() for button in buttons)
    for button in buttons:
        button.setMinimumWidth(width)


def _resize_card(card: QWidget, height: int) -> None:
    """写入卡片新高度，并让所在的 SettingCardGroup 跟着收放。

    ExpandLayout 只按 `w.height()` 摆位、从不改高度；它的 eventFilter 又只在
    「高度变了、宽度没变」时才撑父控件，窗口横向缩放正好落在这个盲区里，
    所以这里显式补一次 adjustSize()。
    """
    if card.height() == height:
        return
    card.setFixedHeight(height)
    group = card.parent()
    if isinstance(group, SettingCardGroup):
        group.adjustSize()


class CertificateStatusCard(SettingCard):
    """安装状态卡：图标 + 状态标题 + 说明 + 重新检测按钮。

    骨架直接用 SettingCard，和设置页的卡片同宽同高同边距；只把说明文字放开换行，
    因为五种状态里最长的一句有四十多个字，一行放不下。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(FluentIcon.CERTIFICATE, "", " ", parent)
        self.contentLabel.setWordWrap(True)
        _shrinkable(self.titleLabel)
        _shrinkable(self.contentLabel)
        # 换行文字要真正用上整行宽度，得拆掉 SettingCard 的两处默认约定：
        # 1) 末尾那个 addStretch(1)（此刻正是最后一项）会和文字列平分富余宽度，
        #    因子清零后富余宽度全归文字列；
        # 2) contentLabel 是带 AlignLeft 加进去的，带水平对齐的项不会被拉开，
        #    只拿 sizeHint——而 wordWrap 的 sizeHint 是个又窄又高的启发值。
        self.hBoxLayout.setStretch(self.hBoxLayout.count() - 1, 0)
        self.hBoxLayout.setStretchFactor(self.vBoxLayout, 1)
        self.vBoxLayout.setAlignment(self.contentLabel, Qt.AlignmentFlag(0))

        self.busy_ring = IndeterminateProgressRing(self, start=False)
        self.busy_ring.setFixedSize(18, 18)
        self.busy_ring.setStrokeWidth(3)
        self.busy_ring.setVisible(False)

        self.refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self.refresh_btn.setToolTip(self.tr("重新检测"))

        self.hBoxLayout.addWidget(self.busy_ring, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(12)
        self.hBoxLayout.addWidget(self.refresh_btn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def set_status(self, icon: FluentIcon, title: str, detail: str) -> None:
        self.iconLabel.setIcon(icon)
        self.setTitle(title)
        self.setContent(detail)
        self._sync_height()

    def set_busy(self, busy: bool) -> None:
        self.busy_ring.setVisible(busy)
        self.busy_ring.start() if busy else self.busy_ring.stop()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self) -> None:
        # 说明文字换行时按「多出几行」加高：一行的常见情形仍是 70px，
        # 和 install/uninstall 卡片齐平，不会看出这张是特制的。
        self.hBoxLayout.activate()  # 先定下 contentLabel 的实际可用宽度
        line = self.contentLabel.fontMetrics().height()
        width = self.contentLabel.width()
        wrapped = self.contentLabel.heightForWidth(width) if width > 1 else line
        _resize_card(self, 70 + max(0, wrapped - line))


class CertificateDetailCard(SimpleCardWidget):
    """证书详情卡：字段名 / 值两列。

    用 SimpleCardWidget 而不是 SettingCard——两者的底色、描边、圆角画法一模一样
    （见各自的 `paintEvent`），但详情是十行动态内容，套不进 SettingCard
    「图标 + 标题 + 右侧控件」的固定骨架。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setBorderRadius(6)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(16, 16, 16, 16)
        self.grid.setHorizontalSpacing(24)
        self.grid.setVerticalSpacing(10)
        self.grid.setColumnStretch(1, 1)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                # 先摘出父子关系再排队删，否则旧标签会在下一轮事件循环前还留在卡片上。
                widget.setParent(None)
                widget.deleteLater()
        for row, (label, value) in enumerate(rows):
            name = CaptionLabel(label, self)
            content = BodyLabel(value, self)
            content.setWordWrap(True)
            # 指纹和序列号常要拿去跟系统里的证书对照，允许直接选中复制。
            content.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            _shrinkable(content)
            self.grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(content, row, 1)
            # 必须显式 show()：新建的子控件带着 WA_WState_Hidden，
            # 而 QLayoutItem.isEmpty() 对隐藏控件为真，heightForWidth 会算成 0 行。
            name.show()
            content.show()
        self._sync_height()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self) -> None:
        height = self.grid.heightForWidth(self.width())
        if height <= 0:  # 没有能换行的子控件时 heightForWidth 返回 -1
            height = self.grid.sizeHint().height()
        _resize_card(self, max(height, 1))


class CertificateInterface(ScrollArea):
    """证书页主体。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        controller: CertificateController,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self._busy = False

        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)
        self.certificate_label = TitleLabel(self.tr("证书"), self)

        self.status_group = SettingCardGroup(self.tr("安装状态"), self.scroll_widget)
        self.detail_group = SettingCardGroup(self.tr("证书详情"), self.scroll_widget)
        self.export_group = SettingCardGroup(self.tr("导出证书"), self.scroll_widget)
        self.maintain_group = SettingCardGroup(self.tr("维护"), self.scroll_widget)

        self.__init_cards()
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._on_state_changed(self.controller.state)

    # --- 构造 ---

    def __init_cards(self) -> None:
        self.status_card = CertificateStatusCard(self.status_group)
        self.install_card = PrimaryPushSettingCard(
            self.tr("安装证书"),
            FluentIcon.ADD_TO,
            self.tr("安装到系统信任库"),
            self.tr("写入当前用户的「受信任的根证书颁发机构」，无需管理员权限。"),
            self.status_group,
        )
        self.uninstall_card = PushSettingCard(
            self.tr("卸载"),
            FluentIcon.DELETE,
            self.tr("从系统信任库移除"),
            self.tr("连历次重新生成留下的同名旧证书一并清理。"),
            self.status_group,
        )

        self.detail_card = CertificateDetailCard(self.detail_group)

        self.export_cards: list[PushSettingCard] = [
            PushSettingCard(
                self.tr("导出"),
                FluentIcon.DOCUMENT,
                fmt.label,
                fmt.hint,
                self.export_group,
            )
            for fmt in EXPORT_FORMATS
        ]

        self.regenerate_card = PushSettingCard(
            self.tr("重新生成"),
            FluentIcon.UPDATE,
            self.tr("重新生成 CA 证书"),
            self.tr("生成新的私钥与证书，所有已导入旧证书的设备都要重新导入。"),
            self.maintain_group,
        )
        self.open_dir_card = PushSettingCard(
            self.tr("打开目录"),
            FluentIcon.FOLDER,
            self.tr("证书目录"),
            str(self.controller.certs_dir),
            self.maintain_group,
        )
        self.open_dir_card.setToolTip(str(self.controller.certs_dir))

        action_cards = (
            self.install_card,
            self.uninstall_card,
            self.regenerate_card,
            self.open_dir_card,
            *self.export_cards,
        )
        # 说明文字的 minimumSizeHint 会顺着布局一路顶宽，把右侧按钮挤出视口，
        # 所以每张卡片的说明都放开压缩——挤不下时宁可截字，也不能吃掉按钮。
        for card in action_cards:
            _shrinkable(card.contentLabel)
        # 按钮列上下对齐：四组卡片右侧的按钮共用一个宽度。
        _unify_button_widths([card.button for card in action_cards])

        # 老名字保留：外部（含用例）按控件说事，不必知道卡片是怎么拆的。
        self.install_btn = self.install_card.button
        self.uninstall_btn = self.uninstall_card.button
        self.regenerate_btn = self.regenerate_card.button
        self.open_dir_btn = self.open_dir_card.button
        self.refresh_btn = self.status_card.refresh_btn
        self.busy_ring = self.status_card.busy_ring
        self.status_title = self.status_card.titleLabel
        self.status_detail = self.status_card.contentLabel
        self.detail_grid = self.detail_card.grid

    def __init_widget(self) -> None:
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 顶部 80px 留给悬浮的标题：标题不进滚动内容，滚动时钉在原位。
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        # 必须在 setWidget 之后：它要拿 self.widget() 去刷内层背景。
        self.enableTransparentBackground()
        self.setSmoothMode(SmoothMode.NO_SMOOTH, Qt.Orientation.Vertical)

        self.setObjectName("CertificateInterface")
        self.scroll_widget.setObjectName("scrollWidget")
        self.certificate_label.setObjectName("settingLabel")
        self.certificate_label.move(36, 30)

    def __init_layout(self) -> None:
        self.status_group.addSettingCard(self.status_card)
        self.status_group.addSettingCard(self.install_card)
        self.status_group.addSettingCard(self.uninstall_card)
        self.detail_group.addSettingCard(self.detail_card)
        for card in self.export_cards:
            self.export_group.addSettingCard(card)
        self.maintain_group.addSettingCard(self.regenerate_card)
        self.maintain_group.addSettingCard(self.open_dir_card)

        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 10, 36, 0)
        self.expand_layout.addWidget(self.status_group)
        self.expand_layout.addWidget(self.detail_group)
        self.expand_layout.addWidget(self.export_group)
        self.expand_layout.addWidget(self.maintain_group)

    def __connect_signal_to_slot(self) -> None:
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.busy_changed.connect(self._on_busy_changed)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        self.refresh_btn.clicked.connect(self.controller.refresh)
        self.install_card.clicked.connect(self.controller.install)
        self.uninstall_card.clicked.connect(self.controller.uninstall)
        self.regenerate_card.clicked.connect(self._on_regenerate)
        self.open_dir_card.clicked.connect(self._on_open_dir)
        for fmt, card in zip(EXPORT_FORMATS, self.export_cards, strict=True):
            card.clicked.connect(partial(self._on_export, fmt))

    # --- 生命周期 ---

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 信任库随时可能被外部改动（certmgr.msc、其他抓包工具），每次进页面都重测。
        self.controller.refresh()

    # --- 状态同步 ---

    @Slot(object)
    def _on_state_changed(self, state: CertificateState) -> None:
        self.status_card.set_status(STATE_ICONS[state.trust], state.title, state.detail)
        self.install_btn.setText(
            self.tr("重新安装") if state.needs_reinstall else self.tr("安装证书")
        )
        self.detail_group.setVisible(state.info is not None)
        if state.info is not None:
            self.detail_card.set_rows(info_rows(state.info))
        self._update_actions()

    @Slot(bool)
    def _on_busy_changed(self, busy: bool) -> None:
        self._busy = busy
        self.status_card.set_busy(busy)
        self._update_actions()

    def _update_actions(self) -> None:
        """按状态 + 忙闲整卡启停：连标题一起变灰，比只灰按钮更容易看出来。"""
        state = self.controller.state
        self.install_card.setEnabled(not self._busy and state.can_install)
        self.uninstall_card.setEnabled(not self._busy and state.can_uninstall)
        self.regenerate_card.setEnabled(not self._busy)
        self.refresh_btn.setEnabled(not self._busy)
        for card in self.export_cards:
            card.setEnabled(not self._busy)

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str) -> None:
        show_error(title, detail, self)

    @Slot(str)
    def _on_operation_succeeded(self, message: str) -> None:
        show_success(self.tr("证书"), message, self)

    # --- 用户操作 ---

    @Slot()
    def _on_regenerate(self) -> None:
        if RegenerateCertDialog(self.window()).exec():
            self.controller.regenerate()

    @Slot()
    def _on_open_dir(self) -> None:
        directory = self.controller.certs_dir
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def _on_export(self, fmt: CertExportFormat) -> None:
        target, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("导出证书"),
            str(Path.home() / fmt.filename),
            fmt.file_filter,
        )
        if target:
            self.controller.export(fmt.key, target)
