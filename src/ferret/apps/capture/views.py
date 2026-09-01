from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    InfoBadge,
    InfoBadgePosition,
    LineEdit,
    MessageBoxBase,
    RoundMenu,
    SearchLineEdit,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    VerticalSeparator,
    isDarkTheme,
    setCustomStyleSheet,
)
from sysproxy import SystemProxyService

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.apps.common.filter import MultiFilterManager
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.common.icon import BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.core.mitm.facade import MitmFacade
from ferret.core.mitm.modes import (
    WIREGUARD_PORT,
    LocalTarget,
    checked_tokens,
    list_local_targets,
    qr_matrix,
    split_spec,
)
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, PORT_MAX, PORT_MIN

if TYPE_CHECKING:
    from ferret.apps.window import MainWindow

# 本地重定向选择器的视觉常量：色值实测 dump 自 qfw LineEdit 官方 QSS，勿手改。
_BORDER_LIGHT = "rgba(0, 0, 0, 13)"
_BORDER_DARK = "rgba(255, 255, 255, 0.08)"
_PICKER_ROW_HEIGHT = 33
_PICKER_MAX_ROWS = 8
_PICKER_FRAME_HEIGHT = 33


class CapturesInterface(QWidget):
    """抓包主界面 - 包含工具栏、搜索面板和内容区域"""

    # 右键「屏蔽此主机」向外转发，由 MainWindow 接到 BlockListController
    block_host_requested = Signal(str)

    def __init__(
        self,
        parent: "MainWindow | None" = None,
        *,
        mitm: MitmFacade | None = None,
        system_proxy: SystemProxyService | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("CapturesInterface")
        self.controller = CaptureController(self, mitm=mitm, system_proxy=system_proxy)
        self._ui_state = CaptureUiState(
            capture_state=CaptureState.STOPPED,
            endpoint=self.controller.local_endpoint,
            lan_exposed=self.controller.is_lan_exposed,
            total_count=0,
            shown_count=0,
            selected_count=0,
            active_filter_count=0,
        )

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.__init_shortcuts()
        self._refresh_command_bar()

    def __init_widget(self):
        """初始化界面组件"""
        self.command_bar = CaptureCommandBar(self)
        self.filter_panel = CaptureFilterPanel(self)
        # Compatibility alias for callers and existing tests.
        self.toolbar = self.command_bar
        self.content = CapturesContentArea(self, self.controller)

    def __init_layout(self):
        """初始化布局结构"""
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.addWidget(self.command_bar)
        self.main_layout.addWidget(self.filter_panel)
        self.main_layout.addWidget(self.content, 1)

    def __connect_signal_to_slot(self):
        """协调层：连接组件业务信号到 Controller"""
        self.command_bar.captureToggled.connect(self.__on_capture_toggled)
        self.command_bar.filterToggled.connect(self.__toggle_filter_panel)
        self.command_bar.openRequested.connect(self.__on_open_flow_file_requested)
        self.command_bar.portRequested.connect(self.__show_proxy_port_dialog)
        self.command_bar.locateRequested.connect(self.content.table.on_locate_selection)
        self.command_bar.clearRequested.connect(self.__confirm_clear_flows)

        # 右键菜单"从文件回放…"信号 → 弹 file dialog → 调 controller
        self.content.table.context_menu.replay_file_requested.connect(
            self.__on_replay_from_file_requested
        )

        # 右键"屏蔽此主机"信号 → 冒泡给 MainWindow
        self.content.table.context_menu.block_host_requested.connect(
            self.block_host_requested
        )

        # Controller 状态信号 → UI 更新
        self.controller.capture_state_changed.connect(self.__on_capture_state_changed)
        self.controller.recordingChanged.connect(lambda _on: self._refresh_command_bar())
        self.controller.channels_changed.connect(self._refresh_command_bar)
        self.controller.master_ready.connect(self.content.table.set_view)
        self.controller.flow_added.connect(self.content.table.on_flow_added)
        self.controller.flow_updated.connect(self.content.table.on_flow_updated)
        self.controller.flow_removed.connect(self.content.table.on_flow_removed)
        self.controller.view_refreshed.connect(self.content.table.on_view_refreshed)

        self.filter_panel.conditionsChanged.connect(self.__on_search_changed)
        self.filter_panel.panelCloseRequested.connect(self.__hide_filter_panel)

        # 统计信息更新
        self.content.table.stats_updated.connect(self.__on_stats_updated)

    def __init_shortcuts(self) -> None:
        QShortcut(QKeySequence.StandardKey.Find, self).activated.connect(
            self.__show_and_focus_filter
        )
        QShortcut(
            QKeySequence(Qt.Key.Key_Return), self.content.table
        ).activated.connect(self.content.open_selected)
        QShortcut(QKeySequence(Qt.Key.Key_Enter), self.content.table).activated.connect(
            self.content.open_selected
        )
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self).activated.connect(
            self.__handle_escape
        )
        QShortcut(QKeySequence("Ctrl+L"), self).activated.connect(
            self.content.table.on_locate_selection
        )
        QShortcut(QKeySequence.StandardKey.Open, self).activated.connect(
            self.command_bar.openRequested.emit
        )
        QShortcut(QKeySequence("Ctrl+Shift+Delete"), self).activated.connect(
            self.__confirm_clear_flows
        )
        QShortcut(QKeySequence(Qt.Key.Key_Space), self).activated.connect(
            self.__toggle_capture_from_shortcut
        )
        QWidget.setTabOrder(self.command_bar.search_btn, self.command_bar.open_btn)
        QWidget.setTabOrder(self.command_bar.open_btn, self.command_bar.environment_btn)
        QWidget.setTabOrder(
            self.command_bar.environment_btn, self.command_bar.proxy_setting_btn
        )
        QWidget.setTabOrder(
            self.command_bar.proxy_setting_btn,
            self.command_bar.locate_selection_btn,
        )
        QWidget.setTabOrder(
            self.command_bar.locate_selection_btn, self.command_bar.control_btn
        )
        QWidget.setTabOrder(
            self.command_bar.control_btn, self.command_bar.captures_delete_btn
        )
        QWidget.setTabOrder(self.command_bar.captures_delete_btn, self.content.table)

    @Slot(bool)
    def __on_capture_toggled(self, _is_on: bool):
        """协调：command bar 信号 → controller 操作。"""
        self.controller.toggle_capture()

    @Slot(object)
    def __on_capture_state_changed(self, state: object):
        """协调：controller 生命周期状态 → UI 更新。"""
        capture_state = CaptureState(state)
        previous = self._ui_state.capture_state
        self._ui_state = replace(self._ui_state, capture_state=capture_state)
        self._refresh_command_bar()
        if capture_state == CaptureState.RUNNING and previous != CaptureState.RUNNING:
            show_success(
                self.tr("Success"),
                self.tr("System traffic capture started"),
                parent=self,
            )
        elif capture_state == CaptureState.STOPPED and previous in (
            CaptureState.RUNNING,
            CaptureState.STOPPING,
        ):
            show_success(
                self.tr("Success"),
                self.tr("System traffic capture stopped"),
                parent=self,
            )
        elif capture_state == CaptureState.FAILED:
            show_warning(
                self.tr("System traffic capture failed"),
                self.controller.last_error
                or self.tr("Check the listen port and the system proxy settings"),
                parent=self,
            )

    @Slot()
    def __on_search_changed(self):
        """搜索条件变更时更新过滤。

        把 GUI 条件交给 Controller，由 View.set_filter(flowfilter 表达式) 统一做
        「显示过滤」——_store 保留全部流量，仅 _view 可见列表变化，无清除效果。
        """
        conditions = self.filter_panel.get_conditions()
        self.controller.apply_filter(conditions)
        self._ui_state = replace(
            self._ui_state,
            active_filter_count=self.filter_panel.active_condition_count(),
        )
        self._refresh_command_bar()

    @Slot()
    def __show_proxy_port_dialog(self):
        """弹出抓包通道设置对话框"""
        w = ProxyPortDialog(
            self.controller.current_port,
            self.window(),
            is_running=self.controller.is_capturing,
            listen_host=self.controller.current_host,
            block_global=self.controller.block_global,
            block_private=self.controller.block_private,
            lan_address=self.controller.lan_address(),
            use_system_proxy=self.controller.system_proxy_enabled(),
            use_local=self.controller.use_local,
            local_spec=self.controller.local_spec,
            use_wireguard=self.controller.use_wireguard,
            wireguard_config=self.controller.wireguard_client_config,
        )
        if not w.exec():
            return
        try:
            # 顺序有讲究：先提交通道（校验失败就整体中止，且抓包中重启端点前
            # 内核意图值必须先更新，否则重启会带上旧 spec），再提交端点/来源限制。
            self.controller.update_channels(
                use_system_proxy=w.get_use_system_proxy(),
                use_local=w.get_use_local(),
                local_spec=w.get_local_spec(),
                use_wireguard=w.get_use_wireguard(),
            )
            self.controller.update_proxy_settings(
                listen_host=w.get_listen_host(),
                listen_port=w.get_port(),
                block_global=w.get_block_global(),
                block_private=w.get_block_private(),
            )
        except (RuntimeError, ValueError) as exc:
            show_warning(self.tr("Capture settings not applied"), str(exc), self.window())
            return
        # 监听端点也可能顺带变了（端口在对话框里可改），读回刷新。
        self._ui_state = replace(
            self._ui_state,
            endpoint=self.controller.local_endpoint,
            lan_exposed=self.controller.is_lan_exposed,
        )
        self._refresh_command_bar()

    @Slot(int, int, int)
    def __on_stats_updated(self, total: int, shown: int, selected: int) -> None:
        self._ui_state = replace(
            self._ui_state,
            total_count=total,
            shown_count=shown,
            selected_count=selected,
        )
        self._refresh_command_bar()

    @Slot()
    def __on_replay_from_file_requested(self) -> None:
        """用户点击"从文件回放…"：弹文件选择器并调用 controller。"""
        path, _ = QFileDialog.getOpenFileName(
            self.window(),
            self.tr("Select a .flow file to replay"),
            "",
            self.tr("Flow files (*.flow)"),
        )
        if not path:
            return
        try:
            self.controller.load_replay_file(Path(path))
        except Exception as exc:  # noqa: BLE001
            show_warning(self.tr("Replay failed"), str(exc), parent=self)

    @Slot()
    def __on_open_flow_file_requested(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window(),
            self.tr("Load flows into the current list"),
            "",
            self.tr("Flow files (*.flow)"),
        )
        if not path:
            return
        try:
            count = self.controller.load_flow_file(Path(path))
            show_success(
                self.tr("Loaded"),
                self.tr("Loaded {} flow(s)").format(count),
                parent=self,
            )
        except Exception as exc:  # noqa: BLE001
            show_warning(self.tr("Load failed"), str(exc), parent=self)

    @Slot()
    def __confirm_clear_flows(self) -> None:
        if self._ui_state.total_count <= 0:
            return
        dialog = ClearFlowsDialog(self._ui_state.total_count, self.window())
        if dialog.exec():
            self.content.table.clear_all()

    @Slot()
    def __toggle_filter_panel(self) -> None:
        self.filter_panel.setVisible(not self.filter_panel.isVisible())
        if self.filter_panel.isVisible():
            self.filter_panel.focus_first_input()
        self._refresh_command_bar()

    @Slot()
    def __hide_filter_panel(self) -> None:
        self.filter_panel.hide()
        self._refresh_command_bar()

    def __show_and_focus_filter(self) -> None:
        self.filter_panel.show()
        self.filter_panel.focus_first_input()
        self._refresh_command_bar()

    def __handle_escape(self) -> None:
        if self.content.is_panel_expanded():
            self.content.collapse_panel()
        elif self.filter_panel.isVisible():
            self.__hide_filter_panel()

    def __toggle_capture_from_shortcut(self) -> None:
        focus = QApplication.focusWidget()
        if focus is not None and self.filter_panel.isAncestorOf(focus):
            return
        if isinstance(
            focus,
            (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox),
        ):
            return
        if self.controller.capture_state in (
            CaptureState.STOPPED,
            CaptureState.FAILED,
            CaptureState.RUNNING,
        ):
            self.controller.toggle_capture()

    def _refresh_command_bar(self) -> None:
        self._ui_state = replace(
            self._ui_state,
            channels_summary=self._channels_summary(),
            channel_issue=self._channel_issue(),
        )
        self.command_bar.set_state(self._ui_state, self.filter_panel.isVisible())
        self.content.set_capture_context(
            capture_state=self._ui_state.capture_state,
            endpoint=self._ui_state.endpoint,
            total_count=self._ui_state.total_count,
            shown_count=self._ui_state.shown_count,
            active_filter_count=self._ui_state.active_filter_count,
        )

    def _channels_summary(self) -> str:
        """抓包会话开启时端点位置要显示的通道并集；未开启返回空串。"""
        if self._ui_state.capture_state not in (
            CaptureState.STARTING,
            CaptureState.RUNNING,
            CaptureState.STOPPING,
        ):
            return ""
        parts: list[str] = []
        if self.controller.system_proxy_enabled():
            parts.append(self.tr("System proxy"))
        if self.controller.use_local:
            spec = self.controller.local_spec
            label = self.tr("Local redirect")
            parts.append(f"{label} ({spec})" if spec else label)
        if self.controller.use_wireguard:
            parts.append(self.tr("WireGuard :{}").format(WIREGUARD_PORT))
        return " · ".join(parts)

    def _channel_issue(self) -> str:
        errors = self.controller.channel_errors
        if not errors:
            return ""
        names = {"local": self.tr("Local redirect"), "wireguard": self.tr("WireGuard")}
        return "; ".join(
            f"{names.get(key, key)}: {message}" for key, message in errors.items()
        )

    def stop_capture(self):
        """停止抓包（供外部调用，如MainWindow.closeEvent）"""
        self.controller.stop_capture()


