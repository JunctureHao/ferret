from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CardWidget,
    CheckBox,
    ComboBox,
    FluentIcon,
    HorizontalSeparator,
    InfoBadge,
    InfoBadgePosition,
    LineEdit,
    ListWidget,
    MessageBoxBase,
    PasswordLineEdit,
    RoundMenu,
    SmoothMode,
    SpinBox,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    VerticalSeparator,
    isDarkTheme,
)
from sysproxy import SystemProxyService

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.apps.common.filter import MultiFilterManager
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.common.icon import BaseIcon
from ferret.apps.common.info_bar import show_success, show_warning
from ferret.core.mitm import HTTPFlow
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
# 面板底色取 qfw 卡片灰阶（实测 dump ComboBox/菜单浅深底色），别用系统调色板。
_CARD_BG_LIGHT = "rgba(249, 249, 249, 1)"
_CARD_BG_DARK = "rgba(36, 36, 36, 1)"
_PICKER_ROW_HEIGHT = 33
_PICKER_MAX_ROWS = 8
_PICKER_FRAME_HEIGHT = 33

# 过滤面板的底色与边线：对齐 qfw 卡片灰阶（与上面 _CARD_BG_* 同源的实测 dump 值）。
# 背景用不透明实色避免与下层内容叠色；边线透明度与 qfw LineEdit 边框同档。
_PANEL_BG_LIGHT = "rgba(243, 243, 243, 1)"
_PANEL_BG_DARK = "rgba(45, 45, 45, 1)"
_PANEL_BORDER_LIGHT = "rgba(0, 0, 0, 0.09)"
_PANEL_BORDER_DARK = "rgba(255, 255, 255, 0.08)"


