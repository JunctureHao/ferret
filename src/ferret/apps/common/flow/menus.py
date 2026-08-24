"""Flow 表格的右键菜单：查看 / 重放 / 导出 / 屏蔽 / 删除 / 备注。

从 `views.py` 整段搬出来的，只是换了个落脚处 —— 类名、信号、门控语义都没动，
`views.py` 仍然 re-export 这三个类，挂载点的 import 不受影响。
"""

import re
import time
from pathlib import Path

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QFileDialog
from qfluentwidgets import FluentIcon, RoundMenu

from ferret.apps.common.dialog import CommentDialog, TextCopyDialog
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success, show_warning
from ferret.core.mitm import HTTPFlow


class FlowContextMenu(RoundMenu):
    """Flow 上下文菜单 - 提供复制、删除、查看等操作。"""

    delete_requested = Signal(int)  # 删除请求信号
    replay_file_requested = Signal()  # 从文件回放请求信号
    block_host_requested = Signal(str)  # 屏蔽此主机请求信号（携带 host）
    comment_requested = Signal()

    def __init__(
        self,
        parent,
        controller,
        capabilities: FlowViewCapabilities | None = None,
    ):
        super().__init__(parent=parent)
        self.controller = controller  # 供导出子菜单调用控制层
        self.capabilities = capabilities or CAPTURE_CAPABILITIES
        self.row_index = -1  # 初始化一个无效行号
        self.row_data = {}
        self.main_window = parent.window()
        self.flows: list[HTTPFlow] = []

        self.__init_widget()
        self.__init_action()
        self.__connect_signal_to_slot()

    def update_context(
        self,
        row_index: int,
        row_data: dict,
        selected_flows: list[HTTPFlow],
    ):
        """统一的数据更新入口

        Args:
            row_index: 行索引
            row_data: 行数据字典
            selected_flows: 当前选中的 Flow 列表（保持表格选中顺序）
        """
        self.row_index = row_index
        self.row_data = row_data
        self.flows = selected_flows or []
        self._refresh_replay_label()
        self.export_menu.refresh_selection_labels()

    def __init_widget(self):
        """初始化界面组件"""
        self.client_replay_action = BaseAction(
            parent=self, icon=FluentIcon.SYNC, text=self.tr("Replay")
        )
        self.replay_from_file_action = BaseAction(
            parent=self, icon=FluentIcon.FOLDER, text=self.tr("Replay from file...")
        )
        self.delete_action = BaseAction(
            parent=self,
            icon=FluentIcon.DELETE,
            text=self.tr("Delete"),
            shortcut=QKeySequence.StandardKey.Delete,
        )
        self.block_host_action = BaseAction(
            parent=self,
            icon=FluentIcon.CANCEL_MEDIUM,
            text=self.tr("Block this host"),
        )
        self.comment_action = BaseAction(
            parent=self,
            icon=FluentIcon.TAG,
            text=self.tr("Comment..."),
        )
        self.export_menu = FlowExportMenu(self, self.controller)
        self.view_menu = FlowSubViewMenu(self)

    def __init_action(self):
        """初始化菜单动作"""
        self.addMenu(self.view_menu)
        if self.capabilities.can_replay:
            self.addAction(self.client_replay_action)
            self.addAction(self.replay_from_file_action)
        self.addMenu(self.export_menu)
        if self.capabilities.can_block:
            self.addAction(self.block_host_action)
        if self.capabilities.can_delete:
            self.addAction(self.delete_action)

        self.addAction(self.comment_action)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.client_replay_action.triggered.connect(self.__on_client_replay_triggered)
        self.replay_from_file_action.triggered.connect(self.replay_file_requested.emit)
        self.delete_action.triggered.connect(self.__on_delete_triggered)
        self.block_host_action.triggered.connect(self.__on_block_host_triggered)
        self.view_menu.urlViewRequested.connect(self.__show_url_window)
        self.comment_action.triggered.connect(self.__on_comment_triggered)

    def _refresh_replay_label(self) -> None:
        """根据当前选中数量刷新重发动作文案：单选=重发，多选=重发 N 条。"""
        count = len(self.flows)
        if count <= 1:
            self.client_replay_action.setText(self.tr("Replay"))
        else:
            self.client_replay_action.setText(self.tr("Replay {} flows").format(count))

    @Slot()
    def __on_comment_triggered(self) -> None:
        """编辑当前行的备注。"""
        if not self.controller:
            return
        flow_id = self.row_data.get("id", "")
        if not flow_id:
            return
        dialog = CommentDialog(self.row_data.get("comment", "") or "", self.main_window)
        if not dialog.exec():
            return
        try:
            self.controller.set_flow_comment(flow_id, dialog.comment())
            show_success(
                self.tr("Success"),
                self.tr("Comment saved"),
                self.main_window,
            )
        except (ValueError, RuntimeError) as exc:
            show_warning(self.tr("Failed to save comment"), str(exc), self.main_window)

    @Slot()
    def __on_delete_triggered(self):
        """删除动作触发时"""
        if self.row_index != -1:
            self.delete_requested.emit(self.row_index)

    @Slot()
    def __on_block_host_triggered(self):
        """把当前行的主机交给屏蔽规则页（由主窗口牵线到 BlockListController）。"""
        self.block_host_requested.emit(self.row_data.get("Host", ""))

    @Slot()
    def __show_url_window(self):
        """显示 URL 窗口"""
        url = self.row_data.get("URL", "No URL")
        msg = TextCopyDialog(url, "URL", self.main_window)
        if msg.exec():
            show_success(
                self.tr("Success"), self.tr("URL copied to clipboard"), self.main_window
            )

    @Slot()
    def __on_client_replay_triggered(self):
        """重放当前选中的请求（支持单选/多选）。

        多选时直接调用 ``controller.replay_flows(self.flows)``，保持选中
        顺序；单选时回退到 ``replay_flow(flow_id)`` 兼容旧调用方。两种路径
        最终都通过 ``ClientPlayback.start_replay`` 入队。
        """
        if not self.controller:
            return
        try:
            if len(self.flows) > 1:
                self.controller.replay_flows(self.flows)
                return
            flow_id = self.row_data.get("id", "")
            if flow_id:
                self.controller.replay_flow(flow_id)
        except (ValueError, RuntimeError) as exc:
            show_warning(self.tr("Replay failed"), str(exc), self.main_window)


