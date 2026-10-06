from __future__ import annotations

from typing import cast

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from qfluentwidgets import (
    CheckableSystemTrayMenu,
    FluentIcon,
    FluentTitleBar,
    FluentTitleBarButton,
    FluentWindow,
    NavigationItemPosition,
    ToolTipFilter,
    ToolTipPosition,
    qconfig,
    setTheme,
)

from ferret.apps.capture.controllers import CaptureState
from ferret.apps.capture.views import CapturesInterface
from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.views import CertificateInterface
from ferret.apps.common.icon import BaseAction, BaseIcon
from ferret.apps.common.info_bar import show_warning
from ferret.apps.common.search import SearchablePage, SearchHost, is_searchable
from ferret.apps.common.window import center_window
from ferret.apps.compose.controllers import ComposeController
from ferret.apps.compose.views import ComposeInterface
from ferret.apps.gateway.controllers import GatewayController
from ferret.apps.gateway.views import GatewayInterface
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.views import InterceptInterface
from ferret.apps.intercept.window import InterceptWindow
from ferret.apps.mock.controllers import MockController
from ferret.apps.mock.views import MockInterface
from ferret.apps.rewrite.controllers import RewriteController
from ferret.apps.rewrite.views import RewriteInterface
from ferret.apps.scripts.controllers import ScriptsController
from ferret.apps.scripts.views import ScriptsInterface
from ferret.apps.session.controllers import SessionController
from ferret.apps.session.views import SessionsInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import APP_NAME, CONFIG


