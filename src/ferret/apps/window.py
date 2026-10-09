from __future__ import annotations

from typing import cast

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget
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
    setThemeColor,
)

from ferret.apps.capture.controllers import CaptureState
from ferret.apps.capture.views import CapturesInterface
from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.views import CertificateInterface
from ferret.apps.common.icon import BaseAction, BaseIcon
from ferret.apps.common.info_bar import show_warning
from ferret.apps.common.lazy_page import LazyPage
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
from ferret.apps.update.coordinator import UpdateCoordinator
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import APP_NAME, CONFIG


class MainWindow(FluentWindow):
    def __init__(self, runtime: ApplicationRuntime | None = None):
        super().__init__()
        self._shutdown_complete = False
        self.runtime = runtime or ApplicationRuntime(self)
        self._owns_runtime = runtime is None

        self.session_controller = SessionController(self)
        self.updates = UpdateCoordinator(self)
        # titlebar 搜索宿主先于各页创建：捕获页错误面板「清除表达式」与脚本页
        # clear_search 都经它注入（规格 §5.3 v4 / §6）。
        self.search_host = SearchHost(self)
        self.captures_interface = CapturesInterface(
            self,
            mitm=self.runtime.mitm,
            system_proxy=self.runtime.system_proxy,
            search_host=self.search_host,
        )
        self.sessions_interface = LazyPage(
            self.__create_sessions_interface, "SessionsInterface"
        )
        # 规则控制器都建在 runtime.start() 之前：构造时就把已存规则交给 facade，
        # Master 起来时 _run_master 会在服务第一个请求前下发（脚本清单同理）。
        self.gateway_controller = GatewayController(self, mitm=self.runtime.mitm)
        self.rewrite_controller = RewriteController(self, mitm=self.runtime.mitm)
        self.mock_controller = MockController(self, mitm=self.runtime.mitm)
        self.intercept_controller = InterceptController(self, mitm=self.runtime.mitm)
        self.compose_controller = ComposeController(self, mitm=self.runtime.mitm)
        self.scripts_controller = ScriptsController(self, mitm=self.runtime.mitm)
        self.certificate_controller = CertificateController(
            self, mitm=self.runtime.mitm
        )

        # 子页懒构造（LazyPage 占位容器）：每页都是一整套 qfw 控件树（几十 MB
        # 量级），首次切到才建。控制器先行保证规则下发时序；各页构造函数会拉控制器
        # 当前状态（规则 / 池 / 脚本清单 / 证书态 / 断点队列），晚构造不会错过启动期
        # 数据。objectName 由占位容器接管为路由键；断点页的 queue_requested 经
        # on_ensure 补牵线（页和窗口互不认识，见 __connect_intercept_queue）。
        self.gateway_interface = LazyPage(
            lambda: GatewayInterface(controller=self.gateway_controller),
            "GatewayInterface",
        )
        self.rewrite_interface = LazyPage(
            lambda: RewriteInterface(controller=self.rewrite_controller),
            "RewriteInterface",
        )
        self.mock_interface = LazyPage(
            lambda: MockInterface(controller=self.mock_controller),
            "MockInterface",
        )
        self.intercept_interface = LazyPage(
            lambda: InterceptInterface(controller=self.intercept_controller),
            "InterceptInterface",
            on_ensure=self.__connect_intercept_queue,
        )
        self.compose_interface = LazyPage(
            lambda: ComposeInterface(controller=self.compose_controller),
            "ComposeInterface",
        )
        self.scripts_interface = LazyPage(
            lambda: ScriptsInterface(
                controller=self.scripts_controller, search_host=self.search_host
            ),
            "ScriptsInterface",
        )
        # 断点窗口是独立顶层窗口，构造时不能给 Qt 父对象（`qframelesswindow` 的
        # `updateFrameless()` 不补 `Qt.Window`，给了父对象就退化成子控件），所以它的
        # 生命周期就靠这个属性持着 —— 丢了引用窗口会被 GC 掉。它是懒构造的：有流量
        # 被断点攥住才需要，触发器见 `__ensure_intercept_window`。
        self.intercept_window: InterceptWindow | None = None
        self.certificate_interface = LazyPage(
            lambda: CertificateInterface(controller=self.certificate_controller),
            "CertificateInterface",
        )
        self.settings_interface = LazyPage(
            self.__create_settings_interface, "settingInterface"
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
        # 开关与形态闸门都收在协调器内部，不依赖设置页构造。
        QTimer.singleShot(5000, self.updates.check_auto)

    def __create_settings_interface(self) -> QWidget:
        # 模块和控件树都延后到首次切页，更新入口由主窗口常驻持有。
        from ferret.apps.settings.views import SettingsInterface

        return SettingsInterface(mitm=self.runtime.mitm, updates=self.updates)

    def __create_sessions_interface(self) -> QWidget:
        from ferret.apps.session.views import SessionsInterface

        # 首次构造会重新查询会话列表，不依赖页面创建前已发出的 sessions_loaded。
        return SessionsInterface(controller=self.session_controller)

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
        self.updates.restart_requested.connect(self._apply_update)
        self.runtime.startup_error.connect(self._show_startup_error)
        qconfig.themeChanged.connect(lambda theme: setTheme(theme))
        CONFIG.themeColorChanged.connect(setThemeColor)
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
        # 断点窗口懒构造的自动侧：流量攥住的第一批走 flows_changed 确保窗口存在，
        # 空→非空的弹窗边沿与托盘提醒仍由窗口自己判（构造函数会拉当前队列）。
        # 断点页的「拦截队列」走 on_ensure 补牵线——页本身也是懒构造的，见
        # __connect_intercept_queue。
        self.intercept_controller.flows_changed.connect(
            self.__on_intercept_flows_changed
        )

    def __on_page_changed(self, index: int) -> None:
        """titlebar 搜索槽路由（规格 §4.3 v3）：协议页显示 + 动作注入 / 其余隐藏。

        懒构造页在此同步 ensure：真实页随切页信号就地构造，随后的协议查询（鸭子
        判定 + 委托）才有目标。
        """
        page = self.stackedWidget.widget(index)
        if isinstance(page, LazyPage):
            page.ensure()
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

    def __connect_intercept_queue(self, page: QWidget) -> None:
        """断点页 ensure 后补牵线：页不认识窗口，「拦截队列」先 ensure 再弹。"""
        cast(InterceptInterface, page).queue_requested.connect(
            self.__pop_intercept_window
        )

    def __ensure_intercept_window(self) -> InterceptWindow:
        """首次需要时构造断点窗口，attention 接线随构造一并接上。"""
        if self.intercept_window is None:
            window = InterceptWindow(self.intercept_controller)
            window.attention_requested.connect(self.__on_intercept_attention)
            self.intercept_window = window
        return self.intercept_window

    @Slot(list)
    def __on_intercept_flows_changed(self, flows: list) -> None:
        """断点攥住流量：此刻才构造窗口。构造函数会拉当前队列，自动弹窗照常。"""
        if flows:
            self.__ensure_intercept_window()

    def __pop_intercept_window(self) -> None:
        self.__ensure_intercept_window().pop_up()

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
        if (
            CaptureState(state) == CaptureState.STOPPED
            and self.sessions_interface.is_created
        ):
            self.session_controller.refresh()

    @Slot(str)
    def __edit_in_compose(self, flow_id: str) -> None:
        try:
            edit = self.captures_interface.controller.request_edit(flow_id)
        except (ValueError, RuntimeError) as exc:
            show_warning(self.tr("在 Compose 中编辑失败"), str(exc), self)
            return
        self.compose_interface.ensure().prefill(edit)
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
            # 跟着断，这就是退出该有的语义（本轮刻意不做超时自动放行）。窗口可能
            # 尚未懒构造过，判空跳过。
            if self.intercept_window is not None:
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
            self.updates.controller.apply_and_restart(info)
            # A successful SDK call exits. If it returns after failure, the user
            # can start capture again; a later exit must perform cleanup anew.
            # 窗口与 runtime 两级停机闸门都要复位，漏掉 runtime 一级时二次退出在
            # runtime.shutdown() 短路，内核线程带活 Master 撞上进程退出（qFatal）。
            self._shutdown_complete = False
            self.runtime.resume_after_failed_apply()

    @Slot(str)
    def _show_startup_error(self, message: str) -> None:
        QTimer.singleShot(
            0, lambda: show_warning(self.tr("系统代理恢复失败"), message, self)
        )

    def closeEvent(self, event):
        if CONFIG.get(CONFIG.minimize_to_tray):
            event.ignore()
            # 断点窗口跟着一起收起来：主窗口都藏了还留一个飘在桌面上会很意外。挂起的
            # 流量不会因此丢，从断点页的「拦截队列」还能把它叫回来。窗口可能尚未
            # 懒构造过，判空跳过。
            if self.intercept_window is not None:
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
        """切换置顶：真正翻转窗口标志，再按窗口实际状态同步图标与提示。

        快捷键与点击共用这一个入口。此前快捷键只连 `toggle` 翻图标，点击另连窗口的
        `toggleStayOnTop` 翻窗口，两条路各走一半：按 Ctrl+T 图标变了窗口没置顶，随后
        点击又让图标与窗口状态反向。图标状态一律取自窗口真实标志，不再各记一份。
        """
        window = cast("MainWindow", self.window())
        window.toggleStayOnTop()
        self._is_pinned = bool(
            window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        )
        self.__update_ui()

    def __update_ui(self):
        """更新 UI"""
        if self._is_pinned:
            self.setIcon(FluentIcon.UNPIN)
            self.setToolTip(self.tr("取消置顶") + f" ({self._shortcut})")
        else:
            self.setIcon(FluentIcon.PIN)
            self.setToolTip(self.tr("置顶") + f" ({self._shortcut})")
