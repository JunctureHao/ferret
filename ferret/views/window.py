from PySide6.QtCore import QRect, Slot
from PySide6.QtGui import QKeySequence, QShortcut
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

from ferret.config.settings import APP_NAME, CONFIG
from ferret.views.common.icon import BaseAction
from ferret.views.interface.capture.inteface import CapturesInterface
from ferret.views.interface.request.interface import RequestInterface
from ferret.views.interface.settings import SettingsInterface


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()

        # 直接初始化设置界面
        self.settings_interface = SettingsInterface(self)
        self.captures_interface = CapturesInterface(self)
        self.request_interface = RequestInterface(self)

        self.tray_icon = SystemTray(self)
        self.pin_button = PinButton(self)

        self.__init_window()

    def __init_window(self):
        self.setWindowTitle("Ferret")
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
            self.captures_interface, FluentIcon.GLOBE, self.tr("captures")
        )

        self.addSubInterface(
            self.request_interface, FluentIcon.SEND, self.tr("request")
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

    def closeEvent(self, event):
        if CONFIG.get(CONFIG.minimize_to_tray):
            event.ignore()
            self.hide()
        else:
            self.captures_interface.stop_capture()  # ← 加这一行，放在最前面
            event.accept()  # 允许退出


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
        self.setIcon(FluentIcon.APPLICATION.icon())
        self.setToolTip(APP_NAME)

    def __init_tray_menu(self):
        self.quit_action = BaseAction(
            icon=FluentIcon.POWER_BUTTON,
            text=self.tr("退出"),
            parent=self,
            triggered=QApplication.quit,
        )
        self.menu.addAction(self.quit_action)
        self.setContextMenu(self.menu)


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