class CapturesContentArea(FlowViewerPane):
    """Capture-specific name for the shared Flow viewer."""

    def __init__(
        self,
        parent: CapturesInterface,
        controller: CaptureController,
    ) -> None:
        super().__init__(parent=parent, controller=controller)


@dataclass(frozen=True, slots=True)
class CaptureUiState:
    capture_state: CaptureState
    endpoint: str
    total_count: int
    shown_count: int
    selected_count: int
    active_filter_count: int
    # endpoint 恒为本机环回端点；这一位单独说明「局域网设备也能连进来」。
    # 两者不能合并：把局域网地址显示成端点会误导用户去改系统代理。
    lan_exposed: bool = False
    # 抓包会话开启时端点位置改显通道并集（System proxy · Local redirect · …）；
    # 未开启时为空，命令栏回落到 endpoint。
    channels_summary: str = ""
    # 通道健康检查发现的问题（如 UAC 拒绝），非空时以 ⚠ 标注并进提示。
    channel_issue: str = ""


class CaptureFilterPanel(MultiFilterManager):
    """Capture-specific full-width advanced filter band."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CaptureFilterPanel")
        self.setStyleSheet(
            "#CaptureFilterPanel {"
            " background: rgba(127, 127, 127, 0.06);"
            " border-top: 1px solid rgba(127, 127, 127, 0.16);"
            " border-bottom: 1px solid rgba(127, 127, 127, 0.16);"
            "}"
        )


class CaptureCommandBar(QWidget):
    """Compact capture status and command bar."""

    captureToggled = Signal(bool)
    filterToggled = Signal()
    openRequested = Signal()
    clearRequested = Signal()
    portRequested = Signal()
    locateRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._state: CaptureUiState | None = None
        self._filter_panel_visible = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.setFixedHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.state_dot = BodyLabel("●", self)
        self.state_dot.setFixedWidth(12)
        self.state_label = StrongBodyLabel(self.tr("Idle"), self)

        self.endpoint_label = BodyLabel(self)
        self.endpoint_label.setFixedHeight(28)
        self.endpoint_label.setAccessibleName(self.tr("Proxy listen address"))
        self.endpoint_label.setFont(self.font())
        # Compatibility alias for callers that read the endpoint text/visibility.
        self.endpoint_btn = self.endpoint_label

        # 放开到局域网是个有安全含义的状态，必须常驻可见，不能只藏在设置对话框里。
        self.exposure_label = CaptionLabel(self.tr("LAN"), self)
        self.exposure_label.setFixedHeight(28)
        self.exposure_label.setAccessibleName(self.tr("Reachable from LAN devices"))
        self.exposure_label.setStyleSheet("color: #c07000;")
        self.exposure_label.setVisible(False)

        self.stats_label = CaptionLabel(self.tr("{} flow(s)").format(0), self)

        self.search_btn = TransparentToolButton(FluentIcon.SEARCH, self)
        self.search_btn.setCheckable(True)
        self.search_btn.setToolTip(self.tr("Advanced search") + " (Ctrl+F)")
        self.search_btn.setAccessibleName(self.tr("Advanced search"))
        self.open_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.open_btn.setToolTip(self.tr("Load flows into the current list"))
        self.open_btn.setAccessibleName(self.tr("Load flows into the current list"))
        self.filter_badge = InfoBadge.attension(
            0, self, self.search_btn, InfoBadgePosition.TOP_RIGHT
        )
        self.filter_badge.hide()

        self.proxy_setting_btn = TransparentToolButton(FluentIcon.GLOBE, self)
        self.proxy_setting_btn.setToolTip(self.tr("Port settings"))
        self.proxy_setting_btn.setAccessibleName(self.tr("Port settings"))

        self.environment_btn = TransparentToolButton(FluentIcon.MORE, self)
        self.environment_btn.setToolTip(self.tr("Environment settings"))
        self.environment_btn.setAccessibleName(self.tr("Environment settings"))
        self.environment_btn.hide()

        self.locate_selection_btn = TransparentToolButton(
            BaseIcon.LOCATION_TARGET, self
        )
        self.locate_selection_btn.setToolTip(self.tr("Locate selection"))
        self.locate_selection_btn.setAccessibleName(self.tr("Locate selection"))

        self.control_btn = TransparentToolButton(FluentIcon.PLAY, self)
        self.control_btn.setToolTip(self.tr("Start capturing system traffic"))
        self.control_btn.setAccessibleName(self.tr("Start capturing system traffic"))

        self.captures_delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.captures_delete_btn.setToolTip(self.tr("Clear current flows"))
        self.captures_delete_btn.setAccessibleName(self.tr("Clear current flows"))

        self.separator = VerticalSeparator(self)
        self.separator.setFixedHeight(16)

        for button in (
            self.search_btn,
            self.open_btn,
            self.proxy_setting_btn,
            self.environment_btn,
            self.locate_selection_btn,
            self.control_btn,
            self.captures_delete_btn,
        ):
            button.setFixedSize(32, 32)
            button.setIconSize(QSize(18, 18))
            button.installEventFilter(ToolTipFilter(button, 700, ToolTipPosition.TOP))

    def __init_layout(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)
        layout.addWidget(self.state_dot)
        layout.addWidget(self.state_label)
        layout.addSpacing(6)
        layout.addWidget(self.endpoint_btn)
        layout.addWidget(self.exposure_label)
        layout.addSpacing(4)
        layout.addWidget(self.stats_label)
        layout.addSpacing(6)
        layout.addStretch(1)
        layout.addWidget(self.search_btn)
        layout.addWidget(self.open_btn)
        layout.addSpacing(4)
        layout.addWidget(self.proxy_setting_btn)
        layout.addWidget(self.environment_btn)
        layout.addSpacing(4)
        layout.addWidget(self.locate_selection_btn)
        layout.addWidget(self.control_btn)
        layout.addWidget(self.separator)
        layout.addWidget(self.captures_delete_btn)

    def __connect_signal_to_slot(self):
        """组件内部事件管理"""
        self.control_btn.clicked.connect(self.__emit_capture_toggle)
        self.search_btn.clicked.connect(self.filterToggled.emit)
        self.open_btn.clicked.connect(self.openRequested.emit)
        self.proxy_setting_btn.clicked.connect(self.portRequested.emit)
        self.environment_btn.clicked.connect(self.__show_environment_menu)
        self.locate_selection_btn.clicked.connect(self.locateRequested.emit)
        self.captures_delete_btn.clicked.connect(self.clearRequested.emit)

    @Slot()
    def __emit_capture_toggle(self) -> None:
        running = bool(
            self._state and self._state.capture_state == CaptureState.RUNNING
        )
        self.captureToggled.emit(not running)

    @Slot()
    def __show_environment_menu(self) -> None:
        menu = RoundMenu(parent=self)
        port_action = Action(FluentIcon.GLOBE, self.tr("Port settings"), menu)
        port_action.triggered.connect(self.portRequested.emit)
        menu.addAction(port_action)
        menu.exec(
            self.environment_btn.mapToGlobal(QPoint(0, self.environment_btn.height()))
        )

    def set_state(self, state: CaptureUiState, filter_panel_visible: bool) -> None:
        self._state = state
        self._filter_panel_visible = filter_panel_visible

        # 表是每次调用重建的局部变量，所以就地 tr() 没有「求值早于翻译器」的问题；
        # 反过来说，原先在使用点写 `self.tr(label)`（label 是变量）lupdate 一条都
        # 提不出来 —— 它只认字面量实参。
        state_ui = {
            CaptureState.STOPPED: (
                self.tr("Idle"),
                "#8a8a8a",
                FluentIcon.PLAY,
                self.tr("Start capturing"),
                True,
            ),
            CaptureState.STARTING: (
                self.tr("Starting"),
                "#d99a00",
                FluentIcon.PLAY,
                self.tr("Starting the capture session"),
                False,
            ),
            CaptureState.RUNNING: (
                self.tr("Capturing"),
                "#2e9b4d",
                FluentIcon.PAUSE,
                self.tr("Stop capturing"),
                True,
            ),
            CaptureState.STOPPING: (
                self.tr("Stopping"),
                "#d99a00",
                FluentIcon.PAUSE,
                self.tr("Stopping the capture session"),
                False,
            ),
            CaptureState.FAILED: (
                self.tr("Failed"),
                "#d13438",
                FluentIcon.PLAY,
                self.tr("Retry capturing"),
                True,
            ),
        }
        label, color, icon, tooltip, enabled = state_ui[state.capture_state]
        self.state_label.setText(label)
        self.state_dot.setStyleSheet(f"color: {color};")
        self.state_dot.setAccessibleName(label)
        self.control_btn.setIcon(icon)
        self.control_btn.setEnabled(enabled)
        self.control_btn.setToolTip(tooltip)
        self.control_btn.setAccessibleName(tooltip)

        # 抓包会话开着时端点位置显示通道并集；未开启显示本机接入端点。
        # 通道健康有问题（如 UAC 拒绝）时前缀 ⚠ 并把详情放进提示。
        display = state.channels_summary or state.endpoint
        if state.channel_issue:
            self.endpoint_btn.setText(f"⚠ {display}")
            self.endpoint_btn.setToolTip(state.channel_issue)
        else:
            self.endpoint_btn.setText(display)
            self.endpoint_btn.setToolTip(
                self.tr(
                    "This machine connects via {}; LAN devices can connect too"
                ).format(state.endpoint)
                if state.lan_exposed
                else self.tr("This machine connects via {}").format(state.endpoint)
            )
        if state.shown_count == state.total_count:
            stats_text = self.tr("{} flow(s)").format(state.total_count)
        else:
            stats_text = self.tr("{} / {} flow(s)").format(
                state.shown_count, state.total_count
            )
        self.stats_label.setText(stats_text)
        self.stats_label.setToolTip(
            self.tr("{} total, {} shown, {} selected").format(
                state.total_count, state.shown_count, state.selected_count
            )
        )

        self.search_btn.setChecked(
            filter_panel_visible or state.active_filter_count > 0
        )
        self.filter_badge.setText(str(state.active_filter_count))
        self.filter_badge.setVisible(state.active_filter_count > 0)
        self.filter_badge.adjustSize()
        self.filter_badge.raise_()

        self.captures_delete_btn.setEnabled(state.total_count > 0)
        self._apply_compact_mode(self.width())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_compact_mode(event.size().width())

    def _apply_compact_mode(self, width: int) -> None:
        if self._state is None:
            return
        compact = width < 900
        very_compact = width < 720
        # 抓包会话开着时端点位置是通道摘要，它比端点地址更值得占宽度，不再压缩。
        base = self._state.channels_summary or self._state.endpoint
        if self._state.channel_issue:
            base = f"⚠ {base}"
        endpoint = base if self._state.channels_summary else (
            f":{base.rsplit(':', 1)[-1]}" if compact else base
        )
        self.endpoint_btn.setText(endpoint)
        if self._state.shown_count == self._state.total_count:
            stats = (
                str(self._state.total_count)
                if compact
                else self.tr("{} flow(s)").format(self._state.total_count)
            )
        else:
            if compact:
                stats = f"{self._state.shown_count}/{self._state.total_count}"
            else:
                stats = self.tr("{} / {} flow(s)").format(
                    self._state.shown_count, self._state.total_count
                )
        self.stats_label.setText(stats)
        self.endpoint_btn.setVisible(not very_compact)
        self.exposure_label.setVisible(self._state.lan_exposed and not very_compact)
        self.proxy_setting_btn.setVisible(not very_compact)
        self.environment_btn.setVisible(very_compact)

    def update_stats(self, total: int, shown: int, selected: int) -> None:
        """Compatibility helper retained for external callers."""
        if self._state is None:
            return
        self.set_state(
            replace(
                self._state,
                total_count=total,
                shown_count=shown,
                selected_count=selected,
            ),
            self._filter_panel_visible,
        )


# Compatibility name retained for imports outside this module.
CapturesToolBar = CaptureCommandBar


class ClearFlowsDialog(MessageBoxBase):
    """Confirmation for clearing unsaved capture rows."""

    def __init__(self, flow_count: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = SubtitleLabel(
            self.tr("Clear the current {} flow(s)?").format(flow_count), self
        )
        self.desc_label = BodyLabel(
            self.tr("This cannot be undone, but saved sessions are not deleted."), self
        )
        self.desc_label.setWordWrap(True)
        self.yesButton.setText(self.tr("Clear"))
        self.cancelButton.setText(self.tr("Cancel"))
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(380)


class _CheckDelegate(QStyledItemDelegate):
    """列表条目委托：标准绘制（图标 + 文本必然渲染）+ qfw 同款青色勾。

    青勾画法照抄上游 ``CheckIndicatorMenuItemDelegate``（qfw 菜单的勾选指示
    器），勾画在行右缘、仅勾选条目显示。文字走 Qt 标准的 DisplayRole 绘制，
    不受半透明弹窗调色板问题影响（前三版文字消失的教训）。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._accept = FluentIcon.ACCEPT

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        super().paint(painter, option, index)
        if index.data(Qt.ItemDataRole.CheckStateRole) != Qt.CheckState.Checked:
            return
        painter.save()
        rect = option.rect
        size = 11
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        if not option.state & QStyle.StateFlag.State_MouseOver:
            painter.setOpacity(0.75)
        self._accept.render(
            painter,
            QRectF(rect.right() - size - 12, rect.center().y() - size / 2, size, size),
        )
        painter.restore()


