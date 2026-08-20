from PySide6.QtCore import QRect, Slot
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

from ferret.apps.blocklist.controllers import BlockListController
from ferret.apps.blocklist.views import BlockListInterface
from ferret.apps.capture.controllers import CaptureState
from ferret.apps.capture.views import CapturesInterface
from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.views import CertificateInterface
from ferret.apps.common.icon import BaseAction
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
        # 两个规则控制器都建在 runtime.start() 之前：构造时就把已存规则交给 facade，
        # Master 起来时 _run_master 会在服务第一个请求前下发。
        self.blocklist_controller = BlockListController(self, mitm=self.runtime.mitm)
        self.blocklist_interface = BlockListInterface(
            controller=self.blocklist_controller, parent=self
        )
        self.rewrite_controller = RewriteController(self, mitm=self.runtime.mitm)
        self.rewrite_interface = RewriteInterface(
            controller=self.rewrite_controller, parent=self
        )
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
        self.__center_window()
        self.__init_navigation()
        self.__connect_signal_to_slot()

    def __init_navigation(self):
        self.addSubInterface(
            self.captures_interface, FluentIcon.WIFI, self.tr("captures")
        )

        self.addSubInterface(
            self.sessions_interface, FluentIcon.HISTORY, self.tr("sessions")
        )

        self.addSubInterface(
            self.blocklist_interface, FluentIcon.FILTER, self.tr("blocklist")
        )

        self.addSubInterface(
            self.rewrite_interface, FluentIcon.SYNC, self.tr("rewrite")
        )

        self.addSubInterface(
            self.certificate_interface,
            FluentIcon.CERTIFICATE,
            self.tr("certificate"),
        )

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            self.tr("settings"),
            NavigationItemPosition.BOTTOM,
        )

    def __connect_signal_to_slot(self):
        qconfig.themeChanged.connect(lambda theme: setTheme(theme))
        self.pin_button.clicked.connect(self.toggleStayOnTop)
        self.tray_icon.activated.connect(self.__on_activated)
        self.captures_interface.controller.capture_state_changed.connect(
            self.__on_capture_state_changed
        )
        # 由主窗口牵线，apps/capture 不必认识 apps/blocklist。
        self.captures_interface.block_host_requested.connect(
            self.blocklist_controller.add_host_rule
        )

    @Slot(object)
    def __on_capture_state_changed(self, state: object) -> None:
        if CaptureState(state) == CaptureState.STOPPED:
            self.session_controller.refresh()

    def __center_window(self):
        """窗口居中逻辑"""
        desktop: QRect = QApplication.primaryScreen().availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

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
        return complete

    def closeEvent(self, event):
        if CONFIG.get(CONFIG.minimize_to_tray):
            event.ignore()
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
