from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSizePolicy,
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
    MessageBoxBase,
    RoundMenu,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    VerticalSeparator,
)

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.apps.common.filter import MultiFilterManager
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.common.icon import BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.core.mitm.facade import MitmFacade
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, PORT_MAX, PORT_MIN
from ferret.core.system_proxy import SystemProxyService

if TYPE_CHECKING:
    from ferret.apps.window import MainWindow


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
            show_success("成功", "已开始捕获系统流量", parent=self)
        elif capture_state == CaptureState.STOPPED and previous in (
            CaptureState.RUNNING,
            CaptureState.STOPPING,
        ):
            show_success("成功", "已停止捕获系统流量", parent=self)
        elif capture_state == CaptureState.FAILED:
            show_warning(
                "系统流量捕获失败",
                self.controller.last_error or "请检查监听端口和系统代理设置",
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
        """弹出代理监听设置对话框"""
        w = ProxyPortDialog(
            self.controller.current_port,
            self.window(),
            is_running=self.controller.is_capturing,
            listen_host=self.controller.current_host,
            block_global=self.controller.block_global,
            block_private=self.controller.block_private,
            lan_address=self.controller.lan_address(),
        )
        if not w.exec():
            return
        try:
            # 一次提交四项：控制器自己判断哪些真的变了、哪些需要重启内核。
            self.controller.update_proxy_settings(
                listen_host=w.get_listen_host(),
                listen_port=w.get_port(),
                block_global=w.get_block_global(),
                block_private=w.get_block_private(),
            )
        except (RuntimeError, ValueError) as exc:
            show_warning(self.tr("代理设置未生效"), str(exc), self.window())
            return
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
            self.tr("选择 .flow 文件回放"),
            "",
            self.tr("Flow 文件 (*.flow)"),
        )
        if not path:
            return
        try:
            self.controller.load_replay_file(Path(path))
        except Exception as exc:  # noqa: BLE001
            show_warning(self.tr("回放失败"), str(exc), parent=self)

    @Slot()
    def __on_open_flow_file_requested(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window(),
            self.tr("加载 Flow 到当前列表"),
            "",
            self.tr("Flow 文件 (*.flow)"),
        )
        if not path:
            return
        try:
            count = self.controller.load_flow_file(Path(path))
            show_success(
                self.tr("加载完成"),
                self.tr("已加载 {} 条").format(count),
                parent=self,
            )
        except Exception as exc:  # noqa: BLE001
            show_warning(self.tr("加载失败"), str(exc), parent=self)

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
        self.command_bar.set_state(self._ui_state, self.filter_panel.isVisible())
        self.content.set_capture_context(
            capture_state=self._ui_state.capture_state,
            endpoint=self._ui_state.endpoint,
            total_count=self._ui_state.total_count,
            shown_count=self._ui_state.shown_count,
            active_filter_count=self._ui_state.active_filter_count,
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
        self.state_label = StrongBodyLabel(self.tr("已停止"), self)

        self.endpoint_label = BodyLabel(self)
        self.endpoint_label.setFixedHeight(28)
        self.endpoint_label.setAccessibleName(self.tr("代理监听地址"))
        self.endpoint_label.setFont(self.font())
        # Compatibility alias for callers that read the endpoint text/visibility.
        self.endpoint_btn = self.endpoint_label

        # 放开到局域网是个有安全含义的状态，必须常驻可见，不能只藏在设置对话框里。
        self.exposure_label = CaptionLabel(self.tr("局域网"), self)
        self.exposure_label.setFixedHeight(28)
        self.exposure_label.setAccessibleName(self.tr("局域网设备可连接"))
        self.exposure_label.setStyleSheet("color: #c07000;")
        self.exposure_label.setVisible(False)

        self.stats_label = CaptionLabel("0 条", self)

        self.search_btn = TransparentToolButton(FluentIcon.SEARCH, self)
        self.search_btn.setCheckable(True)
        self.search_btn.setToolTip(self.tr("高级搜索") + " (Ctrl+F)")
        self.search_btn.setAccessibleName(self.tr("高级搜索"))
        self.open_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.open_btn.setToolTip(self.tr("加载 Flow 到当前列表"))
        self.open_btn.setAccessibleName(self.tr("加载 Flow 到当前列表"))
        self.filter_badge = InfoBadge.attension(
            0, self, self.search_btn, InfoBadgePosition.TOP_RIGHT
        )
        self.filter_badge.hide()

        self.proxy_setting_btn = TransparentToolButton(FluentIcon.GLOBE, self)
        self.proxy_setting_btn.setToolTip(self.tr("端口设置"))
        self.proxy_setting_btn.setAccessibleName(self.tr("端口设置"))

        self.environment_btn = TransparentToolButton(FluentIcon.MORE, self)
        self.environment_btn.setToolTip(self.tr("环境设置"))
        self.environment_btn.setAccessibleName(self.tr("环境设置"))
        self.environment_btn.hide()

        self.locate_selection_btn = TransparentToolButton(
            BaseIcon.LOCATION_TARGET, self
        )
        self.locate_selection_btn.setToolTip(self.tr("定位选中"))
        self.locate_selection_btn.setAccessibleName(self.tr("定位选中"))

        self.control_btn = TransparentToolButton(FluentIcon.PLAY, self)
        self.control_btn.setToolTip(self.tr("开始捕获系统流量"))
        self.control_btn.setAccessibleName(self.tr("开始捕获系统流量"))

        self.captures_delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.captures_delete_btn.setToolTip(self.tr("清空当前流量"))
        self.captures_delete_btn.setAccessibleName(self.tr("清空当前流量"))

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
        port_action = Action(FluentIcon.GLOBE, self.tr("端口设置"), menu)
        port_action.triggered.connect(self.portRequested.emit)
        menu.addAction(port_action)
        menu.exec(
            self.environment_btn.mapToGlobal(QPoint(0, self.environment_btn.height()))
        )

    def set_state(self, state: CaptureUiState, filter_panel_visible: bool) -> None:
        self._state = state
        self._filter_panel_visible = filter_panel_visible

        state_ui = {
            CaptureState.STOPPED: (
                "未捕获系统流量",
                "#8a8a8a",
                FluentIcon.PLAY,
                "开始捕获系统流量",
                True,
            ),
            CaptureState.STARTING: (
                "启动中",
                "#d99a00",
                FluentIcon.PLAY,
                "正在接入系统代理",
                False,
            ),
            CaptureState.RUNNING: (
                "正在捕获",
                "#2e9b4d",
                FluentIcon.PAUSE,
                "停止捕获系统流量",
                True,
            ),
            CaptureState.STOPPING: (
                "停止中",
                "#d99a00",
                FluentIcon.PAUSE,
                "正在恢复系统代理",
                False,
            ),
            CaptureState.FAILED: (
                "启动失败",
                "#d13438",
                FluentIcon.PLAY,
                "重试捕获系统流量",
                True,
            ),
        }
        label, color, icon, tooltip, enabled = state_ui[state.capture_state]
        self.state_label.setText(self.tr(label))
        self.state_dot.setStyleSheet(f"color: {color};")
        self.state_dot.setAccessibleName(self.tr(label))
        self.control_btn.setIcon(icon)
        self.control_btn.setEnabled(enabled)
        self.control_btn.setToolTip(self.tr(tooltip))
        self.control_btn.setAccessibleName(self.tr(tooltip))

        self.endpoint_btn.setText(state.endpoint)
        self.endpoint_btn.setToolTip(
            self.tr("本机通过 {} 接入；局域网设备也可连接").format(state.endpoint)
            if state.lan_exposed
            else self.tr("本机通过 {} 接入").format(state.endpoint)
        )
        if state.shown_count == state.total_count:
            stats_text = self.tr("{} 条").format(state.total_count)
        else:
            stats_text = self.tr("{} / {} 条").format(
                state.shown_count, state.total_count
            )
        self.stats_label.setText(stats_text)
        self.stats_label.setToolTip(
            self.tr("共 {} 条，当前显示 {} 条，已选 {} 条").format(
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
        endpoint = self._state.endpoint
        self.endpoint_btn.setText(
            f":{endpoint.rsplit(':', 1)[-1]}" if compact else endpoint
        )
        if self._state.shown_count == self._state.total_count:
            stats = (
                str(self._state.total_count)
                if compact
                else f"{self._state.total_count} 条"
            )
        else:
            if compact:
                stats = f"{self._state.shown_count}/{self._state.total_count}"
            else:
                stats = f"{self._state.shown_count} / {self._state.total_count} 条"
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
            self.tr("清空当前 {} 条流量？").format(flow_count), self
        )
        self.desc_label = BodyLabel(
            self.tr("此操作无法撤销，但不会删除已保存的会话。"), self
        )
        self.desc_label.setWordWrap(True)
        self.yesButton.setText(self.tr("清空"))
        self.cancelButton.setText(self.tr("取消"))
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addWidget(self.desc_label)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(380)


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
        """
        super().__init__(parent)
        self._lan_address = lan_address
        self.__init_widget(
            current_port, is_running, listen_host, block_global, block_private
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
    ):
        """初始化界面组件"""
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("设置代理监听"))

        self.host_combo = ComboBox(self)
        self.host_combo.addItems(
            [
                self.tr("仅本机（{}）").format(LOOPBACK_HOST),
                self.tr("局域网可访问（{}）").format(ANY_HOST),
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
        self.lan_copy_btn.setToolTip(self.tr("复制局域网地址"))
        self.lan_copy_btn.setAccessibleName(self.tr("复制局域网地址"))
        self.lan_copy_btn.setFixedSize(28, 28)

        self.source_title = StrongBodyLabel(self.tr("来源限制"), self)
        # 文案用「拒绝」而不是「允许」：直接对应原生 Block addon 的语义
        # （勾上 = block_global/block_private 为真 = 杀掉该类来源的连接），
        # 不用在脑子里做一次取反。
        self.block_global_check = CheckBox(self.tr("拒绝来自公网的连接"), self)
        self.block_global_check.setChecked(block_global)
        self.block_private_check = CheckBox(self.tr("拒绝来自局域网的连接"), self)
        self.block_private_check.setChecked(block_private)
        self.source_hint = CaptionLabel(self)
        self.source_hint.setWordWrap(True)

        self.restart_hint = CaptionLabel(self.tr("修改后将重启代理"), self)
        self.restart_hint.setVisible(is_running)

    def __init_layout(self):
        """初始化布局结构"""
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow(BodyLabel(self.tr("监听地址"), self), self.host_combo)
        form.addRow(BodyLabel(self.tr("端口"), self), self.port_spin)

        lan_row = QHBoxLayout()
        lan_row.setSpacing(6)
        lan_row.addWidget(self.lan_value)
        lan_row.addWidget(self.lan_copy_btn)
        lan_row.addStretch(1)
        form.addRow(self.lan_label, lan_row)

        # 两个开关与「监听地址」是兄弟关系，不做嵌套：它们各自独立生效，
        # 视觉上嵌进地址下面会暗示「只有选某个地址才存在」，那是错的。
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.title_label)
        layout.addLayout(form)
        layout.addWidget(self.local_hint)
        layout.addWidget(self.source_title)
        layout.addWidget(self.block_global_check)
        layout.addWidget(self.block_private_check)
        layout.addWidget(self.source_hint)
        layout.addWidget(self.restart_hint)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(400)

    def __connect_signal_to_slot(self):
        self.host_combo.currentIndexChanged.connect(self._sync_exposure)
        self.port_spin.valueChanged.connect(self._sync_exposure)
        self.lan_copy_btn.clicked.connect(self._copy_lan_address)

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
        """按当前选择刷新提示文案与来源限制的可用性。"""
        exposed = self.get_listen_host() == ANY_HOST
        port = self.port_spin.value()

        self.local_hint.setText(
            self.tr(
                "本机始终通过 {}:{} 接入，切换监听地址只影响别的设备能否连进来。"
            ).format(LOOPBACK_HOST, port)
        )

        self.lan_label.setVisible(exposed)
        self.lan_value.setVisible(exposed)
        self.lan_copy_btn.setVisible(exposed)
        if exposed:
            self.lan_label.setText(self.tr("局域网地址"))
            if self._lan_address:
                self.lan_value.setText(f"{self._lan_address}:{port}")
                self.lan_copy_btn.setEnabled(True)
            else:
                # 多网卡 / VPN 场景下探测可能失败。宁可说「未知」，也不要显示一个
                # Hyper-V 虚拟网卡的地址让用户白试半天。
                self.lan_value.setText(self.tr("未能识别，请在系统网络设置中查看"))
                self.lan_copy_btn.setEnabled(False)

        # 环回监听时两个开关都是空转：外部来源根本到不了 socket，而环回来源被
        # 原生 Block 无条件放行。置灰但保留勾选状态，切回局域网时用户的偏好还在。
        self.block_global_check.setEnabled(exposed)
        self.block_private_check.setEnabled(exposed)
        self.source_hint.setVisible(not exposed)
        if not exposed:
            self.source_hint.setText(self.tr("仅本机监听时不生效"))

    def _copy_lan_address(self):
        if not self._lan_address:
            return
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(f"{self._lan_address}:{self.port_spin.value()}")
        self.lan_copy_btn.setIcon(FluentIcon.ACCEPT)
        QTimer.singleShot(1200, lambda: self.lan_copy_btn.setIcon(FluentIcon.COPY))