class CapturesInterface(QWidget):
    """抓包主界面 - 包含工具栏、搜索面板和内容区域"""

    # 右键「屏蔽此主机」向外转发，由 MainWindow 接到 BlockListController
    block_host_requested = Signal(str)
    # 右键「在 Compose 中编辑」向外转发（携带 flow id），由 MainWindow 提取并灌表单
    edit_in_compose_requested = Signal(str)

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
        # View 是 runtime.__init__ 里一次性创建、跨重启复用的持久对象，初始化期
        # 接一次源即可（比旧 master_ready 单次 emit 还早、还稳）。
        self.content.table.set_source(_CaptureFlowSource(self.controller))

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
        # 右键"在 Compose 中编辑"信号 → 冒泡给 MainWindow
        self.content.table.context_menu.edit_in_compose_requested.connect(
            self.edit_in_compose_requested
        )

        # Controller 状态信号 → UI 更新
        self.controller.capture_state_changed.connect(self.__on_capture_state_changed)
        self.controller.recordingChanged.connect(
            lambda _on: self._refresh_command_bar()
        )
        self.controller.channels_changed.connect(self._refresh_command_bar)
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
                self.tr("成功"),
                self.tr("已开始捕获系统流量"),
                parent=self,
            )
        elif capture_state == CaptureState.STOPPED and previous in (
            CaptureState.RUNNING,
            CaptureState.STOPPING,
        ):
            show_success(
                self.tr("成功"),
                self.tr("已停止捕获系统流量"),
                parent=self,
            )
        elif capture_state == CaptureState.FAILED:
            show_warning(
                self.tr("系统流量捕获失败"),
                self.controller.last_error or self.tr("请检查监听端口和系统代理设置"),
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
            use_reverse=self.controller.use_reverse,
            reverse_target=self.controller.reverse_target,
            reverse_port=self.controller.reverse_port,
            use_upstream=self.controller.use_upstream,
            upstream_target=self.controller.upstream_target,
            upstream_username=self.controller.upstream_username,
            upstream_password=self.controller.upstream_password,
            wireguard_config=self.controller.wireguard_client_config,
        )
        if not w.exec():
            return
        # 端口撞车前置（plans/reverse-mode.md §3）：把最常见的撞车在对话框侧
        # 拦下，给中文文案；内核拒绝→回滚（plan §7）仍是兜底。注意 reverse 与
        # regular 共用 listen_host，撞端口必炸；reverse_port 还可能与
        # WIREGUARD_PORT（UDP 侧）撞（reverse https 是 BOTH）。
        reverse_port = w.get_reverse_port()
        listen_port = w.get_port()
        if w.get_use_reverse() and reverse_port == listen_port:
            show_warning(
                self.tr("抓包设置未生效"),
                self.tr("反向代理端口 {} 与系统代理监听端口撞车，请换一个。").format(
                    reverse_port
                ),
                self.window(),
            )
            return
        if (
            w.get_use_reverse()
            and w.get_use_wireguard()
            and reverse_port == WIREGUARD_PORT
        ):
            show_warning(
                self.tr("抓包设置未生效"),
                self.tr(
                    "反向代理端口 {} 与 WireGuard UDP 51820 撞车，请换一个。"
                ).format(reverse_port),
                self.window(),
            )
            return
        # 上游代理前置校验。自环是硬拦：原生 `proxyserver.server_connect` 的自连
        # 守卫（proxyserver.py:376-395）虽有兜底，但它写的是 "Request destination
        # unknown"，且要等到有流量才出现 —— 提交时就该说清楚。
        upstream_on = w.get_use_upstream()
        upstream_target = w.get_upstream_target()
        if upstream_on and not upstream_target:
            show_warning(
                self.tr("抓包设置未生效"),
                self.tr("勾选了上游代理但没填地址，请填写或取消勾选。"),
                self.window(),
            )
            return
        if upstream_on:
            try:
                loops_back = self.controller.upstream_targets_self(
                    upstream_target,
                    listen_host=w.get_listen_host(),
                    listen_port=listen_port,
                )
            except ValueError as exc:
                show_warning(self.tr("抓包设置未生效"), str(exc), self.window())
                return
            if loops_back:
                show_warning(
                    self.tr("抓包设置未生效"),
                    self.tr(
                        "上游代理地址 {} 指回 ferret 自己的监听口，请换一个。"
                    ).format(upstream_target),
                    self.window(),
                )
                return
        # 凭证串台只警告不拦：这是原生 UpstreamAuth 分不开的行为（见
        # core/mitm/runtime.py::_upstream_auth），用户知情后仍可能就是要这么用。
        if upstream_on and w.get_upstream_username() and w.get_use_reverse():
            show_warning(
                self.tr("上游凭证会一并发给反代目标"),
                self.tr(
                    "内核对上游代理与反向代理用同一份凭证，反代目标也会收到认证头。"
                ),
                self.window(),
            )
        try:
            # 顺序有讲究：先提交通道（校验失败就整体中止，且抓包中重启端点前
            # 内核意图值必须先更新，否则重启会带上旧 spec），再提交端点/来源限制。
            self.controller.update_channels(
                use_system_proxy=w.get_use_system_proxy(),
                use_local=w.get_use_local(),
                local_spec=w.get_local_spec(),
                use_wireguard=w.get_use_wireguard(),
                use_reverse=w.get_use_reverse(),
                reverse_target=w.get_reverse_target(),
                reverse_port=reverse_port,
                use_upstream=upstream_on,
                upstream_target=upstream_target,
                upstream_username=w.get_upstream_username(),
                upstream_password=w.get_upstream_password(),
            )
            self.controller.update_proxy_settings(
                listen_host=w.get_listen_host(),
                listen_port=listen_port,
                block_global=w.get_block_global(),
                block_private=w.get_block_private(),
            )
        except (RuntimeError, ValueError) as exc:
            show_warning(self.tr("抓包设置未生效"), str(exc), self.window())
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
            parts.append(self.tr("系统代理"))
        if self.controller.use_local:
            spec = self.controller.local_spec
            label = self.tr("本地重定向")
            parts.append(f"{label} ({spec})" if spec else label)
        if self.controller.use_wireguard:
            parts.append(self.tr("WireGuard :{}").format(WIREGUARD_PORT))
        if self.controller.use_reverse:
            target = self.controller.reverse_target
            label = self.tr("反向代理 → {}").format(target or "—")
            parts.append(label)
        summary = " · ".join(parts)
        # 上游代理**不并进通道并集**：它换的是系统代理那条的出口，并进去会被读成
        # 第五条通道。另起一段跟在后面，空地址时首槽位仍是 regular，不显示。
        if self.controller.use_upstream and self.controller.upstream_target:
            egress = self.tr("出口 → {}").format(self.controller.upstream_target)
            return f"{summary} | {egress}" if summary else egress
        return summary

    def _channel_issue(self) -> str:
        errors = self.controller.channel_errors
        if not errors:
            return ""
        names = {
            "local": self.tr("本地重定向"),
            "wireguard": self.tr("WireGuard"),
            "reverse": self.tr("反向代理"),
        }
        return "; ".join(
            f"{names.get(key, key)}: {message}" for key, message in errors.items()
        )

    def stop_capture(self):
        """停止抓包（供外部调用，如MainWindow.closeEvent）"""
        self.controller.stop_capture()


