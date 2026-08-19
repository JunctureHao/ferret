from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
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
            endpoint=f"127.0.0.1:{self.controller.current_port}",
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
        """弹出端口设置对话框"""
        w = ProxyPortDialog(
            self.controller.current_port,
            self.window(),
            is_running=self.controller.is_capturing,
        )
        if w.exec():
            new_port = w.get_port()
            if new_port and new_port != self.controller.current_port:
                self.controller.update_port(new_port)
                self._ui_state = replace(
                    self._ui_state,
                    endpoint=f"127.0.0.1:{self.controller.current_port}",
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
    """代理端口设置对话框"""

    PORT_MIN = 1024
    PORT_MAX = 65535

    def __init__(
        self,
        current_port: int,
        parent: QWidget | None = None,
        *,
        is_running: bool = False,
    ):
        """初始化端口设置对话框

        Args:
            current_port: 当前端口号
            parent: 父组件
        """
        super().__init__(parent)
        self.__init_widget(current_port, is_running)
        self.__init_layout()

    def __init_widget(self, current_port: int, is_running: bool):
        """初始化界面组件

        :param int current_port: 当前端口号
        """
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("设置代理端口"))
        self.host_label = CaptionLabel(self.tr("监听地址：127.0.0.1"), self)
        self.port_spin = SpinBox(self)
        self.port_spin.setRange(self.PORT_MIN, self.PORT_MAX)
        self.port_spin.setValue(current_port)
        self.port_spin.setSingleStep(1)
        self.restart_hint = CaptionLabel(self.tr("修改后将重启代理"), self)
        self.restart_hint.setVisible(is_running)

    def __init_layout(self):
        """初始化布局结构"""
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.host_label)
        self.viewLayout.addWidget(self.port_spin)
        self.viewLayout.addWidget(self.restart_hint)
        self.widget.setMinimumWidth(350)

    def get_port(self) -> int:
        """获取用户设置的端口号

        :return: 用户设置的端口号，例如 8080
        """
        return self.port_spin.value()
