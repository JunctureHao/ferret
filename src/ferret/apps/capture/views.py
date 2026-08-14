from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QSize, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    IconInfoBadge,
    IconWidget,
    InfoBadge,
    InfoBadgePosition,
    InfoLevel,
    MessageBoxBase,
    PushButton,
    SimpleCardWidget,
    SpinBox,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ferret.apps.capture.controllers import CaptureController, CertBadgeController
from ferret.apps.common.filter import MultiFilterManager
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.common.icon import BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.core.mitm import Cert

if TYPE_CHECKING:
    from ferret.apps.window import MainWindow


class CapturesInterface(QWidget):
    """抓包主界面 - 包含工具栏、搜索面板和内容区域"""

    def __init__(self, parent: "MainWindow | None" = None):
        super().__init__(parent)
        self.setObjectName("CapturesInterface")
        self.controller = CaptureController(self)

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.toolbar = CapturesToolBar(self, self.controller)
        self.content = CapturesContentArea(self, self.controller)

    def __init_layout(self):
        """初始化布局结构"""
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.addWidget(self.toolbar)
        self.main_layout.addWidget(self.content)

    def __connect_signal_to_slot(self):
        """协调层：连接组件业务信号到 Controller"""
        # 工具栏业务信号 → Controller
        self.toolbar.captureToggled.connect(self.__on_capture_toggled)

        # 简单点击：直接连接按钮原生信号
        self.toolbar.proxy_setting_btn.clicked.connect(self.__show_proxy_port_dialog)
        self.toolbar.locate_selection_btn.clicked.connect(
            self.content.table.on_locate_selection
        )
        self.toolbar.captures_delete_btn.clicked.connect(self.content.table.clear_all)

        # Controller 状态信号 → UI 更新
        self.controller.captureStateChanged.connect(self.__on_capture_state_changed)
        self.controller.master_ready.connect(self.content.table.set_view)
        self.controller.flow_added.connect(self.content.table.on_flow_added)
        self.controller.flow_updated.connect(self.content.table.on_flow_updated)
        self.controller.flow_removed.connect(self.content.table.on_flow_removed)
        self.controller.view_refreshed.connect(self.content.table.on_view_refreshed)

        # 搜索面板（通过 toolbar 暴露的信号）
        self.toolbar.conditionsChanged.connect(self.__on_search_changed)

        # 统计信息更新
        self.content.table.stats_updated.connect(self.toolbar.update_stats)

    @Slot(bool)
    def __on_capture_toggled(self, is_on: bool):
        """协调：toolbar 信号 → controller 操作"""
        self.toolbar.control_btn.setEnabled(False)
        try:
            self.controller.toggle_capture()
        finally:
            self.toolbar.control_btn.setEnabled(True)

    @Slot(bool)
    def __on_capture_state_changed(self, is_capturing: bool):
        """协调：controller 状态信号 → UI 更新"""
        if is_capturing:
            self.toolbar.control_btn.setIcon(FluentIcon.PAUSE)
            show_success("成功", "代理已启动", parent=self)
        else:
            self.toolbar.control_btn.setIcon(FluentIcon.PLAY)
            show_success("成功", "代理已关闭", parent=self)

    @Slot()
    def __on_search_changed(self):
        """搜索条件变更时更新过滤。

        把 GUI 条件交给 Controller，由 View.set_filter(flowfilter 表达式) 统一做
        「显示过滤」——_store 保留全部流量，仅 _view 可见列表变化，无清除效果。
        """
        conditions = self.toolbar.search_panel.get_conditions()
        self.controller.apply_filter(conditions)

    @Slot()
    def __show_proxy_port_dialog(self):
        """弹出端口设置对话框"""
        w = ProxyPortDialog(self.controller.current_port, self.window())
        if w.exec():
            new_port = w.get_port()
            if new_port and new_port != self.controller.current_port:
                self.controller.update_port(new_port)

    def stop_capture(self):
        """停止抓包（供外部调用，如MainWindow.closeEvent）"""
        self.controller.stop_capture()

    def showEvent(self, event):
        """每次打开（切到）抓包页面时，检测证书安装状态以刷新角标。"""
        super().showEvent(event)
        self.toolbar.cert_controller.refresh()