class _CaptureFlowSource:
    """FlowSource 适配器：三个操作全部经 facade 投到 mitm 线程执行。

    `FlowTableModel` 只认 `FlowSource` 协议、不认识 facade —— 迭代（过滤后的
    可见列表，`visible_http_flows`）与 clear/remove 的线程安全由这里保证
    （AGENTS.md §3：Qt 线程不直连 View）。
    """

    def __init__(self, controller: CaptureController) -> None:
        self._controller = controller

    def __iter__(self) -> Iterator[HTTPFlow]:
        return iter(self._controller.visible_http_flows())

    def clear(self) -> None:
        self._controller.clear_flows()

    def remove(self, flows: Sequence[HTTPFlow]) -> None:
        self._controller.remove_flows(list(flows))


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
        # 重入守卫：setStyleSheet 本身会再触发 PaletteChange，没有它
        # changeEvent → _apply_theme → setStyleSheet → changeEvent …… 栈溢出。
        self._applying_theme = False
        self._apply_theme()

    def _apply_theme(self) -> None:
        dark = isDarkTheme()
        bg = _PANEL_BG_DARK if dark else _PANEL_BG_LIGHT
        border = _PANEL_BORDER_DARK if dark else _PANEL_BORDER_LIGHT
        self.setStyleSheet(
            f"#CaptureFilterPanel {{"
            f" background: {bg};"
            f" border-top: 1px solid {border};"
            f" border-bottom: 1px solid {border};"
            f"}}"
        )

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # qfw 切主题会发 PaletteChange，面板 QSS 不重算就会停在旧主题。
        # setStyleSheet 自身也派生 PaletteChange，重入时必须短路。
        if event.type() == QEvent.Type.PaletteChange and not self._applying_theme:
            self._applying_theme = True
            try:
                self._apply_theme()
            finally:
                self._applying_theme = False


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
        self.state_label = StrongBodyLabel(self.tr("未捕获系统流量"), self)

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

        self.stats_label = CaptionLabel(self.tr("{} 条").format(0), self)

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

        # 表是每次调用重建的局部变量，所以就地 tr() 没有「求值早于翻译器」的问题；
        # 反过来说，原先在使用点写 `self.tr(label)`（label 是变量）lupdate 一条都
        # 提不出来 —— 它只认字面量实参。
        state_ui = {
            CaptureState.STOPPED: (
                self.tr("未捕获系统流量"),
                "#8a8a8a",
                FluentIcon.PLAY,
                self.tr("开始抓包"),
                True,
            ),
            CaptureState.STARTING: (
                self.tr("启动中"),
                "#d99a00",
                FluentIcon.PLAY,
                self.tr("正在开启抓包会话"),
                False,
            ),
            CaptureState.RUNNING: (
                self.tr("正在捕获"),
                "#2e9b4d",
                FluentIcon.PAUSE,
                self.tr("停止抓包"),
                True,
            ),
            CaptureState.STOPPING: (
                self.tr("停止中"),
                "#d99a00",
                FluentIcon.PAUSE,
                self.tr("正在停止抓包会话"),
                False,
            ),
            CaptureState.FAILED: (
                self.tr("启动失败"),
                "#d13438",
                FluentIcon.PLAY,
                self.tr("重试抓包"),
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
        # 抓包会话开着时端点位置是通道摘要，它比端点地址更值得占宽度，不再压缩。
        base = self._state.channels_summary or self._state.endpoint
        if self._state.channel_issue:
            base = f"⚠ {base}"
        endpoint = (
            base
            if self._state.channels_summary
            else (f":{base.rsplit(':', 1)[-1]}" if compact else base)
        )
        self.endpoint_btn.setText(endpoint)
        if self._state.shown_count == self._state.total_count:
            stats = (
                str(self._state.total_count)
                if compact
                else self.tr("{} 条").format(self._state.total_count)
            )
        else:
            if compact:
                stats = f"{self._state.shown_count}/{self._state.total_count}"
            else:
                stats = self.tr("{} / {} 条").format(
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


class LocalSpecSelector(ListWidget):
    """本地重定向进程勾选列表：勾选 = 监听该应用，全不勾 = 全部。

    条目即运行中的用户程序（mitmproxy_rs 枚举，图标 + 名称）。
    勾选框由 qfw 原生 ListItemDelegate 按 CheckStateRole 绘制（条目带
    CheckStateRole 数据时才画框）；条目摘掉了 ItemIsUserCheckable，
    切换统一走 ``itemClicked`` → ``_toggle_item``，避免点勾选框区域时
    原生切换与 ``_toggle_item`` 双重翻转。
    展开逻辑（收起/展开与 ▾ 按钮）由对话框层的 local_row 驱动：
    ``is_expanded``/``toggle_expanded``/``set_expanded``。
    """

    tokensChanged = Signal(list)

    _ICON_SIDE = 16
    """进程图标统一边长：既保证各行图标一致，也让无图标行的占位与文字对齐。"""

    _full_height: int = _PICKER_ROW_HEIGHT * 6 + 4
    """满 6 行的期望高度：__init__ 按实测行重算，sizeHint 拿它报给布局。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._targets: list[LocalTarget] | None = None
        self._icons: dict[str, QIcon] = {}

        self.scrollDelegate.verticalSmoothScroll.setSmoothMode(SmoothMode.NO_SMOOTH)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIconSize(QSize(self._ICON_SIDE, self._ICON_SIDE))
        self.itemClicked.connect(self._toggle_item)
        self.itemChanged.connect(lambda _item: self.tokensChanged.emit(self.tokens()))

        color = (
            QColor(Qt.GlobalColor.white)
            if isDarkTheme()
            else QColor(Qt.GlobalColor.black)
        )
        for name in self._known_names():
            self.add_token(name, self._icon(name), color)

        # 行高 = 显式 SizeHint(33) + 委托上下 margin(qfw TableItemDelegate +4)。
        # 期望满 6 行、最少 2 行：窗口不够高时让布局合法压矮列表（内部滚动）。
        # 固定高度在这里不可用——空间不足那轮分配会把列表压到最小值以下，
        # QWidget::setGeometry 再按固定值钳回，后续兄弟件却按未钳回的位置摆，
        # 结果就是列表与下方文案叠画、被盖住的行连点击都被文案吃掉。
        row_h = (
            self.sizeHintForIndex(self.model().index(0, 0)).height()
            if self.count()
            else _PICKER_ROW_HEIGHT
        )
        self._full_height = row_h * 6 + 4
        self.setMinimumHeight(row_h * 2 + 4)
        self.setMaximumHeight(self._full_height)

    # —— 对外 API ——

    def is_expanded(self) -> bool:
        return self.isVisible()

    def set_expanded(self, expanded: bool) -> None:
        self.setVisible(expanded)

    def tokens(self) -> list[str]:
        return self._checked_labels()

    def add_token(
        self, label: str, icon: QIcon | None, color: QColor
    ) -> QListWidgetItem:
        item = QListWidgetItem(label, self)
        item.setIcon(icon if icon is not None else self._blank_icon())
        # 摘掉 ItemIsUserCheckable：勾选框仅作显示（delegate 按 CheckStateRole
        # 绘制），切换统一走 itemClicked→_toggle_item，避免点击勾选框区域时
        # 原生切换与 _toggle_item 双重翻转。
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        item.setForeground(color)
        item.setSizeHint(QSize(0, _PICKER_ROW_HEIGHT))
        return item

    def set_items_for_testing(self) -> None:
        """测试注入 ``_targets`` 后调用：清空重建候选条目。"""
        self.clear()
        color = (
            QColor(Qt.GlobalColor.white)
            if isDarkTheme()
            else QColor(Qt.GlobalColor.black)
        )
        for name in self._known_names():
            self.add_token(name, self._icon(name), color)

    def set_tokens(self, tokens: list[str]) -> None:
        """按过滤串回显：对上候选的点亮，对不上的手输 token 也各成一条。"""
        cleaned = [token.strip() for token in tokens if token.strip()]
        spec = ",".join(cleaned)
        color = (
            QColor(Qt.GlobalColor.white)
            if isDarkTheme()
            else QColor(Qt.GlobalColor.black)
        )
        for row in range(self.count()):
            item = self.item(row)
            target = self._target_by_name(item.text())
            item.setCheckState(
                Qt.CheckState.Checked
                if target and checked_tokens(spec, [target])
                else Qt.CheckState.Unchecked
            )
        matched = {
            label.lower() for label in checked_tokens(spec, self._ensure_targets())
        }
        for token in cleaned:
            if token.lower() not in matched:
                self.add_token(token, None, color).setCheckState(Qt.CheckState.Checked)

    # —— 内部 ——

    def sizeHint(self) -> QSize:
        """基类滚动区的默认 hint 不足 6 行，布局会照着它截；报满高。"""
        hint = super().sizeHint()
        hint.setHeight(self._full_height)
        return hint

    def _toggle_item(self, item: QListWidgetItem) -> None:
        item.setCheckState(
            Qt.CheckState.Unchecked
            if item.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )

    def _checked_labels(self) -> list[str]:
        return [
            self.item(row).text()
            for row in range(self.count())
            if self.item(row).checkState() == Qt.CheckState.Checked
        ]

    def _known_names(self) -> list[str]:
        """候选进程名（枚举失败降级为空列表：手输 token 仍可用）。"""
        try:
            return [target.display_name for target in self._ensure_targets()]
        except Exception:  # noqa: BLE001
            return []

    def _ensure_targets(self) -> list[LocalTarget]:
        if self._targets is None:
            self._targets = list_local_targets()
        return self._targets

    def _target_by_name(self, label: str) -> LocalTarget | None:
        for target in self._ensure_targets():
            if target.display_name == label:
                return target
        return None

    def _icon(self, name: str) -> QIcon:
        for target in self._ensure_targets():
            if target.display_name == name:
                cached = self._icons.get(name)
                if cached is None:
                    pixmap = QPixmap()
                    pixmap.loadFromData(target.icon_png or b"")
                    cached = QIcon(pixmap)
                    self._icons[name] = cached
                return cached
        return QIcon()

    def _blank_icon(self) -> QIcon:
        """无图标行的透明占位：让文字起点与有图标行对齐。"""
        cached = self._icons.get("")
        if cached is None:
            pixmap = QPixmap(self._ICON_SIDE, self._ICON_SIDE)
            pixmap.fill(Qt.GlobalColor.transparent)
            cached = QIcon(pixmap)
            self._icons[""] = cached
        return cached


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
        use_reverse: bool = False,
        reverse_target: str = "",
        reverse_port: int = 8081,
        use_upstream: bool = False,
        upstream_target: str = "",
        upstream_username: str = "",
        upstream_password: str = "",
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
            use_reverse: 反向代理通道是否启用（.plans/reverse-mode.md）
            reverse_target: 反向代理的目标 URL，如 https://example.com
            reverse_port: 反向代理的独立监听端口
            use_upstream: 系统代理通道是否经上游代理出口（不是第五条通道）
            upstream_target: 上游代理地址，如 http://proxy.corp:8080
            upstream_username: 上游代理的 Basic 用户名（留空 = 不发认证头）
            upstream_password: 上游代理的 Basic 密码
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
            use_reverse,
            reverse_target,
            reverse_port,
            use_upstream,
            upstream_target,
            upstream_username,
            upstream_password,
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
        use_reverse: bool,
        reverse_target: str,
        reverse_port: int,
        use_upstream: bool,
        upstream_target: str,
        upstream_username: str,
        upstream_password: str,
    ):
        """初始化界面组件"""
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("抓包通道"))

        # —— 系统代理通道 ——
        self.system_proxy_check = CheckBox(
            self.tr("系统代理（浏览器与多数桌面应用）"), self
        )
        self.system_proxy_check.setChecked(use_system_proxy)

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

        # 本机接入路径恒为环回，combo 选项文案已自解释，不再单列说明。

        self.lan_label = BodyLabel(self)
        self.lan_value = CaptionLabel(self)
        self.lan_copy_btn = TransparentToolButton(FluentIcon.COPY, self)
        self.lan_copy_btn.setToolTip(self.tr("复制局域网地址"))
        self.lan_copy_btn.setAccessibleName(self.tr("复制局域网地址"))
        self.lan_copy_btn.setFixedSize(28, 28)

        # 文案用「拒绝」而不是「允许」：直接对应原生 Block addon 的语义
        # （勾上 = block_global/block_private 为真 = 杀掉该类来源的连接），
        # 不用在脑子里做一次取反。
        self.block_global_check = CheckBox(self.tr("拒绝来自公网的连接"), self)
        self.block_global_check.setChecked(block_global)
        self.block_private_check = CheckBox(self.tr("拒绝来自局域网的连接"), self)
        self.block_private_check.setChecked(block_private)
        # 让路原因放在 tooltip：hint 文案只留结论（一行），细节悬停可见。
        self.block_private_check.setToolTip(
            self.tr(
                "WireGuard / 反向代理开启期间此项暂停生效：隧道客户端来自 10.0.0.x 网段。"
            )
        )
        self.source_hint = CaptionLabel(self)
        self.source_hint.setWordWrap(True)

        # —— 上游代理出口：**不是第五条通道**，而是把系统代理这条的出口从直连换成
        # 「先交给上游代理」（内核侧是 mode 首槽位替换，见 core/mitm/modes.py::
        # upstream_mode_spec）。所以它长在 Card ① 里、与监听地址端口同卡，而不是
        # 第五张卡片——独立成卡必被读成第五条抓包通道。
        self.upstream_separator = HorizontalSeparator(self)
        self.upstream_check = CheckBox(
            self.tr("经上游代理出口（企业代理 / 链式抓包）"), self
        )
        self.upstream_check.setChecked(use_upstream)
        self.upstream_target_edit = LineEdit(self)
        self.upstream_target_edit.setText(upstream_target)
        self.upstream_target_edit.setPlaceholderText(self.tr("http://proxy.corp:8080"))
        # 不放端口 SpinBox：端口在地址里，上游没有自己的监听口（spec 不带 @）。
        self.upstream_user_edit = LineEdit(self)
        self.upstream_user_edit.setText(upstream_username)
        self.upstream_user_edit.setPlaceholderText(self.tr("可选"))
        self.upstream_password_edit = PasswordLineEdit(self)
        self.upstream_password_edit.setText(upstream_password)
        self.upstream_password_edit.setPlaceholderText(self.tr("可选"))
        self.upstream_hint = CaptionLabel(self)
        self.upstream_hint.setWordWrap(True)

        # —— 本地重定向通道：勾选框 + 文字 + 折叠按钮同行 ——
        self.local_check = CheckBox(self.tr("本地重定向（零配置、按进程）"), self)
        self.local_check.setChecked(use_local)
        self.local_fold_btn = TransparentToolButton(FluentIcon.CHEVRON_RIGHT_MED, self)
        self.local_fold_btn.setFixedSize(28, 26)
        self.local_fold_btn.setToolTip(self.tr("选择进程"))
        self.local_fold_btn.clicked.connect(self._toggle_process_list)

        self.local_spec_edit = LocalSpecSelector(self)
        self.local_spec_edit.set_tokens(split_spec(local_spec))
        self.local_spec_hint = CaptionLabel(self)
        self.local_spec_hint.setWordWrap(True)

        # —— WireGuard 通道：勾选框 + 文字 + 二维码按钮同行 ——
        self.wireguard_check = CheckBox(self.tr("WireGuard 隧道（手机等设备）"), self)
        self.wireguard_check.setChecked(use_wireguard)
        self.wireguard_hint = CaptionLabel(self)
        self.wireguard_hint.setWordWrap(True)
        self.wireguard_config_btn = TransparentToolButton(FluentIcon.QRCODE, self)
        self.wireguard_config_btn.setToolTip(self.tr("查看客户端配置"))
        self.wireguard_config_btn.setAccessibleName(self.tr("查看客户端配置"))
        self.wireguard_config_btn.setFixedSize(28, 26)
        self.wireguard_config_btn.setVisible(self._wireguard_config is not None)

        # —— 反向代理通道（.plans/reverse-mode.md）：勾选框 + 目标 URL + 监听端口 ——
        self.reverse_check = CheckBox(
            self.tr("反向代理（把 ferret 架在目标服务前）"), self
        )
        self.reverse_check.setChecked(use_reverse)
        self.reverse_target_edit = LineEdit(self)
        self.reverse_target_edit.setText(reverse_target)
        self.reverse_target_edit.setPlaceholderText(self.tr("https://example.com"))
        self.reverse_port_spin = SpinBox(self)
        self.reverse_port_spin.setRange(self.PORT_MIN, self.PORT_MAX)
        self.reverse_port_spin.setValue(reverse_port)
        self.reverse_port_spin.setSingleStep(1)
        self.reverse_hint = CaptionLabel(self)
        self.reverse_hint.setWordWrap(True)
        # 提示明确说出两种访问方式：按域名（SNI → 目标证书）或按 IP（SNI 空
        # → 监听口本地证书），避免用户对「签目标证书」的过度承诺（§0）。
        self.reverse_hint.setText(
            self.tr("客户端需信任 ferret CA；按域名签目标证书，按 IP 直连签本机证书")
        )

        self.restart_hint = CaptionLabel(self.tr("更改立即生效"), self)
        self.restart_hint.setVisible(is_running)

    def __init_layout(self):
        """初始化布局结构：四个通道各一张卡片，视觉平级。"""
        # 标题行：重启提示挪到标题右侧，不再是底部一条常态说明。
        header_row = QHBoxLayout()
        header_row.setSpacing(6)
        header_row.addWidget(self.title_label)
        header_row.addStretch(1)
        header_row.addWidget(self.restart_hint, 0, Qt.AlignmentFlag.AlignVCenter)

        # —— Card ① 系统代理 ——
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

        # 两个来源限制开关同行，省一行标题。
        source_row = QHBoxLayout()
        source_row.setSpacing(12)
        source_row.addWidget(self.block_global_check)
        source_row.addWidget(self.block_private_check)
        source_row.addStretch(1)

        card_system = CardWidget(self)
        system_layout = QVBoxLayout(card_system)
        system_layout.setSpacing(8)
        system_layout.addWidget(self.system_proxy_check)
        system_layout.addLayout(form)
        system_layout.addLayout(source_row)
        system_layout.addWidget(self.source_hint)

        # 上游代理块：分隔线以下是「出口」，以上是「监听」，一张卡两件事不至于混读。
        # 整块随勾选显隐（同 reverse 的做法）：关掉时卡片回到原来的高度。
        self.upstream_target_row = QWidget(self)
        upstream_target_layout = QHBoxLayout(self.upstream_target_row)
        upstream_target_layout.setContentsMargins(0, 0, 0, 0)
        upstream_target_layout.setSpacing(6)
        upstream_target_layout.addWidget(
            BodyLabel(self.tr("代理地址"), self), 0, Qt.AlignmentFlag.AlignVCenter
        )
        upstream_target_layout.addWidget(self.upstream_target_edit, 1)

        self.upstream_cred_row = QWidget(self)
        upstream_cred_layout = QHBoxLayout(self.upstream_cred_row)
        upstream_cred_layout.setContentsMargins(0, 0, 0, 0)
        upstream_cred_layout.setSpacing(6)
        upstream_cred_layout.addWidget(
            BodyLabel(self.tr("用户名"), self), 0, Qt.AlignmentFlag.AlignVCenter
        )
        upstream_cred_layout.addWidget(self.upstream_user_edit, 1)
        upstream_cred_layout.addWidget(
            BodyLabel(self.tr("密码"), self), 0, Qt.AlignmentFlag.AlignVCenter
        )
        upstream_cred_layout.addWidget(self.upstream_password_edit, 1)

        system_layout.addWidget(self.upstream_separator)
        system_layout.addWidget(self.upstream_check)
        system_layout.addWidget(self.upstream_target_row)
        system_layout.addWidget(self.upstream_cred_row)
        system_layout.addWidget(self.upstream_hint)

        # —— Card ② 本地重定向 ——
        # 通道行：勾选框 + 文字同行，右缘放折叠按钮（▾）。折叠按钮常驻可见，
        # 不勾选通道也能提前展开挑选进程。
        local_row = QHBoxLayout()
        local_row.setSpacing(6)
        local_row.addWidget(self.local_check, 1)
        local_row.addWidget(self.local_fold_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        card_local = CardWidget(self)
        local_layout = QVBoxLayout(card_local)
        local_layout.setSpacing(8)
        local_layout.addLayout(local_row)
        local_layout.addWidget(self.local_spec_edit)
        local_layout.addWidget(self.local_spec_hint)

        # —— Card ③ WireGuard ——
        wireguard_row = QHBoxLayout()
        wireguard_row.setSpacing(6)
        wireguard_row.addWidget(self.wireguard_check, 1)
        wireguard_row.addWidget(
            self.wireguard_config_btn, 0, Qt.AlignmentFlag.AlignVCenter
        )

        card_wg = CardWidget(self)
        wg_layout = QVBoxLayout(card_wg)
        wg_layout.setSpacing(8)
        wg_layout.addLayout(wireguard_row)
        wg_layout.addWidget(self.wireguard_hint)

        # —— Card ④ 反向代理（.plans/reverse-mode.md §6 布局图）——
        # 把两个行包进 QWidget：reverse 通道未启用时整体隐藏，避免矮窗口下被
        # hint 行顶下来与上方 local_spec_hint 区域抢高度。
        self.reverse_target_row = QWidget(self)
        target_row_layout = QHBoxLayout(self.reverse_target_row)
        target_row_layout.setContentsMargins(0, 0, 0, 0)
        target_row_layout.setSpacing(6)
        target_row_layout.addWidget(
            BodyLabel(self.tr("目标 URL"), self), 0, Qt.AlignmentFlag.AlignVCenter
        )
        target_row_layout.addWidget(self.reverse_target_edit, 1)
        self.reverse_port_row = QWidget(self)
        port_row_layout = QHBoxLayout(self.reverse_port_row)
        port_row_layout.setContentsMargins(0, 0, 0, 0)
        port_row_layout.setSpacing(6)
        port_row_layout.addWidget(
            BodyLabel(self.tr("监听端口"), self), 0, Qt.AlignmentFlag.AlignVCenter
        )
        # 与目标 URL 输入框等长：吃满行宽，不再右留一段 stretch。
        port_row_layout.addWidget(self.reverse_port_spin, 1)

        card_reverse = CardWidget(self)
        reverse_layout = QVBoxLayout(card_reverse)
        reverse_layout.setSpacing(8)
        reverse_layout.addWidget(self.reverse_check)
        reverse_layout.addWidget(self.reverse_target_row)
        reverse_layout.addWidget(self.reverse_port_row)
        reverse_layout.addWidget(self.reverse_hint)

        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.addLayout(header_row)
        layout.addWidget(card_system)
        layout.addWidget(card_local)
        layout.addWidget(card_wg)
        layout.addWidget(card_reverse)
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(440)

    def __connect_signal_to_slot(self):
        self.host_combo.currentIndexChanged.connect(self._sync_exposure)
        self.port_spin.valueChanged.connect(self._sync_exposure)
        self.lan_copy_btn.clicked.connect(self._copy_lan_address)
        self.local_check.toggled.connect(self._sync_exposure)
        self.wireguard_check.toggled.connect(self._sync_exposure)
        self.wireguard_config_btn.clicked.connect(self._show_wireguard_config)
        # 反向代理通道：勾选 / 端口 / 目标都会影响参数可用性与撞车提示。
        self.reverse_check.toggled.connect(self._sync_exposure)
        self.reverse_port_spin.valueChanged.connect(self._sync_exposure)
        # 上游代理：勾选切显隐；用户名变化会切换「凭证串台」那条警告文案，
        # 所以它也要连（见 _sync_exposure 末段与 core/mitm/runtime.py::
        # _upstream_auth 的说明）。
        self.upstream_check.toggled.connect(self._sync_exposure)
        self.upstream_user_edit.textChanged.connect(self._sync_exposure)

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

    def get_use_reverse(self) -> bool:
        """是否启用反向代理通道（.plans/reverse-mode.md）。"""
        return self.reverse_check.isChecked()

    def get_reverse_target(self) -> str:
        """反向代理的目标 URL（含 http(s):// 前缀与端口），原样返回，校验交给控制器。"""
        return self.reverse_target_edit.text().strip()

    def get_reverse_port(self) -> int:
        """反向代理的独立监听端口。"""
        return self.reverse_port_spin.value()

    def get_use_upstream(self) -> bool:
        """系统代理通道是否经上游代理出口。"""
        return self.upstream_check.isChecked()

    def get_upstream_target(self) -> str:
        """上游代理地址（host[:port] 或 http(s)://host[:port]），校验交给调用方。"""
        return self.upstream_target_edit.text().strip()

    def get_upstream_username(self) -> str:
        """上游代理的 Basic 用户名；留空 = 不发认证头。"""
        return self.upstream_user_edit.text().strip()

    def get_upstream_password(self) -> str:
        """上游代理的 Basic 密码（**不 strip**：尾随空格可能就是密码的一部分）。"""
        return self.upstream_password_edit.text()

    def _show_wireguard_config(self) -> None:
        if self._wireguard_config is None:
            return
        try:
            config = self._wireguard_config()
        except Exception as exc:  # noqa: BLE001
            show_warning(
                self.tr("WireGuard 配置不可用"),
                str(exc),
                parent=self,
            )
            return
        dialog = WireGuardConfigDialog(config, self.window())
        dialog.exec()

    def _toggle_process_list(self) -> None:
        expanded = not self.local_spec_edit.isVisible()
        self.local_spec_edit.setVisible(expanded)
        self.local_fold_btn.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
        )

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
        reverse_on = self.get_use_reverse()
        # reverse 启用时「绑定非环回」才会触发 block_private 让路（与内核
        # runtime._effective_block_private 同式：reverse_yield = use_reverse and
        # listen_host == ANY_HOST，plans/reverse-mode.md §4）。reverse 绑环回
        # 时让路条件不成立，block_private 走原值，与本地客户端无关。
        reverse_yield = reverse_on and exposed

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
        # wireguard 开着时 block_private 必须让路：隧道客户端全部来自 10.0.0.x，
        # 原生 Block 会把它们当「局域网来源」全杀（内核侧已自动豁免，这里同步置灰
        # 免得用户以为勾选生效了）；reverse 开启且绑非环回时同理。
        self.block_global_check.setEnabled(exposed)
        self.block_private_check.setEnabled(
            exposed and not wireguard_on and not reverse_yield
        )
        self.source_hint.setVisible(not exposed or wireguard_on or reverse_yield)
        if not exposed:
            self.source_hint.setText(self.tr("仅本机监听，不生效"))
        elif wireguard_on:
            self.source_hint.setText(
                self.tr("WireGuard 开启期间「拒绝局域网」暂停生效")
            )
        elif reverse_yield:
            self.source_hint.setText(self.tr("反向代理开启期间「拒绝局域网」暂停生效"))

        # 进程列表只看展开态（不勾选也能提前展开挑选进程）；提示仍随勾选显隐。
        self.local_spec_edit.setVisible(self.local_spec_edit.is_expanded())
        self.local_spec_hint.setVisible(local_on)
        self.local_spec_hint.setText(
            self.tr("留空即截获全部进程，启用时可能请求管理员授权")
        )
        self.wireguard_hint.setVisible(wireguard_on)
        self.wireguard_hint.setText(
            self.tr("监听 UDP {}，点右侧二维码导入客户端").format(WIREGUARD_PORT)
        )
        self.wireguard_config_btn.setEnabled(wireguard_on)

        # 反向代理：勾选未启用时参数区置灰。目标 URL 与端口始终跟随勾选状态，
        # 端口撞车提示只在启用时才有意义（§3）。wireguard 端口（51820/UDP）
        # 与 reverse_port 撞车同样在启用时校验，避免 reverse https 的 UDP 侧
        # 与 wireguard 撞地址。
        self.reverse_check.setChecked(reverse_on)
        self.reverse_target_edit.setEnabled(reverse_on)
        self.reverse_port_spin.setEnabled(reverse_on)
        # 通道未启用时整组子参数 + hint 一起隐掉：避免矮窗口下 reverse 行
        # 顶高整体高度、与 local_spec_hint 区抢空间（test_short_window_shrinks）。
        self.reverse_target_row.setVisible(reverse_on)
        self.reverse_port_row.setVisible(reverse_on)
        self.reverse_hint.setVisible(reverse_on)
        if reverse_on:
            reverse_port = self.reverse_port_spin.value()
            if reverse_port == port:
                self.reverse_hint.setText(
                    self.tr(
                        "端口 {} 与系统代理监听端口撞车，提交后内核会拒（已配置查重兜底）。"
                    ).format(reverse_port)
                )
            elif reverse_port == WIREGUARD_PORT and wireguard_on:
                self.reverse_hint.setText(
                    self.tr(
                        "端口 {} 与 WireGuard UDP 51820 撞车，提交后内核会拒。"
                    ).format(reverse_port)
                )
            else:
                self.reverse_hint.setText(
                    self.tr(
                        "客户端需信任 ferret CA；按域名签目标证书，按 IP 直连签本机证书"
                    )
                )

        # 上游代理：整块随勾选显隐（同 reverse），hint 复用同一行位置轮播两条文案。
        upstream_on = self.get_use_upstream()
        self.upstream_separator.setVisible(upstream_on)
        self.upstream_target_row.setVisible(upstream_on)
        self.upstream_cred_row.setVisible(upstream_on)
        self.upstream_hint.setVisible(upstream_on)
        if upstream_on:
            if reverse_on and self.get_upstream_username():
                # 原生 UpstreamAuth 的模式闸门放行 upstream **和 reverse**
                # （upstream_auth.py:40-58），两者同开时反代目标也会收到这份
                # Authorization 头。分不开，只能明说 —— 详见 runtime._upstream_auth。
                self.upstream_hint.setText(
                    self.tr(
                        "注意：反向代理同时开启时，上游凭证也会发给反代目标（内核限制）"
                    )
                )
            else:
                self.upstream_hint.setText(
                    self.tr(
                        "仅系统代理通道经上游出口；本地重定向 / WireGuard / 反向代理仍为直连"
                    )
                )

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
        self.title_label.setText(self.tr("WireGuard 客户端配置"))

        self.desc_label = CaptionLabel(self)
        self.desc_label.setWordWrap(True)
        self.desc_label.setText(
            self.tr(
                "在设备的 WireGuard 应用中导入此配置；该设备的全部流量将经由 Ferret。"
            )
        )

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
        self.viewLayout.addLayout(layout)
        self.widget.setMinimumWidth(460)

        self.hideYesButton()

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
