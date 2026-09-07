from PySide6.QtCore import Slot
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
from ferret.apps.common.window import center_window
from ferret.apps.compose.controllers import ComposeController
from ferret.apps.compose.views import ComposeInterface
from ferret.apps.gateway.controllers import GatewayController
from ferret.apps.gateway.views import GatewayInterface
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.views import InterceptInterface
from ferret.apps.intercept.window import InterceptWindow
from ferret.apps.rewrite.controllers import RewriteController
from ferret.apps.rewrite.views import RewriteInterface
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
        self.settings_interface = SettingsInterface(self)
        self.captures_interface = CapturesInterface(
            self,
            mitm=self.runtime.mitm,
            system_proxy=self.runtime.system_proxy,
        )
        self.sessions_interface = SessionsInterface(
            controller=self.session_controller, parent=self
        )
        # 三个规则控制器都建在 runtime.start() 之前：构造时就把已存规则交给 facade，
        # Master 起来时 _run_master 会在服务第一个请求前下发。
        self.gateway_controller = GatewayController(self, mitm=self.runtime.mitm)
        self.gateway_interface = GatewayInterface(
            controller=self.gateway_controller, parent=self
        )
        self.rewrite_controller = RewriteController(self, mitm=self.runtime.mitm)
        self.rewrite_interface = RewriteInterface(
            controller=self.rewrite_controller, parent=self
        )
        self.intercept_controller = InterceptController(self, mitm=self.runtime.mitm)
        self.intercept_interface = InterceptInterface(
            controller=self.intercept_controller, parent=self
        )
        self.compose_controller = ComposeController(self, mitm=self.runtime.mitm)
        self.compose_interface = ComposeInterface(
            controller=self.compose_controller, parent=self
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

        self.navigationInterface.setExpandWidth(260)
        center_window(self)
        self.__init_navigation()
        self.__connect_signal_to_slot()

    def __init_navigation(self):
        self.addSubInterface(
            self.captures_interface, FluentIcon.WIFI, self.tr("Captures")
        )

        self.addSubInterface(
            self.sessions_interface, FluentIcon.HISTORY, self.tr("Sessions")
        )

        self.addSubInterface(self.gateway_interface, FluentIcon.VPN, self.tr("Gateway"))

        self.addSubInterface(
            self.rewrite_interface, FluentIcon.PENCIL_INK, self.tr("Rewrite")
        )

        self.addSubInterface(
            self.intercept_interface, BaseIcon.BUG, self.tr("Intercept")
        )

        self.addSubInterface(
            self.compose_interface, FluentIcon.SEND, self.tr("Compose")
        )

        self.addSubInterface(
            self.certificate_interface,
            FluentIcon.CERTIFICATE,
            self.tr("Certificate"),
        )

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            self.tr("Settings"),
            NavigationItemPosition.BOTTOM,
        )

    def __connect_signal_to_slot(self):
        qconfig.themeChanged.connect(lambda theme: setTheme(theme))
        self.pin_button.clicked.connect(self.toggleStayOnTop)
        self.tray_icon.activated.connect(self.__on_activated)
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
        # 同上：断点页和断点窗口互不认识，两个方向都从这里接。
        self.intercept_interface.queue_requested.connect(self.intercept_window.pop_up)
        self.intercept_window.attention_requested.connect(self.__on_intercept_attention)

    @Slot(int)
    def __on_intercept_attention(self, count: int) -> None:
        """断点刚攥住第一批流量，托盘再提醒一次。

        窗口自己已经 show/raise/activateWindow 过了（边沿判定只在
        `InterceptWindow._on_flows_changed` 里做一次），但主窗口最小化到托盘、或者
        用户正在别的应用里全屏时，那次抢焦点未必看得见。
        """
        self.tray_icon.showMessage(
            APP_NAME,
            self.tr("Intercepted {} flow(s), waiting to be handled").format(count),
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
            show_warning(self.tr("Edit in Compose failed"), str(exc), self)
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
        self.captures_interface.stop_capture()
        complete = self.runtime.shutdown()
        self._shutdown_complete = complete
        if complete:
            # hide 而不是 close：`closeEvent` 在队列非空时会弹确认框，而这里用户已经
            # 决定退出了，再问一遍「挂着的怎么办」只是噪音 —— 内核马上停，挂起的连接
            # 跟着断，这就是退出该有的语义（本轮刻意不做超时自动放行）。
            self.intercept_window.hide()
        return complete

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
            text=self.tr("Quit"),
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
        self.setToolTip(self.tr("Pin window"))
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
            self.setToolTip(self.tr("Unpin window") + f" ({self._shortcut})")
        else:
            self.setIcon(FluentIcon.PIN)
            self.setToolTip(self.tr("Pin window") + f" ({self._shortcut})")