class CapturesContentArea(FlowViewerPane):
    """Capture-specific name for the shared Flow viewer."""

    def __init__(
        self,
        parent: CapturesInterface,
        controller: CaptureController,
    ) -> None:
        super().__init__(parent=parent, controller=controller)


class CapturesToolBar(QWidget):
    """自定义工具栏 - 内嵌搜索面板，自管理显隐，对外暴露业务信号"""

    # 业务信号
    captureToggled = Signal(bool)
    conditionsChanged = Signal()  # 搜索面板条件变更（透传）

    def __init__(
        self,
        parent: "CapturesInterface",
        controller: CaptureController,
    ):
        super().__init__(parent)
        self.capture_controller = controller
        self.cert_controller = CertBadgeController(self)

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        # 垂直方向只占所需空间，不被 VBoxLayout 拉伸
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        # 左侧：过滤按钮
        self.search_btn = TransparentToolButton(FluentIcon.FILTER, self)
        self.search_btn.setCheckable(True)
        self.search_btn.setToolTip(self.tr("高级搜索") + " (Ctrl+F)")
        self.search_btn.installEventFilter(
            ToolTipFilter(self.search_btn, 1000, ToolTipPosition.TOP)
        )
        self.search_btn.setFixedSize(32, 32)
        self.search_btn.setIconSize(QSize(20, 20))
        self.stats_badge = InfoBadge.attension(
            0, self, self.search_btn, InfoBadgePosition.TOP_RIGHT
        )
        self.stats_badge.raise_()

        # 右侧：操作按钮
        self.cert_btn = TransparentToolButton(FluentIcon.CERTIFICATE, self)
        self.cert_btn.setToolTip(self.tr("证书设置"))
        self.cert_btn.installEventFilter(
            ToolTipFilter(self.cert_btn, 1000, ToolTipPosition.TOP)
        )
        self.cert_btn.setFixedSize(32, 32)
        self.cert_btn.setIconSize(QSize(20, 20))
        self.cert_bage = IconInfoBadge.error(
            FluentIcon.CANCEL_MEDIUM,
            self,
            self.cert_btn,
            InfoBadgePosition.TOP_RIGHT,
        )
        self.cert_bage.raise_()

        # 证书状态控制器（仅发信号，UI 更新在本类 _on_cert_status 处理）

        self.proxy_setting_btn = TransparentToolButton(FluentIcon.GLOBE, self)
        self.proxy_setting_btn.setToolTip(self.tr("端口设置"))
        self.proxy_setting_btn.installEventFilter(
            ToolTipFilter(self.proxy_setting_btn, 1000, ToolTipPosition.TOP)
        )
        self.proxy_setting_btn.setFixedSize(32, 32)
        self.proxy_setting_btn.setIconSize(QSize(20, 20))

        self.locate_selection_btn = TransparentToolButton(
            BaseIcon.LOCATION_TARGET, self
        )
        self.locate_selection_btn.setToolTip(self.tr("定位选中"))
        self.locate_selection_btn.installEventFilter(
            ToolTipFilter(self.locate_selection_btn, 1000, ToolTipPosition.TOP)
        )
        self.locate_selection_btn.setFixedSize(32, 32)
        self.locate_selection_btn.setIconSize(QSize(20, 20))

        self.control_btn = TransparentToolButton(FluentIcon.PLAY, self)
        self.control_btn.setCheckable(True)
        self.control_btn.setToolTip(self.tr("系统代理"))
        self.control_btn.installEventFilter(
            ToolTipFilter(self.control_btn, 1000, ToolTipPosition.TOP)
        )
        self.control_btn.setFixedSize(32, 32)
        self.control_btn.setIconSize(QSize(20, 20))

        self.captures_delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.captures_delete_btn.setToolTip(self.tr("清空数据"))
        self.captures_delete_btn.installEventFilter(
            ToolTipFilter(self.captures_delete_btn, 1000, ToolTipPosition.TOP)
        )
        self.captures_delete_btn.setFixedSize(32, 32)
        self.captures_delete_btn.setIconSize(QSize(20, 20))

        # 搜索面板（内嵌）
        self.search_panel = MultiFilterManager(self)
        self.search_panel.setContentsMargins(0, 0, 0, 4)

        # Ctrl+F 快捷键（应用级事件过滤）
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def __init_layout(self):
        """初始化布局结构 - 按钮行 + 搜索面板（垂直）"""
        v_layout = QVBoxLayout(self)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(0)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(12, 8, 12, 4)
        btn_layout.setSpacing(4)
        btn_layout.addWidget(self.search_btn)
        btn_layout.addStretch(1)

        btn_layout.addWidget(self.cert_btn)
        btn_layout.addWidget(self.proxy_setting_btn)
        btn_layout.addWidget(self.locate_selection_btn)
        btn_layout.addWidget(self.control_btn)
        btn_layout.addWidget(self.captures_delete_btn)

        v_layout.addLayout(btn_layout)
        v_layout.addWidget(self.search_panel)

    def __connect_signal_to_slot(self):
        """组件内部事件管理"""
        self.control_btn.toggled.connect(self.captureToggled.emit)
        self.search_btn.toggled.connect(self.__toggle_search_panel)
        self.search_panel.conditionsChanged.connect(self.conditionsChanged.emit)
        self.search_panel.panelCloseRequested.connect(self.__on_search_panel_close)
        self.cert_btn.clicked.connect(self._on_cert_btn_click)
        self.cert_controller.status_changed.connect(self._on_cert_status)

    @Slot()
    def _on_cert_btn_click(self):
        """点击证书按钮：打开证书设置对话框，并同步角标状态。"""
        dlg = CertSettingsDialog(self.window())
        dlg.status_changed.connect(self._on_cert_status)
        dlg.exec()

    @Slot()
    def __toggle_search_panel(self):
        """切换搜索面板显示/隐藏"""
        visible = not self.search_panel.isVisible()
        self.search_panel.setVisible(visible)
        self.search_btn.blockSignals(True)
        self.search_btn.setChecked(visible)
        self.search_btn.blockSignals(False)
        if visible:
            self.search_panel.focus_first_input()

    @Slot()
    def __on_search_panel_close(self):
        """搜索面板请求关闭（最后一行被删除）"""
        self.search_panel.setHidden(True)
        self.search_btn.blockSignals(True)
        self.search_btn.setChecked(False)
        self.search_btn.blockSignals(False)

    @Slot(bool)
    def _on_cert_status(self, installed: bool):
        """根据证书检测结果更新角标：已安装=✔成功态，未安装=✗错误态。"""
        if installed:
            self.cert_bage.setLevel(InfoLevel.SUCCESS)
            self.cert_bage.setIcon(FluentIcon.ACCEPT_MEDIUM)
        else:
            self.cert_bage.setLevel(InfoLevel.ERROR)
            self.cert_bage.setIcon(FluentIcon.CANCEL_MEDIUM)

    def eventFilter(self, obj, event):
        """应用级事件过滤：拦截 Ctrl+F 快捷键"""
        if (
            event.type() == QEvent.Type.KeyPress
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
            and event.key() == Qt.Key.Key_F
            and self.window().isActiveWindow()
        ):
            self.__toggle_search_panel()
            return True
        return super().eventFilter(obj, event)

    @Slot(int, int, int)
    def update_stats(self, total: int, shown: int, selected: int):
        """更新统计角标"""
        if shown == total:
            self.stats_badge.setText(str(total))
        else:
            self.stats_badge.setText(f"{shown}/{total}")
        self.stats_badge.adjustSize()