class MainWindow(FluentWindow):
    def __init__(self, runtime: ApplicationRuntime | None = None):
        super().__init__()
        self._shutdown_complete = False
        self.runtime = runtime or ApplicationRuntime(self)
        self._owns_runtime = runtime is None

        self.session_controller = SessionController(self)
        self.settings_interface = SettingsInterface(self, mitm=self.runtime.mitm)
        # titlebar 搜索宿主先于各页创建：捕获页错误面板「清除表达式」与脚本页
        # clear_search 都经它注入（规格 §5.3 v4 / §6）。
        self.search_host = SearchHost(self)
        self.captures_interface = CapturesInterface(
            self,
            mitm=self.runtime.mitm,
            system_proxy=self.runtime.system_proxy,
            search_host=self.search_host,
        )
        self.sessions_interface = SessionsInterface(
            controller=self.session_controller, parent=self
        )
        # 规则控制器都建在 runtime.start() 之前：构造时就把已存规则交给 facade，
        # Master 起来时 _run_master 会在服务第一个请求前下发（脚本清单同理）。
        self.gateway_controller = GatewayController(self, mitm=self.runtime.mitm)
        self.gateway_interface = GatewayInterface(
            controller=self.gateway_controller, parent=self
        )
        self.rewrite_controller = RewriteController(self, mitm=self.runtime.mitm)
        self.rewrite_interface = RewriteInterface(
            controller=self.rewrite_controller, parent=self
        )
        self.mock_controller = MockController(self, mitm=self.runtime.mitm)
        self.mock_interface = MockInterface(
            controller=self.mock_controller, parent=self
        )
        self.intercept_controller = InterceptController(self, mitm=self.runtime.mitm)
        self.intercept_interface = InterceptInterface(
            controller=self.intercept_controller, parent=self
        )
        self.compose_controller = ComposeController(self, mitm=self.runtime.mitm)
        self.compose_interface = ComposeInterface(
            controller=self.compose_controller, parent=self
        )
        self.scripts_controller = ScriptsController(self, mitm=self.runtime.mitm)
        self.scripts_interface = ScriptsInterface(
            controller=self.scripts_controller,
            search_host=self.search_host,
            parent=self,
        )
        # 断点窗口是独立顶层窗口，构造时不能给 Qt 父对象（`qframelesswindow` 的
        # `updateFrameless()` 不补 `Qt.Window`，给了父对象就退化成子控件），所以它的
        # 生命周期就靠这个属性持着 —— 丢了引用窗口会被 GC 掉。
        self.intercept_window = InterceptWindow(self.intercept_controller)
        self.certificate_controller = CertificateController(
            self, mitm=self.runtime.mitm
        )
        self.certificate_interface = CertificateInterface(
            controller=self.certificate_controller, parent=self
        )

        self.tray_icon = SystemTray(self)
        self.pin_button = PinButton(self)

        self.__init_window()
        if self._owns_runtime:
            self.runtime.start()

    def __init_window(self):
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(":/icon"))
        self.setObjectName("Main")
        self.resize(960, 780)
        self.setMinimumSize(960, 780)

        self.titleBar: FluentTitleBar = self.titleBar
        self.titleBar.buttonLayout.insertWidget(0, self.pin_button)

        # titlebar 搜索槽（规格 §4.1 v3）：唯一全局框，居中悬浮于标题与按钮簇之间
        # （两侧等权 stretch，2026-09-27 用户决议）；捕获页经协议注入独特动作，
        # 不再换控件。
        hbl = self.titleBar.hBoxLayout
        hbl.insertWidget(hbl.count() - 1, self.search_host, 0, Qt.AlignVCenter)  # ty: ignore[unresolved-attribute]
        hbl.insertStretch(hbl.count() - 1, 1)
        self.navigationInterface.setExpandWidth(260)
        center_window(self)
        self.__init_navigation()
        self.__connect_signal_to_slot()
        # 初始路由一次（启动页 = 捕获页 → titlebar 亮出表达式编辑器，规格 §4.3）。
        self.__on_page_changed(self.stackedWidget.currentIndex())
        # 启动自动检查更新（docs/design.md#update）：延迟避开启动高峰；
        # 开关与形态闸门都收在 check_updates_auto 内部，这里只管定时触发。
        QTimer.singleShot(5000, self.settings_interface.check_updates_auto)

    def __init_navigation(self):
        self.addSubInterface(self.captures_interface, FluentIcon.WIFI, self.tr("捕获"))

        self.addSubInterface(
            self.sessions_interface, FluentIcon.HISTORY, self.tr("会话")
        )

        self.addSubInterface(self.gateway_interface, FluentIcon.VPN, self.tr("网关"))

        self.addSubInterface(
            self.rewrite_interface, FluentIcon.PENCIL_INK, self.tr("重写")
        )

        self.addSubInterface(self.mock_interface, FluentIcon.ROBOT, self.tr("Mock"))

        self.addSubInterface(self.intercept_interface, BaseIcon.BUG, self.tr("断点"))

        self.addSubInterface(self.scripts_interface, FluentIcon.CODE, self.tr("脚本"))

        self.addSubInterface(
            self.compose_interface, FluentIcon.SEND, self.tr("请求编辑")
        )

        self.addSubInterface(
            self.certificate_interface,
            FluentIcon.CERTIFICATE,
            self.tr("证书"),
            NavigationItemPosition.BOTTOM,
        )

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            self.tr("设置"),
            NavigationItemPosition.BOTTOM,
        )

    def __connect_signal_to_slot(self):
        self.settings_interface.update_restart_requested.connect(self._apply_update)
        self.runtime.startup_error.connect(self._show_startup_error)
        qconfig.themeChanged.connect(lambda theme: setTheme(theme))
        self.pin_button.clicked.connect(self.toggleStayOnTop)
        self.tray_icon.activated.connect(self.__on_activated)
        # titlebar 搜索路由（规格 §4.3）：切页三分支 + 键入转发 + Esc 归还焦点。
        self.stackedWidget.currentChanged.connect(self.__on_page_changed)
        self.search_host.search_requested.connect(self.__on_titlebar_search_changed)
        self.search_host.escape_pressed.connect(self.__return_search_focus)
        # 捕获页非法表达式 → 全局框 tooltip（§5.1 v3：框归 SearchHost，主窗口牵线）。
        self.captures_interface.controller.filterExpressionRejected.connect(
            self.search_host.edit.setToolTip
        )
        # 全局 Ctrl+F（§4.4）：六页页内同名快捷键已随迁移删除，无双触发。
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            self.search_host.focus_current
        )
        self.captures_interface.controller.capture_state_changed.connect(
            self.__on_capture_state_changed
        )
        # 由主窗口牵线，apps/capture 不必认识 apps/gateway。
        self.captures_interface.block_host_requested.connect(
            self.gateway_controller.add_host_rule
        )
        # 同上：apps/capture 不必认识 apps/compose。提取在 mitm 线程上跑
        # （facade.request_edit），prefill 与切页都在主线程。
        self.captures_interface.edit_in_compose_requested.connect(
            self.__edit_in_compose
        )
        # 同上：apps/capture 不必认识 apps/mock。加入池的动作在 mitm 线程做副本
        # （facade.add_mock_flows），id 列表冒泡进来直接落控制器。
        self.captures_interface.add_to_mock_requested.connect(
            self.mock_controller.add_from_selection
        )
        # 同上：断点页和断点窗口互不认识，两个方向都从这里接。
        self.intercept_interface.queue_requested.connect(self.intercept_window.pop_up)
        self.intercept_window.attention_requested.connect(self.__on_intercept_attention)

    def __on_page_changed(self, index: int) -> None:
        """titlebar 搜索槽路由（规格 §4.3 v3）：协议页显示 + 动作注入 / 其余隐藏。"""
        page = self.stackedWidget.widget(index)
        if is_searchable(page):
            searchable = cast(SearchablePage, page)
            actions_hook = getattr(page, "search_actions", None)
            self.search_host.set_actions(
                actions_hook() if callable(actions_hook) else []
            )
            self.search_host.show_box(
                searchable.search_placeholder(), searchable.current_search_text()
            )
        else:
            self.search_host.hide_all()

    def __on_titlebar_search_changed(self, text: str) -> None:
        page = self.stackedWidget.currentWidget()
        if is_searchable(page):
            cast(SearchablePage, page).apply_search(text)

    def __return_search_focus(self) -> None:
        """Esc 后焦点归还：页面给了 search_focus_target 就用它，否则回页容器（§4.7）。"""
        page = self.stackedWidget.currentWidget()
        if page is None:
            return
        hook = getattr(page, "search_focus_target", None)
        target = hook() if callable(hook) else None
        (target or page).setFocus()

    @Slot(int)
    def __on_intercept_attention(self, count: int) -> None:
        """断点刚攥住第一批流量，托盘再提醒一次。

        窗口自己已经 show/raise/activateWindow 过了（边沿判定只在
        `InterceptWindow._on_flows_changed` 里做一次），但主窗口最小化到托盘、或者
        用户正在别的应用里全屏时，那次抢焦点未必看得见。
        """
        self.tray_icon.showMessage(
            APP_NAME,
            self.tr("断点拦下 {} 条流量，等待处理").format(count),
            QIcon(":/icon"),
            5000,
        )

    @Slot(object)
    def __on_capture_state_changed(self, state: object) -> None:
        if CaptureState(state) == CaptureState.STOPPED:
            self.session_controller.refresh()

    @Slot(str)
    def __edit_in_compose(self, flow_id: str) -> None:
        try:
            edit = self.captures_interface.controller.request_edit(flow_id)
        except (ValueError, RuntimeError) as exc:
            show_warning(self.tr("在 Compose 中编辑失败"), str(exc), self)
            return
        self.compose_interface.prefill(edit)
        self.switchTo(self.compose_interface)

    @Slot()
    def __on_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """处理图标激活事件"""
        # 判断是否为双击
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.raise_()
            self.activateWindow()

    def shutdown(self) -> bool:
        """统一退出流程：关闭 Save、停止代理、提交录制。"""
        if self._shutdown_complete:
            return True
        try:
            CONFIG.flush_pending_save()
        except OSError as exc:
            show_warning(self.tr("退出未完成"), str(exc), self)
            return False
        self.captures_interface.stop_capture()
        complete = self.runtime.shutdown()
        self._shutdown_complete = complete
        if complete:
            # hide 而不是 close：`closeEvent` 在队列非空时会弹确认框，而这里用户已经
            # 决定退出了，再问一遍「挂着的怎么办」只是噪音 —— 内核马上停，挂起的连接
            # 跟着断，这就是退出该有的语义（本轮刻意不做超时自动放行）。
            self.intercept_window.hide()
        else:
            self.show()
            self.raise_()
            show_warning(
                self.tr("退出未完成"),
                self.runtime.last_shutdown_error
                or self.tr("清理代理或内核失败，请重试退出。"),
                self,
            )
        return complete

    @Slot(object)
    def _apply_update(self, info: object) -> None:
        # The update SDK exits the process directly, bypassing Qt's quit hooks.
        if self.shutdown():
            self.settings_interface.update_controller.apply_and_restart(info)
            # A successful SDK call exits. If it returns after failure, the user
            # can start capture again; a later exit must perform cleanup anew.
            self._shutdown_complete = False

    @Slot(str)
    def _show_startup_error(self, message: str) -> None:
        QTimer.singleShot(
            0, lambda: show_warning(self.tr("系统代理恢复失败"), message, self)
        )

    def closeEvent(self, event):
        if CONFIG.get(CONFIG.minimize_to_tray):
            event.ignore()
            # 断点窗口跟着一起收起来：主窗口都藏了还留一个飘在桌面上会很意外。挂起的
            # 流量不会因此丢，从断点页的「拦截队列」还能把它叫回来。
            self.intercept_window.hide()
            self.hide()
        else:
            if self.shutdown():
                event.accept()
            else:
                event.ignore()


