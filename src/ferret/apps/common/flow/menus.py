"""Flow 表格的右键菜单：查看 / 重放 / 导出 / 屏蔽 / 删除 / 备注。

从 `views.py` 整段搬出来的，只是换了个落脚处 —— 类名、信号、门控语义都没动，
`views.py` 仍然 re-export 这三个类，挂载点的 import 不受影响。
"""

import re
import time
from pathlib import Path
from typing import ClassVar

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
    # 「在 Compose 中编辑」请求信号（携带 flow id）。载荷是 id 不是 flow：
    # handler 只拿 id 去 facade 提取，活 flow 引用不进 Qt 槽。
    edit_in_compose_requested = Signal(str)
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
        # 多选禁用（「编辑并重发」语义不明）、CONNECT 禁用（隧道请求没有可编辑的
        # 报文形态）。判据都来自既有入参，不新读活 flow。
        self.edit_in_compose_action.setEnabled(
            len(self.flows) == 1 and self.row_data.get("Method") != "CONNECT"
        )
        self.export_menu.refresh_selection_labels()

    def __init_widget(self):
        """初始化界面组件"""
        self.client_replay_action = BaseAction(
            parent=self, icon=FluentIcon.SYNC, text=self.tr("重发")
        )
        self.edit_in_compose_action = BaseAction(
            parent=self, icon=FluentIcon.EDIT, text=self.tr("在 Compose 中编辑")
        )
        self.replay_from_file_action = BaseAction(
            parent=self, icon=FluentIcon.FOLDER, text=self.tr("从文件回放…")
        )
        self.delete_action = BaseAction(
            parent=self,
            icon=FluentIcon.DELETE,
            text=self.tr("删除"),
            shortcut=QKeySequence.StandardKey.Delete,
        )
        self.block_host_action = BaseAction(
            parent=self,
            icon=FluentIcon.CANCEL_MEDIUM,
            text=self.tr("屏蔽此主机"),
        )
        self.comment_action = BaseAction(
            parent=self,
            icon=FluentIcon.TAG,
            text=self.tr("备注..."),
        )
        self.export_menu = FlowExportMenu(self, self.controller)
        self.view_menu = FlowSubViewMenu(self)

    def __init_action(self):
        """初始化菜单动作"""
        self.addMenu(self.view_menu)
        if self.capabilities.can_replay:
            self.addAction(self.client_replay_action)
            self.addAction(self.replay_from_file_action)
        if self.capabilities.can_edit_compose:
            self.addAction(self.edit_in_compose_action)
        self.addMenu(self.export_menu)
        if self.capabilities.can_block:
            self.addAction(self.block_host_action)
        if self.capabilities.can_delete:
            self.addAction(self.delete_action)

        self.addAction(self.comment_action)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.client_replay_action.triggered.connect(self.__on_client_replay_triggered)
        self.edit_in_compose_action.triggered.connect(
            self.__on_edit_in_compose_triggered
        )
        self.replay_from_file_action.triggered.connect(self.replay_file_requested.emit)
        self.delete_action.triggered.connect(self.__on_delete_triggered)
        self.block_host_action.triggered.connect(self.__on_block_host_triggered)
        self.view_menu.urlViewRequested.connect(self.__show_url_window)
        self.comment_action.triggered.connect(self.__on_comment_triggered)

    def _refresh_replay_label(self) -> None:
        """根据当前选中数量刷新重发动作文案：单选=重发，多选=重发 N 条。"""
        count = len(self.flows)
        if count <= 1:
            self.client_replay_action.setText(self.tr("重发"))
        else:
            self.client_replay_action.setText(self.tr("重发 {} 条").format(count))

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
                self.tr("成功"),
                self.tr("备注已保存"),
                self.main_window,
            )
        except (ValueError, RuntimeError) as exc:
            show_warning(self.tr("备注保存失败"), str(exc), self.main_window)

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
    def __on_edit_in_compose_triggered(self):
        """把当前行交给 compose 页（由主窗口牵线：提取 → prefill → 切页）。"""
        flow_id = self.row_data.get("id", "")
        if flow_id:
            self.edit_in_compose_requested.emit(flow_id)

    @Slot()
    def __show_url_window(self):
        """显示 URL 窗口"""
        url = self.row_data.get("URL", "No URL")
        msg = TextCopyDialog(url, "URL", self.main_window)
        if msg.exec():
            show_success(
                self.tr("成功"), self.tr("URL 已复制到剪贴板"), self.main_window
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
            show_warning(self.tr("回放失败"), str(exc), self.main_window)


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
        self.setTitle(self.tr("导出"))

        self.curl_action = BaseAction(
            parent=self,
            icon=FluentIcon.COPY,
            text=self.tr("复制 cURL"),
            shortcut=QKeySequence("Ctrl+Shift+C"),
        )
        self.httpie_action = BaseAction(
            parent=self,
            icon=FluentIcon.CODE,
            text=self.tr("复制 HTTPie"),
        )
        self.raw_request_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("复制原始请求"),
        )
        self.raw_response_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("复制原始响应"),
        )
        self.raw_flow_action = BaseAction(
            parent=self,
            icon=FluentIcon.DOCUMENT,
            text=self.tr("复制原始流量"),
        )
        self.save_request_body_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("另存请求体为文件…"),
        )
        self.save_response_body_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("另存响应体为文件…"),
        )
        self.save_raw_request_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("另存原始请求为文件…"),
        )
        self.save_raw_response_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("另存原始响应为文件…"),
        )
        self.save_raw_flow_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("另存原始流量为文件…"),
        )
        self.har_action = BaseAction(
            parent=self,
            icon=FluentIcon.SAVE,
            text=self.tr("导出为 HAR"),
        )
        self.save_flows_action = BaseAction(
            parent=self, icon=FluentIcon.SAVE, text=self.tr("导出为 FLOW")
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
        self.addAction(self.save_request_body_action)
        self.addAction(self.save_response_body_action)
        self.addAction(self.save_raw_request_action)
        self.addAction(self.save_raw_response_action)
        self.addAction(self.save_raw_flow_action)
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
        self.save_request_body_action.triggered.connect(
            lambda: self.__save_bytes("request_body")
        )
        self.save_response_body_action.triggered.connect(
            lambda: self.__save_bytes("response_body")
        )
        self.save_raw_request_action.triggered.connect(
            lambda: self.__save_bytes("raw_request")
        )
        self.save_raw_response_action.triggered.connect(
            lambda: self.__save_bytes("raw_response")
        )
        self.save_raw_flow_action.triggered.connect(
            lambda: self.__save_bytes("raw_flow")
        )
        self.har_action.triggered.connect(lambda: self.__export_file("har"))
        self.save_flows_action.triggered.connect(lambda: self.__export_file("flow"))

    def refresh_selection_labels(self) -> None:
        """和重发一致：两个文件导出都作用于整个选区，把条数写进文案避免歧义。"""
        count = len(self.context_menu.flows)
        if count <= 1:
            self.har_action.setText(self.tr("导出为 HAR"))
            self.save_flows_action.setText(self.tr("导出为 FLOW"))
        else:
            self.har_action.setText(self.tr("导出 {} 条为 HAR").format(count))
            self.save_flows_action.setText(self.tr("导出 {} 条为 FLOW").format(count))

    def __flow_id(self) -> str:
        """从上下文行数据取出 flow id"""
        return self.context_menu.row_data.get("id", "")

    def __export_text(self, kind: str):
        """导出文本类命令（cURL / HTTPie）到剪贴板"""
        flow_id = self.__flow_id()
        if not flow_id or not self.controller:
            show_warning(
                self.tr("警告"),
                self.tr("导出失败：请求尚未完成或控制器不可用"),
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
                self.tr("警告"),
                self.tr("%s 命令尚未生成，请等待请求完成") % label,
                self.main_window,
            )
            return

        QApplication.clipboard().setText(text)
        show_success(
            self.tr("成功"),
            self.tr("%s 已复制到剪贴板") % label,
            self.main_window,
        )

    def __export_bytes(self, kind: str):
        """导出原始字节报文（请求 / 响应 / 完整流量）到剪贴板"""
        flow_id = self.__flow_id()
        if not flow_id or not self.controller:
            show_warning(
                self.tr("警告"),
                self.tr("导出失败：请求尚未完成或控制器不可用"),
                self.main_window,
            )
            return

        if kind == "raw_request":
            data = self.controller.get_raw_request(flow_id)
            label = self.tr("原始请求")
        elif kind == "raw_response":
            data = self.controller.get_raw_response(flow_id)
            label = self.tr("原始响应")
        else:
            data = self.controller.get_raw_flow(flow_id)
            label = self.tr("原始流量")

        if not data:
            show_warning(
                self.tr("警告"),
                self.tr("%s 尚未生成，请等待请求完成") % label,
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
            self.tr("成功"),
            self.tr("%s 已复制到剪贴板") % label,
            self.main_window,
        )

    # Content-Type（小写、去参数）→ 保存 body 时的默认后缀。不上 mime 库：常见
    # 网络类型一张小表够用，未知类型给空后缀、让用户自己定，比猜错强。
    _BODY_SUFFIXES: ClassVar[dict[str, str]] = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "application/json": ".json",
        "text/html": ".html",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/css": ".css",
        "text/plain": ".txt",
    }

    def __save_bytes(self, kind: str) -> None:
        """把当前行的报文 / body 逐字节写成文件（剪贴板是文本管道，二进制会损坏）。

        与剪贴板 raw 组同语义：作用于右键所在行，不随多选变文案。两类口径刻意
        分开命名——body 是**解压后**内容，raw 是**线上字节**（body 仍压缩态），
        存出来的东西不同，菜单文案不得混用。
        """
        flow_id = self.__flow_id()
        if not flow_id or not self.controller:
            show_warning(
                self.tr("警告"),
                self.tr("导出失败：请求尚未完成或控制器不可用"),
                self.main_window,
            )
            return

        try:
            if kind == "request_body":
                data = self.controller.get_request_body(flow_id)
            elif kind == "response_body":
                data = self.controller.get_response_body(flow_id)
            elif kind == "raw_request":
                data = self.controller.get_raw_request(flow_id)
            elif kind == "raw_response":
                data = self.controller.get_raw_response(flow_id)
            else:
                data = self.controller.get_raw_flow(flow_id)
        except ValueError:
            # raw_response 对挂起中（response 未到）的流量抛 ValueError；body 两件
            # 天然回 b""。统一收敛到同一条「暂无可保存内容」警告，比剪贴板老路稳。
            data = b""

        if not data:
            show_warning(
                self.tr("警告"),
                self.tr("暂无可保存的内容，报文体为空或响应尚未到达"),
                self.main_window,
            )
            return

        suggested = self.__default_save_name(kind)
        path, _ = QFileDialog.getSaveFileName(
            self.main_window,
            self.tr("保存到文件"),
            suggested,
            self.tr("所有文件 (*)"),
        )
        # 用户取消返回空串，必须挡在写之前（同 __export_file 的坑：空路径会让
        # write_bytes 落到目录上抛 PermissionError，界面毫无反馈）。
        if not path:
            return
        # 没写后缀就补建议名的后缀；写了就不动（用户自己定的优先）。
        if not Path(path).suffix and Path(suggested).suffix:
            path += Path(suggested).suffix

        try:
            Path(path).write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            show_error(self.tr("保存失败"), str(exc), self.main_window)
            return

        show_success(
            self.tr("成功"),
            self.tr("已保存到 {}").format(Path(path).name),
            self.main_window,
        )

    def __default_save_name(self, kind: str) -> str:
        """Save 组的默认文件名：`方法_主机` + 角色后缀 / Content-Type 推断后缀。

        Content-Type 从右键时已拉好的详情字典读（``__on_show_context_menu``
        每次都重新构建），不为起个名再开一次 ``runtime.call``。pending 响应没有
        Response 段，``.get`` 拿到 None —— 兜底成空串走「未知类型」。
        """
        method = self.context_menu.row_data.get("Method") or "GET"
        host = self.context_menu.row_data.get("Host") or "unknown"
        base = re.sub(r'[\\/:*?"<>|]', "_", f"{method}_{host}")

        if kind == "request_body":
            content_type = self.context_menu.row_data.get("Request Content-Type") or ""
        elif kind == "response_body":
            content_type = self.context_menu.row_data.get("Response Content-Type") or ""
        else:
            content_type = ""
        mime = content_type.split(";", 1)[0].strip().lower()
        suffix = self._BODY_SUFFIXES.get(mime, "")
        if not suffix and "javascript" in mime:
            suffix = ".js"

        if kind in ("request_body", "response_body"):
            return base + suffix
        role = kind.removeprefix("raw_")  # request / response / flow
        return f"{base}_{role}.txt"

    def __export_file(self, kind: str) -> None:
        """把当前选区的流量写成文件（HAR / Flow）。

        选区来自 ``FlowContextMenu.flows``（和"重发"同一份数据，保持表格选中
        顺序），选 1 条就是 1 条，选 N 条就是 N 条，全部写进同一个文件。
        ``FlowExporter.save_har`` 和 ``FlowFile.write`` 都不依赖 ``ctx``，所以抓包
        页和只读会话页（无 master）走同一条路径。
        """
        if not self.controller:
            show_warning(self.tr("警告"), self.tr("控制器不可用"), self.main_window)
            return

        flows = list(self.context_menu.flows)
        if not flows:
            show_warning(
                self.tr("警告"),
                self.tr("请先选中要导出的流量"),
                self.main_window,
            )
            return

        if kind == "har":
            title = self.tr("导出 HAR")
            suffix = ".har"
            name_filter = self.tr("HAR 文件 (*.har)")
        else:
            title = self.tr("导出 Flow")
            suffix = ".flow"
            name_filter = self.tr("Flow 文件 (*.flow)")

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
            show_error(self.tr("导出失败"), str(exc), self.main_window)
            return

        show_success(
            self.tr("成功"),
            self.tr("已导出 {} 条流量到 {}").format(len(flows), Path(path).name),
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


__all__ = ["FlowContextMenu", "FlowExportMenu", "FlowSubViewMenu"]
