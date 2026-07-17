"""数据包上下文菜单组件"""

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon, RoundMenu

from ferret.views.common.dialog import TextCopyDialog
from ferret.views.common.icon import BaseAction
from ferret.views.common.info_bar import show_success, show_warning

if TYPE_CHECKING:
    from ferret.views.interface.capture.inteface import CapturesDataTable


class PacketContextMenu(RoundMenu):
    """数据包上下文菜单 - 提供复制、删除、查看等操作"""

    delete_requested = Signal(int)  # 删除请求信号

    def __init__(self, parent: "CapturesDataTable"):
        super().__init__(parent=parent)
        self.row_index = -1  # 初始化一个无效行号
        self.row_data = {}
        self.main_window = parent.window()

        self.__init_widget()
        self.__init_action()
        self.__connect_signal_to_slot()

    def update_context(self, row_index: int, row_data: dict):
        """统一的数据更新入口

        Args:
            row_index: 行索引
            row_data: 行数据字典
        """
        self.row_index = row_index
        self.row_data = row_data

    def __init_widget(self):
        """初始化界面组件"""
        self.curl_action = BaseAction(
            parent=self,
            icon=FluentIcon.COPY,
            text=self.tr("复制 cURL"),
            shortcut=QKeySequence("Ctrl+Shift+C"),
        )
        self.delete_action = BaseAction(
            parent=self,
            icon=FluentIcon.DELETE,
            text=self.tr("删除"),
            shortcut=QKeySequence.StandardKey.Delete,
        )
        self.view_menu = PacketSubViewMenu(self)

    def __init_action(self):
        """初始化菜单动作"""
        self.addAction(self.curl_action)
        self.addAction(self.delete_action)
        self.addMenu(self.view_menu)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.curl_action.triggered.connect(self.__export_curl)
        self.delete_action.triggered.connect(self.__on_delete_triggered)
        self.view_menu.urlViewRequested.connect(self.__show_url_window)

    @Slot()
    def __on_delete_triggered(self):
        """删除动作触发时"""
        if self.row_index != -1:
            self.delete_requested.emit(self.row_index)

    @Slot()
    def __export_curl(self):
        """使用预生成的 cURL 命令"""
        curl_cmd = self.row_data.get("curl_command")
        if not curl_cmd:
            show_warning(
                self.tr("警告"),
                self.tr("cURL 命令尚未生成，请等待请求完成"),
                self.main_window,
            )
            return

        QApplication.clipboard().setText(curl_cmd)
        show_success(self.tr("成功"), self.tr("cURL 已复制到剪贴板"), self.main_window)

    @Slot()
    def __show_url_window(self):
        """显示 URL 窗口"""
        url = self.row_data.get("URL", "No URL")
        msg = TextCopyDialog(url, "URL", self.main_window)
        if msg.exec():
            show_success(
                self.tr("成功"), self.tr("URL 已复制到剪贴板"), self.main_window
            )


class PacketSubViewMenu(RoundMenu):
    """数据包子菜单 - 提供查看详细信息的功能"""

    urlViewRequested = Signal()

    def __init__(self, parent: PacketContextMenu):
        super().__init__(parent=parent)

        self.__init_widget()
        self.__init_action()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.setIcon(FluentIcon.VIEW)
        self.setTitle(self.tr("查看"))
        self.url_action = BaseAction(
            parent=self,
            icon=FluentIcon.LINK,
            text=self.tr("URL"),
            shortcut=QKeySequence("Ctrl+U"),
        )

    def __init_action(self):
        """初始化菜单动作"""
        self.addAction(self.url_action)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.url_action.triggered.connect(self.urlViewRequested.emit)