class CertSettingsDialog(MessageBoxBase):
    """证书设置对话框

    内容随当前安装状态变化：
    - 已安装：“已安装”说明，无安装按钮
    - 未安装：“未安装”说明 + 安装按钮，点击后自动安装并刷新
    """

    status_changed = Signal(bool)  # 状态变化（装完后广播），供 toolbar 角标同步

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._cert = Cert()
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._refresh()

    def __init_widget(self):
        # 标题
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("证书设置"))

        # 状态卡片（图标 + 主文案）
        self.status_card = SimpleCardWidget(self)
        self.status_card.setFixedHeight(72)

        self.icon_widget = IconWidget(self.status_card)
        self.icon_widget.setFixedSize(36, 36)

        self.status_label = BodyLabel(self.status_card)
        self.status_label.setWordWrap(True)

        # 提示文字（次要信息）
        self.tip_label = BodyLabel(self)
        self.tip_label.setStyleSheet(
            "color: rgba(255, 255, 255, 0.6); font-size: 12px;"
        )
        self.tip_label.setText(
            self.tr("mitmproxy 需要信任其 CA 证书才能解密 HTTPS 流量。")
        )
        self.tip_label.setWordWrap(True)

        # 安装按钮
        self.install_btn = PushButton(self.tr("安装证书"), self)
        self.install_btn.setFixedHeight(34)
        self.install_btn.setMinimumWidth(140)

    def __init_layout(self):
        # 卡片内部布局：图标 + 文字
        card_layout = QHBoxLayout(self.status_card)
        card_layout.setContentsMargins(16, 10, 16, 10)
        card_layout.setSpacing(14)
        card_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        card_layout.addWidget(self.icon_widget)
        card_layout.addWidget(self.status_label, 1)

        # 对话框主布局
        self.viewLayout.setSpacing(12)
        self.viewLayout.setContentsMargins(24, 20, 24, 16)
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.status_card)
        self.viewLayout.addWidget(self.tip_label)
        self.viewLayout.addWidget(self.install_btn)
        self.widget.setMinimumWidth(400)

    def __connect_signal_to_slot(self):
        self.install_btn.clicked.connect(self._on_install)

    def _refresh(self):
        """按当前安装状态刷新图标、文案与按钮可见性。"""
        installed = self._cert.check()
        if installed:
            self.icon_widget.setIcon(FluentIcon.ACCEPT_MEDIUM)
            self.status_label.setText(self.tr("证书已安装"))
            self.install_btn.setVisible(False)
        else:
            self.icon_widget.setIcon(FluentIcon.CANCEL_MEDIUM)
            self.status_label.setText(self.tr("证书未安装"))
            self.install_btn.setVisible(True)
        self.status_changed.emit(installed)

    @Slot()
    def _on_install(self):
        """点击安装：生成（如需要）+ 安装证书，完成后刷新。"""
        try:
            self._cert.install()
            show_success("成功", "证书已安装", parent=self)
        except RuntimeError as e:
            show_warning("安装失败", str(e), parent=self)
        self._refresh()


class ProxyPortDialog(MessageBoxBase):
    """代理端口设置对话框"""

    PORT_MIN = 1024
    PORT_MAX = 65535

    def __init__(self, current_port, parent: QWidget):
        """初始化端口设置对话框

        Args:
            current_port: 当前端口号
            parent: 父组件
        """
        super().__init__(parent)
        self.__init_widget(current_port)
        self.__init_layout()

    def __init_widget(self, current_port: int):
        """初始化界面组件

        :param int current_port: 当前端口号
        """
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("设置代理端口"))
        self.port_spin = SpinBox(self)
        self.port_spin.setRange(self.PORT_MIN, self.PORT_MAX)
        self.port_spin.setValue(current_port)
        self.port_spin.setSingleStep(1)

    def __init_layout(self):
        """初始化布局结构"""
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.port_spin)
        self.widget.setMinimumWidth(350)

    def get_port(self) -> int:
        """获取用户设置的端口号

        :return: 用户设置的端口号，例如 8080
        """
        return self.port_spin.value()