class FlowExportMenu(RoundMenu):
    """Flow 导出子菜单 - 汇总统一定义的导出能力。"""

    def __init__(self, parent: FlowContextMenu, controller=None):
        super().__init__(parent=parent)
        self.context_menu = parent  # 强类型引用，避免 self.parent() 的 QObject | None
        self.controller = controller
        self.main_window = parent.main_window

        self.__init_widget()
        self.__init_action()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.setIcon(FluentIcon.SAVE)
        self.setTitle(self.tr("Export"))

        self.curl_action = BaseAction(
            parent=self,
            icon=FluentIcon.COPY,
            text=self.tr("Copy as cURL"),
            shortcut=QKeySequence("Ctrl+Shift+C"),
        )
        self.httpie_action = BaseAction(
            parent=self,
            icon=FluentIcon.CODE,
            text=self.tr("Copy as HTTPie"),
        )
        self.raw_request_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("Copy raw request"),
        )
        self.raw_response_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("Copy raw response"),
        )
        self.raw_flow_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("Copy raw flow"),
        )
        self.har_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("Export as HAR"),
        )
        self.save_flows_action = BaseAction(
            parent=self, icon=FluentIcon.SAVE, text=self.tr("Export as FLOW")
        )

    def __init_action(self):
        """初始化菜单动作"""
        self.addAction(self.curl_action)
        self.addAction(self.httpie_action)
        self.addSeparator()
        self.addAction(self.raw_request_action)
        self.addAction(self.raw_response_action)
        self.addAction(self.raw_flow_action)
        self.addSeparator()
        self.addAction(self.har_action)
        # 门控从 FlowContextMenu 一起搬过来，保持原来的语义不变
        if self.context_menu.capabilities.can_save_selection:
            self.addAction(self.save_flows_action)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.curl_action.triggered.connect(lambda: self.__export_text("curl"))
        self.httpie_action.triggered.connect(lambda: self.__export_text("httpie"))
        self.raw_request_action.triggered.connect(
            lambda: self.__export_bytes("raw_request")
        )
        self.raw_response_action.triggered.connect(
            lambda: self.__export_bytes("raw_response")
        )
        self.raw_flow_action.triggered.connect(lambda: self.__export_bytes("raw_flow"))
        self.har_action.triggered.connect(lambda: self.__export_file("har"))
        self.save_flows_action.triggered.connect(lambda: self.__export_file("flow"))

    def refresh_selection_labels(self) -> None:
        """和重发一致：两个文件导出都作用于整个选区，把条数写进文案避免歧义。"""
        count = len(self.context_menu.flows)
        if count <= 1:
            self.har_action.setText(self.tr("Export as HAR"))
            self.save_flows_action.setText(self.tr("Export as FLOW"))
        else:
            self.har_action.setText(self.tr("Export {} flows as HAR").format(count))
            self.save_flows_action.setText(
                self.tr("Export {} flows as FLOW").format(count)
            )

    def __flow_id(self) -> str:
        """从上下文行数据取出 flow id"""
        return self.context_menu.row_data.get("id", "")

    def __export_text(self, kind: str):
        """导出文本类命令（cURL / HTTPie）到剪贴板"""
        flow_id = self.__flow_id()
        if not flow_id or not self.controller:
            show_warning(
                self.tr("Warning"),
                self.tr(
                    "Export failed: the request is unfinished or the controller is unavailable"
                ),
                self.main_window,
            )
            return

        if kind == "curl":
            text = self.context_menu.row_data.get("curl_command") or ""
            label = "cURL"

        else:
            text = self.controller.get_httpie_command(flow_id)
            label = "HTTPie"

        if not text:
            show_warning(
                self.tr("Warning"),
                self.tr(
                    "The %s command is not ready yet, wait for the request to finish"
                )
                % label,
                self.main_window,
            )
            return

        QApplication.clipboard().setText(text)
        show_success(
            self.tr("Success"),
            self.tr("%s copied to clipboard") % label,
            self.main_window,
        )

    def __export_bytes(self, kind: str):
        """导出原始字节报文（请求 / 响应 / 完整流量）到剪贴板"""
        flow_id = self.__flow_id()
        if not flow_id or not self.controller:
            show_warning(
                self.tr("Warning"),
                self.tr(
                    "Export failed: the request is unfinished or the controller is unavailable"
                ),
                self.main_window,
            )
            return

        if kind == "raw_request":
            data = self.controller.get_raw_request(flow_id)
            label = self.tr("Raw request")
        elif kind == "raw_response":
            data = self.controller.get_raw_response(flow_id)
            label = self.tr("Raw response")
        else:
            data = self.controller.get_raw_flow(flow_id)
            label = self.tr("Raw flow")

        if not data:
            show_warning(
                self.tr("Warning"),
                self.tr("%s is not ready yet, wait for the request to finish") % label,
                self.main_window,
            )
            return

        # 优先尝试按 UTF-8 文本复制，失败则回退为十六进制描述
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")

        QApplication.clipboard().setText(text)
        show_success(
            self.tr("Success"),
            self.tr("%s copied to clipboard") % label,
            self.main_window,
        )

    def __export_file(self, kind: str) -> None:
        """把当前选区的流量写成文件（HAR / Flow）。

        选区来自 ``FlowContextMenu.flows``（和"重发"同一份数据，保持表格选中
        顺序），选 1 条就是 1 条，选 N 条就是 N 条，全部写进同一个文件。
        ``FlowExporter.save_har`` 和 ``FlowFile.write`` 都不依赖 ``ctx``，所以抓包
        页和只读会话页（无 master）走同一条路径。
        """
        if not self.controller:
            show_warning(
                self.tr("Warning"), self.tr("Controller unavailable"), self.main_window
            )
            return

        flows = list(self.context_menu.flows)
        if not flows:
            show_warning(
                self.tr("Warning"),
                self.tr("Select the flows you want to export first"),
                self.main_window,
            )
            return

        if kind == "har":
            title = self.tr("Export HAR")
            suffix = ".har"
            name_filter = self.tr("HAR files (*.har)")
        else:
            title = self.tr("Export Flow")
            suffix = ".flow"
            name_filter = self.tr("Flow files (*.flow)")

        path, _ = QFileDialog.getSaveFileName(
            self.main_window,
            title,
            self.__default_file_name(flows, suffix),
            name_filter,
        )
        # 用户取消时返回空串，必须挡在这里：空路径会让 open() 落到 "." 上抛
        # PermissionError，而这是 Qt 槽，异常穿出去只进日志、界面毫无反馈。
        if not path:
            return
        if not path.lower().endswith(suffix):
            path += suffix

        try:
            if kind == "har":
                self.controller.export_har(flows, path)
            else:
                self.controller.save_flows(flows, path)
        except Exception as exc:  # noqa: BLE001
            show_error(self.tr("Export failed"), str(exc), self.main_window)
            return

        show_success(
            self.tr("Success"),
            self.tr("Exported {} flow(s) to {}").format(len(flows), Path(path).name),
            self.main_window,
        )

    @staticmethod
    def __default_file_name(flows: list[HTTPFlow], suffix: str) -> str:
        """单选用 方法_主机，多选用 时间戳_条数，再滤掉 Windows 非法字符。"""
        if len(flows) == 1:
            request = flows[0].request
            host = request.pretty_host or request.host or "unknown"
            name = f"{request.method}_{host}"
        else:
            stamp = time.strftime(
                "%Y%m%d_%H%M%S", time.localtime(flows[0].timestamp_created)
            )
            name = f"flows_{stamp}_{len(flows)}flows"
        return re.sub(r'[\\/:*?"<>|]', "_", name) + suffix


class FlowSubViewMenu(RoundMenu):
    """Flow 查看子菜单 - 提供查看详细信息的功能。"""

    urlViewRequested = Signal()

    def __init__(self, parent: FlowContextMenu):
        super().__init__(parent=parent)

        self.__init_widget()
        self.__init_action()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.setIcon(FluentIcon.VIEW)
        self.setTitle(self.tr("View"))
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


__all__ = ["FlowContextMenu", "FlowExportMenu", "FlowSubViewMenu"]