class _PickerPanel(QDialog):
    """进程勾选面板：搜索 + 原生列表条目（图标 + 文本 + 青勾）+ 手输行。

    一切皆条目：候选进程与手输 token（如 ``!1234`` 排除项）都是同等的勾选
    条目，spec = 勾选文本按列表顺序连接，没有 manual/checked 两本账。弹窗
    **不透明**且条目走原生渲染路径——半透明弹窗的调色板文字色会失效，这是
    前几版「文字消失」的根因。
    """

    tokensChanged = Signal(list)

    def __init__(self, anchor: QWidget, targets: list[LocalTarget], tokens: list[str]):
        super().__init__(anchor, Qt.WindowType.Popup)
        self.setFixedSize(
            max(anchor.width(), 320),
            min(len(targets), _PICKER_MAX_ROWS) * _PICKER_ROW_HEIGHT + 148,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._search = SearchLineEdit(self)
        self._search.setPlaceholderText(self.tr("Search processes"))
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.setItemDelegate(_CheckDelegate(self._list))
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setUniformItemSizes(True)
        color = QColor(Qt.GlobalColor.white) if isDarkTheme() else QColor(Qt.GlobalColor.black)
        spec = ",".join(tokens)
        for target in targets:
            item = self._make_item(target.display_name, self._icon(target), color)
            item.setCheckState(
                Qt.CheckState.Checked
                if checked_tokens(spec, [target])
                else Qt.CheckState.Unchecked
            )
        # 对不上任何候选的手输 token（如 !1234）也各成一条，默认勾选。
        matched = {label.lower() for label in checked_tokens(spec, targets)}
        for token in tokens:
            if token.lower() not in matched:
                item = self._make_item(token, None, color)
                item.setCheckState(Qt.CheckState.Checked)
        layout.addWidget(self._list, 1)

        self._add_edit = LineEdit(self)
        self._add_edit.setPlaceholderText(
            self.tr("Add process name or !pid and press Enter")
        )
        self._add_edit.setClearButtonEnabled(True)
        self._add_edit.returnPressed.connect(self._commit_manual)
        layout.addWidget(self._add_edit)

        self._list.itemChanged.connect(lambda _item: self._emit_tokens())
        self._search.textChanged.connect(self._filter)

    def _make_item(
        self, label: str, icon: QIcon | None, color: QColor
    ) -> QListWidgetItem:
        item = QListWidgetItem(label, self._list)
        if icon is not None:
            item.setIcon(icon)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setForeground(color)
        item.setSizeHint(QSize(0, _PICKER_ROW_HEIGHT))
        return item

    def _icon(self, target: LocalTarget) -> QIcon:
        pixmap = QPixmap()
        # 解码失败时 pixmap 保持空图，空 QIcon 渲染即无图标。
        pixmap.loadFromData(target.icon_png or b"")
        return QIcon(pixmap)

    def _commit_manual(self) -> None:
        token = self._add_edit.text().strip()
        if not token:
            return
        self._add_edit.clear()
        lowered = token.lower()
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.text().lower() == lowered:
                item.setCheckState(Qt.CheckState.Checked)
                return
        for row in range(self._list.count()):
            item = self._list.item(row)
            if lowered in item.text().lower() or item.text().lower() in lowered:
                item.setCheckState(Qt.CheckState.Checked)
                return
        color = QColor(Qt.GlobalColor.white) if isDarkTheme() else QColor(Qt.GlobalColor.black)
        self._make_item(token, None, color).setCheckState(Qt.CheckState.Checked)

    def _filter(self, query: str) -> None:
        needle = query.strip().lower()
        for row in range(self._list.count()):
            self._list.item(row).setHidden(
                bool(needle) and needle not in self._list.item(row).text().lower()
            )

    def _emit_tokens(self) -> None:
        # 被搜索隐藏的已勾条目必须保留——隐藏只是视图过滤，不是取消勾选。
        self.tokensChanged.emit(self.checked_labels())

    def checked_labels(self) -> list[str]:
        return [
            self._list.item(row).text()
            for row in range(self._list.count())
            if self._list.item(row).checkState() == Qt.CheckState.Checked
        ]


class LocalSpecSelector(QFrame):
    """本地重定向过滤串：摘要行 + 下拉勾选面板（含搜索与手输）。

    点整行或 ▾ 弹出面板（运行中的用户程序 + 手输条目），勾选实时回写
    tokens；``tokens()`` 是唯一事实来源（逗号连接即过滤串）。首次展开时枚举
    一次进程并缓存。
    """

    tokensChanged = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("LocalSpecSelector")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        setCustomStyleSheet(
            self,
            f"#LocalSpecSelector {{ border: 1px solid {_BORDER_LIGHT};"
            f" border-radius: 4px; background-color: rgba(249, 249, 249, 0.3); }}"
            f"#LocalSpecSelector:hover {{ background-color: rgba(0, 0, 0, 9); }}",
            f"#LocalSpecSelector {{ border: 1px solid {_BORDER_DARK};"
            f" border-radius: 4px; background-color: rgba(255, 255, 255, 0.0419); }}"
            f"#LocalSpecSelector:hover {{ background-color: rgba(255, 255, 255, 9); }}",
        )
        self._targets: list[LocalTarget] | None = None
        self._tokens: list[str] = []
        self._summary = BodyLabel(self)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._button = TransparentToolButton(FluentIcon.CHEVRON_DOWN_MED, self)
        self._button.setFixedSize(28, 28)
        self._button.clicked.connect(self._open_panel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 6, 0)
        layout.setSpacing(4)
        layout.addWidget(self._summary, 1)
        layout.addWidget(self._button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setFixedHeight(_PICKER_FRAME_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_summary()

    def tokens(self) -> list[str]:
        return list(self._tokens)

    def set_tokens(self, tokens: list[str]) -> None:
        self._tokens = [token.strip() for token in tokens if token.strip()]
        self._refresh_summary()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._open_panel()
        super().mousePressEvent(event)

    def _open_panel(self) -> None:
        if self._targets is None:
            self._targets = list_local_targets()
        panel = _PickerPanel(self, self._targets, self._tokens)
        panel.tokensChanged.connect(self._on_panel_changed)
        panel.move(self.mapToGlobal(QPoint(0, self.height())))
        panel.exec()
        # 关面板后以最终勾选为准（与实时信号同值，兜底防漏）。
        self._on_panel_changed(panel.checked_labels())

    def _on_panel_changed(self, labels: list[str]) -> None:
        if labels == self._tokens:
            return
        self._tokens = labels
        self._refresh_summary()
        self.tokensChanged.emit(self.tokens())

    def _refresh_summary(self) -> None:
        if not self._tokens:
            self._summary.setText(self.tr("Leave empty to capture every process"))
            self._summary.setStyleSheet("color: rgba(127, 127, 127, 0.9);")
            return
        text = ", ".join(self._tokens)
        metrics = self._summary.fontMetrics()
        available = max(self._summary.width() - 8, 40)
        if metrics.horizontalAdvance(text) > available:
            while text and metrics.horizontalAdvance(text + "…") > available:
                text = text[:-1]
            text += "…"
        self._summary.setText(self.tr("Selected: {}").format(text))
        self._summary.setStyleSheet("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_summary()

class ProxyPortDialog(MessageBoxBase):
    """代理监听设置：绑定地址、端口、来源限制。

    三个地址各有各的用途，别在这里混起来（见 core/network.py 的模块注释）：
    绑定地址决定「谁能连进来」，本机接入地址恒为环回，局域网地址只用于显示和复制。
    """

    # 端口范围直接取 core/network 的常量，保证对话框和配置的收敛逻辑不会各说一套。
    PORT_MIN = PORT_MIN
    PORT_MAX = PORT_MAX

    _HOSTS: tuple[str, ...] = (LOOPBACK_HOST, ANY_HOST)

    def __init__(
        self,
        current_port: int,
        parent: QWidget | None = None,
        *,
        is_running: bool = False,
        listen_host: str = LOOPBACK_HOST,
        block_global: bool = True,
        block_private: bool = False,
        lan_address: str | None = None,
        use_system_proxy: bool = True,
        use_local: bool = True,
        local_spec: str = "",
        use_wireguard: bool = True,
        wireguard_config: Callable[[], str] | None = None,
    ):
        """初始化代理监听设置对话框

        Args:
            current_port: 当前端口号
            parent: 父组件
            is_running: 内核是否在跑，决定要不要提示「修改后将重启」
            listen_host: 当前绑定地址
            block_global: 当前是否拒绝公网来源
            block_private: 当前是否拒绝局域网来源
            lan_address: 本机局域网 IPv4，None 表示探测失败
            use_system_proxy: 系统代理通道是否启用
            use_local: 本地重定向通道是否启用
            local_spec: 本地重定向的进程过滤串
            use_wireguard: WireGuard 通道是否启用
            wireguard_config: 取客户端配置文本的回调（None 表示按钮隐藏）
        """
        super().__init__(parent)
        self._lan_address = lan_address
        self._wireguard_config = wireguard_config
        self.__init_widget(
            current_port,
            is_running,
            listen_host,
            block_global,
            block_private,
            use_system_proxy,
            use_local,
            local_spec,
            use_wireguard,
        )
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._sync_exposure()

    def __init_widget(
        self,
        current_port: int,
        is_running: bool,
        listen_host: str,
        block_global: bool,
        block_private: bool,
        use_system_proxy: bool,
        use_local: bool,
        local_spec: str,
        use_wireguard: bool,
    ):
        """初始化界面组件"""
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("Capture channels"))

        # —— 系统代理通道 ——
        self.system_proxy_check = CheckBox(
            self.tr("System proxy (browsers and most desktop apps)"), self
        )
        self.system_proxy_check.setChecked(use_system_proxy)

        self.host_combo = ComboBox(self)
        self.host_combo.addItems(
            [
                self.tr("This machine only ({})").format(LOOPBACK_HOST),
                self.tr("Reachable from LAN ({})").format(ANY_HOST),
            ]
        )
        self.host_combo.setCurrentIndex(
            self._HOSTS.index(listen_host) if listen_host in self._HOSTS else 0
        )

        self.port_spin = SpinBox(self)
        self.port_spin.setRange(self.PORT_MIN, self.PORT_MAX)
        self.port_spin.setValue(current_port)
        self.port_spin.setSingleStep(1)

        # 本机这条路径永远不变，写在最显眼的地方 —— 用户最容易误以为
        # 放开监听之后系统代理也得跟着改。
        self.local_hint = CaptionLabel(self)
        self.local_hint.setWordWrap(True)

        self.lan_label = BodyLabel(self)
        self.lan_value = CaptionLabel(self)
        self.lan_copy_btn = TransparentToolButton(FluentIcon.COPY, self)
        self.lan_copy_btn.setToolTip(self.tr("Copy LAN address"))
        self.lan_copy_btn.setAccessibleName(self.tr("Copy LAN address"))
        self.lan_copy_btn.setFixedSize(28, 28)

        self.source_title = StrongBodyLabel(self.tr("Source restrictions"), self)
        # 文案用「拒绝」而不是「允许」：直接对应原生 Block addon 的语义
        # （勾上 = block_global/block_private 为真 = 杀掉该类来源的连接），
        # 不用在脑子里做一次取反。
        self.block_global_check = CheckBox(
            self.tr("Reject connections from the public internet"), self
        )
        self.block_global_check.setChecked(block_global)
        self.block_private_check = CheckBox(
            self.tr("Reject connections from the LAN"), self
        )
        self.block_private_check.setChecked(block_private)
        self.source_hint = CaptionLabel(self)
        self.source_hint.setWordWrap(True)

        # —— 本地重定向通道 ——
        self.local_check = CheckBox(
            self.tr("Local redirect (zero-config, per-process)"), self
        )
        self.local_check.setChecked(use_local)

        self.local_spec_edit = LocalSpecSelector(self)
        self.local_spec_edit.set_tokens(split_spec(local_spec))
        self.local_spec_hint = CaptionLabel(self)
        self.local_spec_hint.setWordWrap(True)

        # —— WireGuard 通道 ——
        self.wireguard_check = CheckBox(
            self.tr("WireGuard tunnel (phones and other devices)"), self
        )
        self.wireguard_check.setChecked(use_wireguard)
        self.wireguard_hint = CaptionLabel(self)
        self.wireguard_hint.setWordWrap(True)
        self.wireguard_config_btn = TransparentToolButton(
            FluentIcon.SHARE, self
        )
        self.wireguard_config_btn.setToolTip(self.tr("View client configuration"))
        self.wireguard_config_btn.setAccessibleName(
            self.tr("View client configuration")
        )
        self.wireguard_config_btn.setFixedSize(28, 28)
        self.wireguard_config_btn.setVisible(self._wireguard_config is not None)

        self.restart_hint = CaptionLabel(
            self.tr("Changes apply immediately"), self
        )
        self.restart_hint.setVisible(is_running)

    def __init_layout(self):
        """初始化布局结构"""
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("Listen address"), self), self.host_combo)
        form.addRow(BodyLabel(self.tr("Port"), self), self.port_spin)

        lan_row = QHBoxLayout()
        lan_row.setSpacing(6)
        lan_row.addWidget(self.lan_value)
        lan_row.addWidget(self.lan_copy_btn)
        lan_row.addStretch(1)
        form.addRow(self.lan_label, lan_row)

        wg_row = QHBoxLayout()
        wg_row.setSpacing(6)
        wg_row.addWidget(self.wireguard_hint, 1)
        wg_row.addWidget(self.wireguard_config_btn)

        # 三个通道各是一段：启用勾选 + 参数。勾选与参数是兄弟行不嵌套 ——
        # 取消勾选只把参数区置灰，用户填了一半的内容不清掉。
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.system_proxy_check)
        layout.addLayout(form)
        layout.addWidget(self.local_hint)
        layout.addWidget(self.source_title)
        layout.addWidget(self.block_global_check)
        layout.addWidget(self.block_private_check)
        layout.addWidget(self.source_hint)
        layout.addWidget(self.local_check)
        layout.addWidget(self.local_spec_edit)
        layout.addWidget(self.local_spec_hint)
        layout.addWidget(self.wireguard_check)
        layout.addLayout(wg_row)
        layout.addWidget(self.restart_hint)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(440)

    def __connect_signal_to_slot(self):
        self.host_combo.currentIndexChanged.connect(self._sync_exposure)
        self.port_spin.valueChanged.connect(self._sync_exposure)
        self.lan_copy_btn.clicked.connect(self._copy_lan_address)
        self.local_check.toggled.connect(self._sync_exposure)
        self.wireguard_check.toggled.connect(self._sync_exposure)
        self.wireguard_config_btn.clicked.connect(self._show_wireguard_config)

    def get_use_system_proxy(self) -> bool:
        """「开始抓包」时是否挂系统代理。"""
        return self.system_proxy_check.isChecked()

    def get_use_local(self) -> bool:
        """是否启用本地重定向通道。"""
        return self.local_check.isChecked()

    def get_local_spec(self) -> str:
        """本地重定向的进程过滤串（原样返回，校验交给控制器）。"""
        return ",".join(self.local_spec_edit.tokens())

    def get_use_wireguard(self) -> bool:
        """是否启用 WireGuard 通道。"""
        return self.wireguard_check.isChecked()

    def _show_wireguard_config(self) -> None:
        if self._wireguard_config is None:
            return
        try:
            config = self._wireguard_config()
        except Exception as exc:  # noqa: BLE001
            show_warning(
                self.tr("WireGuard configuration unavailable"),
                str(exc),
                parent=self,
            )
            return
        dialog = WireGuardConfigDialog(config, self.window())
        dialog.exec()

    def get_port(self) -> int:
        """获取用户设置的端口号

        :return: 用户设置的端口号，例如 8080
        """
        return self.port_spin.value()

    def get_listen_host(self) -> str:
        """获取用户选择的绑定地址（`127.0.0.1` 或 `0.0.0.0`）。"""
        index = self.host_combo.currentIndex()
        return self._HOSTS[index] if 0 <= index < len(self._HOSTS) else LOOPBACK_HOST

    def get_block_global(self) -> bool:
        """是否拒绝公网来源。"""
        return self.block_global_check.isChecked()

    def get_block_private(self) -> bool:
        """是否拒绝局域网来源。"""
        return self.block_private_check.isChecked()

    def _sync_exposure(self):
        """按当前选择刷新提示文案与各参数区的可用性。"""
        exposed = self.get_listen_host() == ANY_HOST
        port = self.port_spin.value()
        wireguard_on = self.get_use_wireguard()
        local_on = self.get_use_local()

        self.local_hint.setText(
            self.tr(
                "This machine always connects via {}:{}; changing the listen "
                "address only affects whether other devices can reach it."
            ).format(LOOPBACK_HOST, port)
        )

        self.lan_label.setVisible(exposed)
        self.lan_value.setVisible(exposed)
        self.lan_copy_btn.setVisible(exposed)
        if exposed:
            self.lan_label.setText(self.tr("LAN address"))
            if self._lan_address:
                self.lan_value.setText(f"{self._lan_address}:{port}")
                self.lan_copy_btn.setEnabled(True)
            else:
                # 多网卡 / VPN 场景下探测可能失败。宁可说「未知」，也不要显示一个
                # Hyper-V 虚拟网卡的地址让用户白试半天。
                self.lan_value.setText(
                    self.tr("Not detected — check your system network settings")
                )
                self.lan_copy_btn.setEnabled(False)

        # 环回监听时两个开关都是空转：外部来源根本到不了 socket，而环回来源被
        # 原生 Block 无条件放行。置灰但保留勾选状态，切回局域网时用户的偏好还在。
        # wireguard 开着时 block_private 必须让路：隧道客户端全部来自 10.0.0.x，
        # 原生 Block 会把它们当「局域网来源」全杀（内核侧已自动豁免，这里同步置灰
        # 免得用户以为勾选生效了）。
        self.block_global_check.setEnabled(exposed)
        self.block_private_check.setEnabled(exposed and not wireguard_on)
        self.source_hint.setVisible(not exposed or wireguard_on)
        if not exposed:
            self.source_hint.setText(
                self.tr("No effect while listening on loopback only")
            )
        elif wireguard_on:
            self.source_hint.setText(
                self.tr(
                    "Reject-LAN is paused while the WireGuard tunnel is on: "
                    "tunnel clients connect from the 10.0.0.x network."
                )
            )

        self.local_spec_edit.setEnabled(local_on)
        self.local_spec_hint.setVisible(local_on)
        self.local_spec_hint.setText(
            self.tr(
                "Process names or PIDs, comma separated, prefix ! to exclude; "
                "Ferret itself is always excluded. Starting this channel may "
                "ask for administrator approval (UAC)."
            )
        )
        self.wireguard_hint.setText(
            self.tr(
                "Listens on UDP {}; copy the client configuration to your device "
                "after starting a capture."
            ).format(WIREGUARD_PORT)
        )
        self.wireguard_config_btn.setEnabled(wireguard_on)

    def _copy_lan_address(self):
        if not self._lan_address:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(f"{self._lan_address}:{self.port_spin.value()}")
        self.lan_copy_btn.setIcon(FluentIcon.ACCEPT)
        QTimer.singleShot(1200, lambda: self.lan_copy_btn.setIcon(FluentIcon.COPY))