class SystemTray(QSystemTrayIcon):
    def __init__(self, parent: MainWindow):
        super().__init__(parent)
        self.quit_action = None

        self.__init_tray()
        self.menu = CheckableSystemTrayMenu()
        self.__init_tray_menu()

        self.show()

    def __init_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.setIcon(QIcon(":/icon"))
        self.setToolTip(APP_NAME)

    def __init_tray_menu(self):
        self.quit_action = BaseAction(
            icon=FluentIcon.POWER_BUTTON,
            text=self.tr("退出"),
            parent=self,
            triggered=self._on_quit,
        )
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)

    @Slot()
    def _on_quit(self) -> None:
        window = self.parent()
        if isinstance(window, MainWindow) and window.shutdown():
            QApplication.quit()


class PinButton(FluentTitleBarButton):
    """置顶按钮组件"""

    def __init__(self, parent=None, shortcut: str = "Ctrl+T"):
        super().__init__(FluentIcon.PIN, parent)

        self._is_pinned = False
        self._shortcut = shortcut

        self.__init_widget()
        self.__init_shortcut()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化组件"""
        self.setToolTip(self.tr("置顶"))
        self.installEventFilter(ToolTipFilter(self, 1000, ToolTipPosition.TOP))

    def __init_shortcut(self):
        """初始化快捷键"""
        if self._shortcut:
            self._shortcut_obj = QShortcut(QKeySequence(self._shortcut), self.window())
            self._shortcut_obj.activated.connect(self.toggle)

    def __connect_signal_to_slot(self):
        """连接信号到槽"""
        self.clicked.connect(self.toggle)

    @Slot()
    def toggle(self):
        """切换置顶状态"""
        self._is_pinned = not self._is_pinned
        self.__update_ui()

    def __update_ui(self):
        """更新 UI"""
        if self._is_pinned:
            self.setIcon(FluentIcon.UNPIN)
            self.setToolTip(self.tr("取消置顶") + f" ({self._shortcut})")
        else:
            self.setIcon(FluentIcon.PIN)
            self.setToolTip(self.tr("置顶") + f" ({self._shortcut})")