class WireGuardConfigDialog(MessageBoxBase):
    """WireGuard 客户端配置预览：扫码导入 + 只读文本，确认键即复制。

    二维码直接从传入的配置文本派生（同一段内容两种呈现，永不各说一套）；
    扫不上码的人仍可手动复制。QR 矩阵不含静区，绘制时按规范补 4 模块。
    """

    QUIET_ZONE = 4
    """QR 规范静区（模块数）。"""

    def __init__(self, config: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("WireGuard client configuration"))

        self.desc_label = CaptionLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr(
                "Import this profile in the WireGuard app on your device; it "
                "routes all of that device's traffic through Ferret."
            )
        )

        self.config_edit = QPlainTextEdit(self)
        self.config_edit.setPlainText(config)
        self.config_edit.setReadOnly(True)
        self.config_edit.setFixedHeight(140)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        try:
            qr = self._render_qr(qr_matrix(config))
        except ValueError:
            # 文本超容量等编码失败不应挡住手动复制这条退路。
            qr = None
        if qr is not None:
            self.qr_label = QLabel(self)
            self.qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.qr_label.setPixmap(qr)
            layout.addWidget(self.qr_label)
        layout.addWidget(self.config_edit)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(460)

        # 预览框只有一个动作：复制后随手关闭。取消键没有存在的意义。
        self.yesButton.setText(self.tr("Copy"))
        self.cancelButton.hide()
        self.yesButton.clicked.connect(self._copy_config)

    def _render_qr(self, matrix: list[list[bool]]) -> QPixmap:
        """布尔矩阵 → 位图。3px/模块 + 4 模块静区：手机取景足够大、又不过分占屏。"""
        scale = 3
        quiet = self.QUIET_ZONE
        size = (len(matrix) + quiet * 2) * scale
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.white)
        painter = QPainter(pixmap)
        painter.setPen(Qt.PenStyle.NoPen)
        for y, row in enumerate(matrix):
            for x, dark in enumerate(row):
                if dark:
                    painter.fillRect(
                        (x + quiet) * scale,
                        (y + quiet) * scale,
                        scale,
                        scale,
                        Qt.GlobalColor.black,
                    )
        painter.end()
        return pixmap

    def _copy_config(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.config_edit.toPlainText())
