import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from mitmproxy.utils import human
from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    RoundMenu,
    SimpleCardWidget,
    SubtitleLabel,
    TableView,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    TreeWidget,
)

from ferret.apps.common.dialog import TextCopyDialog
from ferret.apps.common.edit import ItemDualPanel, JsonDualPanel, ToolPlainTextEdit
from ferret.apps.common.flow.models import FlowProxyModel, FlowTableModel
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success, show_warning
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import OrientationSplitter
from ferret.core.mitm import HTTPFlow
from ferret.core.settings import CONFIG
from ferret.utils.http_parser import format_bytes

FieldKey = str | Callable[[dict], str]


def _format_time(ts) -> str:
    """时间戳 → 本地时间字符串；空值显示 ``-``。

    ``human.format_timestamp`` 对 ``None`` 会返回“当前时间”（``time.localtime(None)``），
    所以空值必须自己挡掉。
    """
    return human.format_timestamp(ts) if ts else "-"


def _infer_body_lang(content_type: str) -> str:
    """根据 Content-Type 推断 body 高亮语言。"""
    ct = (content_type or "").lower()
    if "json" in ct:
        return "json"
    if "xml" in ct or "html" in ct:
        return "xml"
    return "http"


def _body_type_label(content_type: str) -> str:
    value = (content_type or "").lower()
    if "json" in value:
        return "JSON"
    if "html" in value:
        return "HTML"
    if "xml" in value:
        return "XML"
    if "javascript" in value:
        return "JS"
    if "css" in value:
        return "CSS"
    if value.startswith("text/"):
        return "Text"
    if value.startswith("image/"):
        return "Binary"
    return ""


class FlowDataTable(TableView):
    """Flow 数据表格 - 显示网络请求数据。"""

    row_double_clicked = Signal(dict)  # 双击行信号
    row_selected = Signal(dict)  # 选中行信号
    stats_updated = Signal(int, int, int)  # 统计更新信号：总条数、显示条数、选中条数

    def __init__(
        self,
        parent: QWidget | None,
        controller,
        capabilities: FlowViewCapabilities | None = None,
    ):
        """初始化数据表格

        :param parent: Flow 表格的父组件
        :param controller: 控制器实例（满足 FlowViewController 协议）
        :param capabilities: 视图能力配置，控制右键菜单可用操作
        """
        super().__init__(parent)
        self.controller = controller  # 保存 controller 引用
        self.capabilities = capabilities or CAPTURE_CAPABILITIES

        self.__init_widget()
        self.__init_view()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.source_model = FlowTableModel(self)
        self.proxy_model = FlowProxyModel(self)

        self.context_menu = FlowContextMenu(self, self.controller, self.capabilities)
        self.setSelectRightClickedRow(True)
        self.proxy_model.setSourceModel(self.source_model)
        self.setModel(self.proxy_model)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def __init_view(self):
        """初始化表格视图"""
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        # self.setAlternatingRowColors(False) # 斑马纹

        # 关闭平滑滚动，避免晃眼
        self.scrollDelagate.verticalSmoothScroll.setDynamicEngineEnabled(False)

        self.verticalHeader().hide()
        widths = [54, 74, 420, 78, 96, 82, 86]
        h_header = self.horizontalHeader()
        h_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h_header.setMinimumSectionSize(44)
        for i, w in enumerate(widths):
            self.setColumnWidth(i, w)
        h_header.setFixedHeight(36)
        self.verticalHeader().setDefaultSectionSize(34)
        self.setMinimumWidth(360)
        self.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        # self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.proxy_model.rowsInserted.connect(self.__on_sync_visual)
        self.proxy_model.layoutChanged.connect(self.__on_sync_visual)
        self.proxy_model.rowsRemoved.connect(self.__on_sync_visual)
        self.proxy_model.modelReset.connect(self.__on_sync_visual)
        # 同时监听 source_model，确保过滤条件排除所有行时 total 仍能正确更新
        self.source_model.rowsInserted.connect(self.__on_sync_visual)
        self.source_model.rowsRemoved.connect(self.__on_sync_visual)
        self.source_model.modelReset.connect(self.__on_sync_visual)

        self.customContextMenuRequested.connect(self.__on_show_context_menu)
        self.context_menu.delete_requested.connect(self._remove_row)

        self.selectionModel().selectionChanged.connect(self.__on_selection_changed)

        self.doubleClicked.connect(self.__on_row_double_clicked)

    def set_controller(self, controller) -> None:
        """更新 Flow 查看控制器并同步到关联菜单。"""
        self.controller = controller
        self.context_menu.controller = controller
        self.context_menu.export_menu.controller = controller

    def get_selected_flows(self) -> list[HTTPFlow]:
        """获取当前选中的 flow 对象列表(单选/多选通用)"""
        flows = []
        for index in self.selectionModel().selectedRows():
            source_index = self.proxy_model.mapToSource(index)
            flow = self.source_model.get_flow(source_index.row())
            if flow:
                flows.append(flow)
        return flows

    @Slot()
    def __on_selection_changed(self, selected):
        """选择变更时触发

        :param selected: 选中的项
        """
        indexes = selected.indexes()
        if indexes:
            index = indexes[0]
            source_index = self.proxy_model.mapToSource(index)
            row = source_index.row()
            data = self.source_model.get_row_data(row)
            self.row_selected.emit(data)

        # 更新统计信息
        QTimer.singleShot(0, self.__emit_stats_updated)

    @Slot(QPoint)
    def __on_show_context_menu(self, pos: QPoint):
        """右键选中打开上下文菜单

        Args:
            pos: 鼠标位置
        """
        index = self.indexAt(pos)
        if not index.isValid():
            return

        source_index = self.proxy_model.mapToSource(index)
        row = source_index.row()
        row_data = self.source_model.get_row_data(row)  # ← 就来自这里
        selected_flows = self.get_selected_flows()
        self.context_menu.update_context(row, row_data, selected_flows)
        self.context_menu.exec(self.viewport().mapToGlobal(pos))

    @Slot()
    def __on_sync_visual(self):
        """视图更新（动态的插入需要）"""
        QTimer.singleShot(0, self.updateSelectedRows)
        QTimer.singleShot(0, self.__emit_stats_updated)

    def __emit_stats_updated(self):
        """发出统计更新信号"""
        # total = 全部抓取流量（不受搜索过滤影响，来自 View._store）
        # shown = 当前可见行（已按 View.set_filter 过滤）
        total = self.controller.total_count() if self.controller else 0
        shown = self.proxy_model.rowCount()
        selected = len(self.selectionModel().selectedRows())
        self.stats_updated.emit(total, shown, selected)

    def emit_stats(self) -> None:
        self.__emit_stats_updated()

    @Slot()
    def clear_all(self):
        """清除所有数据"""
        if self.controller and hasattr(self.controller, "clear_flows"):
            self.controller.clear_flows()
        else:
            self.source_model.clear_data()
        self.clearSelection()
        QTimer.singleShot(0, self.__emit_stats_updated)

    @Slot(int)
    def _remove_row(self, row: int) -> None:
        flow = self.source_model.get_flow(row)
        if flow is None:
            return
        if self.controller and hasattr(self.controller, "remove_flows"):
            self.controller.remove_flows([flow])
        else:
            self.source_model.remove_row(row)

    def set_view(self, view):
        """设置 mitmproxy View 实例

        Args:
            view: mitmproxy.addons.view.View 实例
        """
        self.source_model.set_view(view)

    def on_flow_added(self, flow):
        """处理 View 新增 flow"""
        self.source_model.handle_add(flow)

    def on_flow_updated(self, flow):
        """处理 View 更新 flow"""
        self.source_model.handle_update(flow)

    def on_flow_removed(self, flow, index):
        """处理 View 移除 flow"""
        self.source_model.handle_remove(flow, index)

    def on_view_refreshed(self):
        """处理 View 整体刷新"""
        self.source_model.handle_refresh()

    @Slot()
    def on_locate_selection(self):
        """定位 滑动到选中"""
        index = self.selectionModel().currentIndex()
        if not index.isValid():
            return
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.horizontalScrollBar().setValue(0)

    def selected_row_data(self) -> dict:
        index = self.selectionModel().currentIndex()
        if not index.isValid():
            indexes = self.selectionModel().selectedRows()
            if not indexes:
                return {}
            index = indexes[0]
        source_index = self.proxy_model.mapToSource(index)
        return self.source_model.get_row_data(source_index.row())

    @Slot(QModelIndex)
    def __on_row_double_clicked(self, index: QModelIndex):
        """双击行时触发

        Args:
            index: 被双击的索引
        """
        source_index = self.proxy_model.mapToSource(index)
        row = source_index.row()
        data = self.source_model.get_row_data(row)
        self.row_double_clicked.emit(data)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_responsive_columns(e.size().width())

    def _apply_responsive_columns(self, width: int) -> None:
        narrow = width < 900
        self.setColumnHidden(4, narrow)
        self.setColumnHidden(5, narrow)


class FlowDataPanel(SimpleCardWidget):
    """Flow 数据面板 - 显示请求和响应详情。"""

    collapseRequested = Signal()  # 请求折叠面板

    def __init__(self, parent: QWidget, controller=None):
        super().__init__(parent=parent)
        self.controller = controller  # 保存 controller 引用
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def minimumSizeHint(self) -> QSize:
        """Keep the outer 50/50 split usable despite long inner tab labels."""
        return QSize(220, 220)

    def __init_widget(self):
        """初始化界面组件"""
        # 空
        self.empty_page = QWidget()
        self.empty_label = SubtitleLabel(self.empty_page)
        self.empty_label.setText(self.tr("什么都没有"))
        self.empty_close_button = TransparentToolButton(self.empty_page)  # 空页面的 X
        self.empty_close_button.setIcon(FluentIcon.CLOSE)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 有数据
        self.detail_page = OrientationSplitter(inverted=True)
        self.req_panel = RequestPanel(self.detail_page, self.controller)
        self.res_panel = ResponsePanel(self.detail_page, self.controller)
        self.detail_page.addWidget(self.req_panel)
        self.detail_page.addWidget(self.res_panel)
        self.req_panel.setMinimumSize(220, 220)
        self.res_panel.setMinimumSize(220, 220)
        self.detail_page.setStretchFactor(0, 1)
        self.detail_page.setStretchFactor(1, 1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty_page)  # index 0
        self.stack.addWidget(self.detail_page)  # index 1

        self.setBorderRadius(0)  # ← 去掉圆角，与表格对齐

        self.context_bar = QWidget(self)
        self.context_bar.setFixedHeight(40)
        self.context_method = BodyLabel(self.context_bar)
        self.context_url = BodyLabel(self.context_bar)
        self.context_url.setMinimumWidth(0)
        self.context_url.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.context_url.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.context_status = CaptionLabel(self.context_bar)
        self.context_status.setMinimumWidth(38)
        self.context_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.context_duration = CaptionLabel(self.context_bar)
        self.context_duration.setMinimumWidth(54)
        self.context_duration.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.context_close_button = TransparentToolButton(
            FluentIcon.CLOSE, self.context_bar
        )
        self.context_close_button.setFixedSize(32, 32)
        self.context_close_button.setIconSize(QSize(16, 16))
        self.context_close_button.setToolTip(self.tr("关闭详情"))
        self.context_close_button.setAccessibleName(self.tr("关闭详情"))

        self.__update_close_buttons()

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.context_bar)
        layout.addWidget(self.stack)

        context_layout = QHBoxLayout(self.context_bar)
        context_layout.setContentsMargins(10, 4, 8, 4)
        context_layout.setSpacing(8)
        context_layout.addWidget(self.context_method)
        context_layout.addWidget(self.context_url, 1)
        context_layout.addWidget(self.context_status)
        context_layout.addWidget(self.context_duration)
        context_layout.addWidget(self.context_close_button)

        # 空页面布局：顶部右侧 X + 中间文字
        empty_layout = QVBoxLayout(self.empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)

        # 顶部行：弹簧 + X 按钮（靠右）
        top_layout = QHBoxLayout()
        top_layout.addStretch(1)
        top_layout.addWidget(self.empty_close_button)

        empty_layout.addLayout(top_layout)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_label, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch(1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        CONFIG.layout.valueChanged.connect(self.__update_close_buttons)
        CONFIG.layout.valueChanged.connect(self.__on_layout_changed)
        self.req_panel.close_button.clicked.connect(self.__collapse_panel)
        self.res_panel.close_button.clicked.connect(self.__collapse_panel)
        self.empty_close_button.clicked.connect(self.__collapse_panel)
        self.context_close_button.clicked.connect(self.__collapse_panel)

    @Slot()
    def __update_close_buttons(self):
        """The outer context bar owns the single detail close affordance."""
        self.req_panel.close_button.hide()
        self.res_panel.close_button.hide()

    @Slot()
    def __on_layout_changed(self):
        """布局方向切换时，延迟重新设置 50:50 比例"""
        if self.stack.currentIndex() == 1:  # 详情页可见
            QTimer.singleShot(100, self.__apply_detail_equal_sizes)

    @Slot()
    def __collapse_panel(self):
        """折叠面板"""
        self.collapseRequested.emit()

    def set_controller(self, controller) -> None:
        """更新 Flow 查看控制器并同步到请求、响应面板。"""
        self.controller = controller
        self.req_panel.controller = controller
        self.res_panel.controller = controller

    def set_data(self, data: dict):
        """有数据时调用，切换到详情页并填充

        Args:
            data: 数据字典
        """
        self.req_panel.set_data(data)  # 请求面板
        self.res_panel.set_data(data)  # 响应面板
        self._update_context_bar(data)
        self.stack.setCurrentIndex(1)
        # detail_page 刚切为当前页时尚未完成 layout，width 可能未就绪，
        # 故延迟到下一事件循环（布局算完）再设一次 50:50，避免抖动循环。
        QTimer.singleShot(0, self.__apply_detail_equal_sizes)

    def eventFilter(self, obj, e):
        """保留空实现以兼容父类；detail 页比例已改由 singleShot(0) 单次设定。"""
        return super().eventFilter(obj, e)

    def __apply_detail_equal_sizes(self):
        """确保请求面板和响应面板严格 50:50"""
        self.detail_page.set_equal_sizes()

    def _update_context_bar(self, data: dict) -> None:
        method = str(data.get("Method", "—"))
        url = str(data.get("URL", "—"))
        status = str(data.get("Status Code", "等待中"))
        duration = str(data.get("Duration", ""))
        self.context_method.setText(method)
        self.context_url.setText(url)
        self.context_url.setToolTip(url)
        self.context_status.setText(status)
        self.context_duration.setText(duration)
        status_kind = "neutral"
        if status == "Error":
            status_kind = "error"
        elif status.isdigit():
            code = int(status)
            if 200 <= code < 300:
                status_kind = "success"
            elif 300 <= code < 400:
                status_kind = "info"
            elif 400 <= code < 500:
                status_kind = "warning"
            elif code >= 500:
                status_kind = "error"
        colors = {
            "success": "#2e9b4d",
            "info": "#2878c8",
            "warning": "#b77900",
            "error": "#d13438",
            "neutral": "#7a7a7a",
        }
        self.context_status.setStyleSheet(
            f"color: {colors[status_kind]}; font-weight: 600;"
        )


class FlowViewerPane(OrientationSplitter):
    """Shared Flow table/detail viewer with consistent interaction semantics."""

    def __init__(
        self,
        parent: QWidget | None = None,
        controller=None,
        capabilities: FlowViewCapabilities | None = None,
    ) -> None:
        super().__init__(parent=parent)
        self.controller = controller
        self._capture_mode = capabilities is None or capabilities.can_delete
        self._capture_context = {
            "capture_state": "stopped",
            "endpoint": "",
            "total_count": 0,
            "shown_count": 0,
            "active_filter_count": 0,
        }
        self.table_container = QWidget(self)
        self.table_stack = QStackedWidget(self.table_container)
        self.table = FlowDataTable(self.table_container, controller, capabilities)
        self.empty_state = FlowEmptyState(self.table_container)
        self.table_stack.addWidget(self.table)
        self.table_stack.addWidget(self.empty_state)
        container_layout = QVBoxLayout(self.table_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(self.table_stack)
        self.panel = FlowDataPanel(self, controller)
        self.addWidget(self.table_container)
        self.addWidget(self.panel)
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 0)
        self.collapse_panel()

        self.table.row_selected.connect(self._on_row_selected)
        self.table.row_double_clicked.connect(self._on_row_double_clicked)
        self.panel.collapseRequested.connect(self.collapse_panel)
        self.table.stats_updated.connect(self._on_stats_updated)
        self._refresh_empty_state()

    def set_controller(self, controller) -> None:
        self.controller = controller
        self.table.set_controller(controller)
        self.panel.set_controller(controller)
        self._refresh_empty_state()

    def is_panel_expanded(self) -> bool:
        sizes = self.sizes()
        return len(sizes) >= 2 and sizes[1] > 0

    @Slot(dict)
    def _on_row_selected(self, data: dict) -> None:
        """Update details only when the outer detail panel is already open."""
        if self.is_panel_expanded():
            self.panel.set_data(data)

    @Slot(dict)
    def _on_row_double_clicked(self, data: dict) -> None:
        """Open details and normalize the outer splitter to 50/50."""
        self.panel.set_data(data)
        QTimer.singleShot(0, self._apply_equal_sizes)

    @Slot()
    def collapse_panel(self) -> None:
        self.collapse(1)

    def _apply_equal_sizes(self) -> None:
        self.set_equal_sizes()

    @Slot()
    def open_selected(self) -> None:
        data = self.table.selected_row_data()
        if data:
            self._on_row_double_clicked(data)

    def set_capture_context(
        self,
        *,
        capture_state: object,
        endpoint: str,
        total_count: int,
        shown_count: int,
        active_filter_count: int,
    ) -> None:
        value = getattr(capture_state, "value", capture_state)
        self._capture_context = {
            "capture_state": str(value),
            "endpoint": endpoint,
            "total_count": total_count,
            "shown_count": shown_count,
            "active_filter_count": active_filter_count,
        }
        self._refresh_empty_state()

    @Slot(int, int, int)
    def _on_stats_updated(self, total: int, shown: int, _selected: int) -> None:
        self._capture_context["total_count"] = total
        self._capture_context["shown_count"] = shown
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        total = int(self._capture_context["total_count"])
        shown = int(self._capture_context["shown_count"])
        state = str(self._capture_context["capture_state"])
        filters = int(self._capture_context["active_filter_count"])

        if shown > 0:
            self.table_stack.setCurrentWidget(self.table)
            return

        self.table_stack.setCurrentWidget(self.empty_state)
        if total > 0:
            self.empty_state.set_text(
                self.tr("没有匹配结果"),
                self.tr("当前有 {} 个有效条件").format(filters),
            )
        elif state in ("running", "starting"):
            self.empty_state.set_text(
                self.tr("等待流量"),
                str(self._capture_context["endpoint"]),
            )
        else:
            subtitle = (
                self.tr("代理已停止")
                if self._capture_mode
                else self.tr("当前会话没有 HTTP 流量")
            )
            self.empty_state.set_text(self.tr("暂无流量"), subtitle)


class FlowEmptyState(QWidget):
    """Small neutral empty state for the shared Flow table area."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = IconWidget(FluentIcon.WIFI, self)
        self.icon.setFixedSize(32, 32)
        self.title = BodyLabel(self)
        self.subtitle = CaptionLabel(self)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addStretch(1)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(8)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addStretch(1)
        self.set_text(self.tr("暂无流量"), self.tr("代理已停止"))

    def set_text(self, title: str, subtitle: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)


class CookieWidget(QWidget):
    """Cookie 显示组件 - 以 TreeWidget 显示键值对，支持复制"""

    def __init__(self, parent=None):
        """初始化 Cookie 组件

        Args:
            parent: 父组件
        """
        super().__init__(parent)
        self.cookies = {}
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.tree = TreeWidget()
        self.tree.setHeaderLabels(["Name", "Value"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setIndentation(0)
        self.tree.header().setVisible(False)

        self.copy_button = TransparentToolButton(self)
        self.copy_button.setIcon(FluentIcon.COPY)
        self.copy_button.setToolTip(self.tr("复制 Cookie"))
        self.copy_button.installEventFilter(
            ToolTipFilter(self.copy_button, 1000, ToolTipPosition.TOP)
        )

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(4, 2, 4, 2)
        btn_layout.addWidget(self.copy_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addWidget(self.tree, 1)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.copy_button.clicked.connect(self.__on_copy)

    def set_cookies(self, cookies: dict | list[dict]):
        """设置 cookie 数据 {name: value, ...}

        Args:
            cookies: Cookie 字典
        """
        if isinstance(cookies, list):
            normalized = {
                str(item.get("name", "")): str(item.get("value", ""))
                for item in cookies
                if item.get("name")
            }
        else:
            normalized = cookies
        self.cookies = normalized
        self.tree.clear()

        for key, value in normalized.items():
            item = QTreeWidgetItem(self.tree)
            item.setText(0, str(key))
            item.setText(1, str(value))
            item.setTextAlignment(0, Qt.AlignmentFlag.AlignLeft)
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignLeft)

        self.tree.setColumnWidth(0, 150)
        self.tree.header().setStretchLastSection(True)

    @Slot()
    def __on_copy(self):
        """复制 Cookie 到剪贴板"""
        if not self.cookies:
            show_warning(self.tr("提示"), self.tr("没有可复制的 Cookie"), self.window())
            return

        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        QApplication.clipboard().setText(cookie_str)
        show_success(self.tr("成功"), self.tr("Cookie 已复制到剪贴板"), self.window())


class RequestPanel(TabPanel):
    """请求面板 - 显示请求详情，包含总览、原始、请求头、参数、请求体、Cookies"""

    def __init__(self, parent=None, controller=None):
        """初始化请求面板

        Args:
            parent: 父组件
            controller: 抓包控制器实例
        """
        super().__init__(parent)
        self.datas: dict | None = None
        self.controller = controller  # 保存 controller 引用

        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        """初始化界面组件"""
        # 注意：所有通过 addTab 加入 stacked 的子组件，不要传 parent=self，
        # 否则 addTab 内部 QStackedWidget.addWidget() 会触发二次 reparenting，
        # 导致内部工具栏/行号区几何偏移（左上角错位）。
        self.overview = Overview(self)

        self.raw_edit = ToolPlainTextEdit()
        self.raw_edit.set_read_only(True)
        self.params_widget = ItemDualPanel()
        self.params_widget.set_read_only(True)
        self.header_card = ItemDualPanel()
        self.header_card.set_read_only(True)

        self.body_card = JsonDualPanel()
        self.body_card.set_read_only(True)

        self.cookie_widget = CookieWidget()

        self.cookie_card = SimpleCardWidget()
        self.cookie_card.setBorderRadius(0)
        cookie_layout = QVBoxLayout(self.cookie_card)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.addWidget(self.cookie_widget)

    def __init_layout(self):
        """初始化布局结构"""
        self.addTab("概览", self.overview, self.tr("概览"))
        self.addTab("Headers", self.header_card, "Headers")
        self.addTab("Params", self.params_widget, "Params")
        self.addTab("Cookies", self.cookie_card, "Cookies")
        self.addTab("Body", self.body_card, "Body")
        self.addTab("Raw", self.raw_edit, "Raw")
        self.setTabFontSize(12)

    def set_data(self, data: dict):
        """填充请求数据

        Args:
            data: 数据字典（已由 mitmproxy 阶段预解析结构化字段）
        """
        self.datas = data  # 保存数据，供 _fill_raw 使用

        # 总览 tab 始终用完整数据
        self.overview.set_data(data)

        headers = data.get("Request Headers", {})
        self.header_card.set_items(headers)

        flow_id = data.get("Connection ID", "")
        content_type = data.get("Request Content-Type", "")
        self._set_body_tab_label(content_type)
        body = data.get("Request Body", b"")
        self._fill_raw(body, flow_id)
        self._fill_body(data)

        params = data.get("Request Params", {})
        self.params_widget.set_items(params)

        cookies = data.get("Request Cookies", {})
        self.cookie_widget.set_cookies(cookies)

    def _fill_raw(self, body: bytes, flow_id: str = ""):
        """生成完整的原始HTTP请求格式

        Args:
            body: 请求体
            flow_id: 流 ID
        """
        # 尝试使用controller获取原始HTTP请求
        if self.controller and flow_id:
            raw_data = self.controller.get_raw_request(flow_id)
            if raw_data:
                if isinstance(raw_data, bytes):
                    text = raw_data.decode("utf-8", errors="replace")
                else:
                    text = str(raw_data)
                self.raw_edit.set_text(text)
                return

        # 如果获取失败，使用手动构建的格式
        if not self.datas:
            return
        raw_lines = []

        # 请求行
        method = self.datas.get("Method", "GET")
        path = self.datas.get("Path", "/")
        http_version = self.datas.get("HTTP Version", "HTTP/1.1")
        raw_lines.append(f"{method} {path} {http_version}")

        # 请求头
        headers = self.datas.get("Request Headers", {})
        for key, value in headers.items():
            raw_lines.append(f"{key}: {value}")

        # 空行分隔头部和body
        raw_lines.append("")

        # body内容
        if body:
            if isinstance(body, bytes):
                # models 阶段已用 Message.get_text 按 charset 解码好，直接消费
                text = self.datas.get("Request Body Text") or ""
            else:
                text = str(body)
            raw_lines.append(text)

        self.raw_edit.set_text("\n".join(raw_lines))

    def _fill_body(self, data: dict):
        """填充请求/响应体（消费 mitmproxy 阶段预解析字段，不再重复解码/格式化）

        Args:
            data: 完整数据字典，含 Request Body Pretty / Request Body Text
        """
        text = data.get("Request Body Pretty")
        if text is None:
            text = data.get("Request Body Text") or ""
        lang = _infer_body_lang(data.get("Request Content-Type", ""))
        self.body_card.set_text(text, lang=lang)

    def _set_body_tab_label(self, content_type: str) -> None:
        item = self.pivot.items.get("Body")
        if item is None:
            return
        label = _body_type_label(content_type)
        item.setText(f"Body · {label}" if label else "Body")
        item.adjustSize()


class ResponsePanel(TabPanel):
    """响应面板 - 包含原始、响应头、响应体三个标签"""

    def __init__(self, parent=None, controller=None):
        """初始化响应面板

        Args:
            parent: 父组件
            controller: 抓包控制器实例
        """
        super().__init__(parent)
        self.datas: dict | None = None
        self.controller = controller  # 保存 controller 引用

        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        """初始化界面组件"""
        self.raw_edit = ToolPlainTextEdit()
        self.raw_edit.set_read_only(True)

        self.body_card = JsonDualPanel()
        self.body_card.set_read_only(True)

        self.header_card = ItemDualPanel()
        self.header_card.set_read_only(True)

        self.cookie_widget = CookieWidget()
        self.cookie_card = SimpleCardWidget()
        self.cookie_card.setBorderRadius(0)
        cookie_layout = QVBoxLayout(self.cookie_card)
        cookie_layout.setContentsMargins(0, 0, 0, 0)
        cookie_layout.addWidget(self.cookie_widget)

    def __init_layout(self):
        """初始化布局结构"""
        self.addTab("Headers", self.header_card, "Headers")
        self.addTab("Cookies", self.cookie_card, "Cookies")
        self.addTab("Body", self.body_card, "Body")
        self.addTab("Raw", self.raw_edit, "Raw")
        self.setTabFontSize(12)

    def set_data(self, data: dict):
        """填充响应数据

        Args:
            data: 数据字典（已由 mitmproxy 阶段预解析结构化字段）
        """
        self.datas = data

        # 响应头
        headers = data.get("Response Headers", {})
        self.header_card.set_items(headers)
        self.cookie_widget.set_cookies(data.get("Response Cookies", {}))

        # 响应体（消费预解析字段）
        flow_id = data.get("Connection ID", "")
        content_type = data.get("Response Content-Type", "")
        self._set_body_tab_label(content_type)
        body = data.get("Response Body", b"")
        self._fill_raw(body, flow_id)
        self._fill_body(data)

    def _fill_raw(self, body: bytes, flow_id: str = ""):
        """生成完整的原始HTTP响应格式

        Args:
            body: 响应体
            flow_id: 流 ID
        """
        # 尝试使用controller获取原始HTTP响应
        if self.controller and flow_id:
            try:
                raw_data = self.controller.get_raw_response(flow_id)

                if raw_data:
                    # 如果成功获取到原始数据，直接使用
                    # raw_response 返回的是「状态行+响应头+空行+body」完整报文，
                    # 直接按文本解码即可，不要走 body 解码器。
                    if isinstance(raw_data, bytes):
                        text = raw_data.decode("utf-8", errors="replace")
                    else:
                        text = str(raw_data)
                    self.raw_edit.set_text(text)
                    return
            except (AttributeError, ValueError, TypeError, RuntimeError) as e:
                print(f"获取原始HTTP数据失败: {e}")

        # 如果获取失败，使用手动构建的格式
        if not self.datas:
            return
        raw_lines = []

        # 响应状态行
        status_code = self.datas.get("Status Code", 200)
        reason = self.datas.get("Reason", "OK")
        http_version = self.datas.get("Response HTTP Version", "HTTP/1.1")
        raw_lines.append(f"{http_version} {status_code} {reason}")

        # 响应头
        headers = self.datas.get("Response Headers", {})
        for key, value in headers.items():
            raw_lines.append(f"{key}: {value}")

        # 空行分隔头部和body
        raw_lines.append("")

        # body内容
        if body:
            if isinstance(body, bytes):
                # models 阶段已用 Message.get_text 按 charset 解码好，直接消费。
                # 切勿在这里再解码一次：body 是解压后的内容，重跑解压会产乱码。
                text = self.datas.get("Response Body Text") or ""
            else:
                text = str(body)
            raw_lines.append(text)

        self.raw_edit.set_text("\n".join(raw_lines))

    def _fill_body(self, data: dict):
        """填充响应体（消费 mitmproxy 阶段预解析字段，不再重复解码/格式化）

        Args:
            data: 完整数据字典，含 Response Body Pretty / Response Body Text
        """
        text = data.get("Response Body Pretty")
        if text is None:
            text = data.get("Response Body Text") or ""
        lang = _infer_body_lang(data.get("Response Content-Type", ""))
        self.body_card.set_text(text, lang=lang)

    def _set_body_tab_label(self, content_type: str) -> None:
        item = self.pivot.items.get("Body")
        if item is None:
            return
        label = _body_type_label(content_type)
        item.setText(f"Body · {label}" if label else "Body")
        item.adjustSize()


class Overview(SimpleCardWidget):
    """总览组件 - 显示 URL 和基本信息"""

    def __init__(self, parent: "RequestPanel"):
        """初始化总览组件

        Args:
            parent: 父组件，通常是 RequestPanel
        """
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        """初始化界面组件"""
        self.setBorderRadius(0)
        self.data = OverviewTree(self)

    def __init_layout(self):
        """初始化布局结构"""
        self.v_layout = QVBoxLayout(self)
        self.v_layout.addWidget(self.data)

    def set_data(self, data: dict):
        """设置数据

        Args:
            data: 数据字典
        """
        self.data.set_data(data)  # 填充树


class OverviewTree(TreeWidget):
    """总览树形控件 - 显示请求/响应的详细信息"""

    # 基本信息字段
    FIELDS: ClassVar[list[tuple[str, FieldKey]]] = [
        (
            "状态",
            lambda d: {
                "request_headers": "等待中...",
                "request": "请求已发送",
                "response_headers": "已收到响应头",
                "complete": "Completed",
                "error": "Error",
            }.get(d.get("state", ""), "未知"),
        ),
        ("方法", "Method"),
        ("协议", "Protocol"),
        ("Code", "Status Code"),
        ("服务器地址", "Server Address"),
        ("Keep Alive", "Keep Alive"),
        ("流", "id"),
        ("Content Type", "Response Content-Type"),
        ("代理协议", "Proxy Protocol"),
    ]

    # 连接信息
    CONN_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("ID", "Connection ID"),
        ("时间", "Connection Time"),
    ]
    CONN_FRONT_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("客户端 地址", "Front Client Address"),
        ("客户端 端口", "Front Client Port"),
        ("服务端 地址", "Front Server Address"),
        ("服务端 端口", "Front Server Port"),
    ]
    CONN_BACK_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("客户端 地址", "Back Client Address"),
        ("客户端 端口", "Back Client Port"),
        ("服务端 地址", "Back Server Address"),
        ("服务端 端口", "Back Server Port"),
    ]

    # TLS 信息
    TLS_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("版本", "TLS Version"),
        ("SNI", "TLS SNI"),
        ("ALPN", "TLS ALPN Offers"),
        ("选择ALPN", "TLS ALPN Selected"),
        ("加密算法列表", "TLS Cipher List"),
        ("选择算法", "TLS Cipher"),
    ]

    # 证书信息 - Subject
    CERT_SUBJECT_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("Common Name", "Subject Common Name"),
        ("国家", "Subject Country"),
        ("省（州）", "Subject State"),
        ("地区", "Subject Locality"),
        ("组织", "Subject Organization"),
        ("单位", "Subject Organizational Unit"),
    ]

    # 证书信息 - 签发者
    CERT_ISSUER_FIELDS: ClassVar[list[tuple[str, str]]] = [
        ("Common Name", "Issuer Common Name"),
        ("国家", "Issuer Country"),
        ("省（州）", "Issuer State"),
        ("地区", "Issuer Locality"),
        ("组织", "Issuer Organization"),
        ("单位", "Issuer Organizational Unit"),
    ]

    # 证书详细信息
    CERT_DETAIL_FIELDS: ClassVar[list[tuple[str, FieldKey]]] = [
        ("开始时间", "Not Before"),
        ("截止时间", "Not After"),
        ("指纹", "Fingerprint SHA1"),
        ("序列号", "Serial Number Hex"),
    ]

    # 时间信息
    TIME_FIELDS: ClassVar[list[tuple[str, FieldKey]]] = [
        ("请求开始", lambda d: _format_time(d.get("req_time"))),
        ("请求结束", lambda d: _format_time(d.get("req_timestamp_end"))),
        (
            "请求时长",
            lambda d: (
                f"{d.get('req_duration', 0):.1f} ms"
                if d.get("req_duration") is not None
                else "-"
            ),
        ),
        ("响应开始", lambda d: _format_time(d.get("res_timestamp_start"))),
        ("响应结束", lambda d: _format_time(d.get("res_time"))),
        (
            "响应时长",
            lambda d: (
                f"{d.get('res_duration', 0):.1f} ms"
                if d.get("res_duration") is not None
                else "-"
            ),
        ),
        ("总时长", "Duration"),
    ]

    # 大小信息
    SIZE_FIELDS: ClassVar[list[tuple[str, FieldKey]]] = [
        (
            "请求",
            lambda d: format_bytes(d.get("req_size", 0) + d.get("req_headers_size", 0)),
        ),
        ("- 请求头", lambda d: format_bytes(d.get("req_headers_size", 0))),
        ("- 请求体", lambda d: format_bytes(d.get("req_size", 0))),
        (
            "响应",
            lambda d: format_bytes(d.get("res_size", 0) + d.get("res_headers_size", 0)),
        ),
        ("- 响应头", lambda d: format_bytes(d.get("res_headers_size", 0))),
        ("- 响应体", lambda d: format_bytes(d.get("res_size", 0))),
        ("总计", lambda d: format_bytes(d.get("total_size", 0))),
    ]

    def __init__(self, parent: QWidget):
        """初始化总览树形控件

        Args:
            parent: 父组件
        """
        super().__init__(parent)
        self.__init_widget()

    def __init_widget(self):
        """初始化界面组件"""
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setColumnWidth(0, 160)

    def set_data(self, data: dict):
        """填充扁平数据，只显示有值的字段

        Args:
            data: 数据字典
        """
        self.clear()

        # ── 基本信息 ──
        for label, key_or_func in self.FIELDS:
            if isinstance(key_or_func, str):
                value = data.get(key_or_func)
            else:
                value = key_or_func(data)

            # 跳过空值
            if value in (None, "", "N/A", "-"):
                continue

            item = QTreeWidgetItem(self)
            item.setText(0, label)
            item.setText(1, str(value))
            item.setTextAlignment(
                0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            item.setTextAlignment(
                1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        # ── 连接信息（仅在有数据时显示） ──
        has_conn = any(data.get(k) for _, k in self.CONN_FIELDS)
        if has_conn:
            conn_parent = QTreeWidgetItem(self)
            conn_parent.setText(0, self.tr("连接"))

            bold_font = QFont()
            bold_font.setBold(True)
            conn_parent.setFont(0, bold_font)

            # ID / 时间（平级）
            for label, key in self.CONN_FIELDS:
                value = data.get(key)
                if value in (None, "", "N/A", "-"):
                    continue
                item = QTreeWidgetItem(conn_parent)
                item.setText(0, label)
                item.setText(1, str(value))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )

            # 前端（子级父节点）
            front_items = [
                (label, data.get(key))
                for label, key in self.CONN_FRONT_FIELDS
                if data.get(key) not in (None, "", "N/A", "-")
            ]
            if front_items:
                front_parent = QTreeWidgetItem(conn_parent)
                front_parent.setText(0, self.tr("前端"))
                front_parent.setFont(0, bold_font)

                for label, value in front_items:
                    item = QTreeWidgetItem(front_parent)
                    item.setText(0, label)
                    item.setText(1, str(value))
                    item.setTextAlignment(
                        0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setTextAlignment(
                        1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )

            # 后端（子级父节点）
            back_items = [
                (label, data.get(key))
                for label, key in self.CONN_BACK_FIELDS
                if data.get(key) not in (None, "", "N/A", "-")
            ]
            if back_items:
                back_parent = QTreeWidgetItem(conn_parent)
                back_parent.setText(0, self.tr("后端"))
                back_parent.setFont(0, bold_font)

                for label, value in back_items:
                    item = QTreeWidgetItem(back_parent)
                    item.setText(0, label)
                    item.setText(1, str(value))
                    item.setTextAlignment(
                        0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setTextAlignment(
                        1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )

        # ── TLS 信息（仅在有数据时显示） ──
        has_tls = any(data.get(k) for _, k in self.TLS_FIELDS)
        if has_tls:
            tls_parent = QTreeWidgetItem(self)
            tls_parent.setText(0, self.tr("TLS"))

            bold_font = QFont()
            bold_font.setBold(True)
            tls_parent.setFont(0, bold_font)

            for label, key in self.TLS_FIELDS:
                value = data.get(key)

                # 跳过空值
                if value in (None, "", "N/A", "-"):
                    continue

                if isinstance(value, list):
                    # 列表类字段：如 ALPN Offers、Cipher List
                    count_item = QTreeWidgetItem(tls_parent)
                    count_item.setText(0, label)
                    count_item.setText(1, f"{len(value)}项")
                    count_item.setFont(0, bold_font)

                    for i, entry in enumerate(value):
                        sub_item = QTreeWidgetItem(count_item)
                        sub_item.setText(0, f"  - 算法{i + 1}")
                        sub_item.setText(1, str(entry))
                        sub_item.setTextAlignment(
                            0,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        )
                        sub_item.setTextAlignment(
                            1,
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                        )
                else:
                    # 普通字段
                    item = QTreeWidgetItem(tls_parent)
                    item.setText(0, label)
                    item.setText(1, str(value))
                    item.setTextAlignment(
                        0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )
                    item.setTextAlignment(
                        1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                    )

        # ── 证书信息（仅在有数据时显示） ──
        has_cert = any(
            data.get(k)
            for _, k in self.CERT_SUBJECT_FIELDS
            + self.CERT_ISSUER_FIELDS
            + self.CERT_DETAIL_FIELDS
        )
        if has_cert:
            cert_parent = QTreeWidgetItem(self)
            cert_parent.setText(0, self.tr("服务端证书"))

            bold_font = QFont()
            bold_font.setBold(True)
            cert_parent.setFont(0, bold_font)

            underline_font = QFont()
            underline_font.setUnderline(True)

            # ── Subject 信息 ──
            subject_parent = QTreeWidgetItem(cert_parent)
            subject_parent.setText(0, "Subject")
            subject_parent.setFont(0, underline_font)

            for label, key in self.CERT_SUBJECT_FIELDS:
                value = data.get(key, "")
                item = QTreeWidgetItem(subject_parent)
                item.setText(0, f"- {label}")
                item.setText(1, str(value) if value else "-")
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )

            # ── 签发者信息 ──
            issuer_parent = QTreeWidgetItem(cert_parent)
            issuer_parent.setText(0, "签发者")
            issuer_parent.setFont(0, underline_font)

            for label, key in self.CERT_ISSUER_FIELDS:
                value = data.get(key, "")
                item = QTreeWidgetItem(issuer_parent)
                item.setText(0, f"- {label}")
                item.setText(1, str(value) if value else "-")
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )

            # ── 证书详细信息（平级） ──
            for label, key_or_func in self.CERT_DETAIL_FIELDS:
                if isinstance(key_or_func, str):
                    value = data.get(key_or_func, "")
                else:
                    value = key_or_func(data)
                if value in (None, "", "N/A", "-"):
                    continue
                item = QTreeWidgetItem(cert_parent)
                item.setText(0, label)
                item.setText(1, str(value))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )

        # ── 时间信息（仅在有数据时显示） ──
        has_time = any(
            data.get(k) is not None
            for k in (
                "req_time",
                "req_timestamp_end",
                "req_duration",
                "res_timestamp_start",
                "res_time",
                "res_duration",
                "Duration",
            )
        )
        if has_time:
            time_parent = QTreeWidgetItem(self)
            time_parent.setText(0, self.tr("时间"))

            bold_font = QFont()
            bold_font.setBold(True)
            time_parent.setFont(0, bold_font)

            for label, key_or_func in self.TIME_FIELDS:
                if isinstance(key_or_func, str):
                    value = data.get(key_or_func)
                else:
                    value = key_or_func(data)

                if value in (None, "", "N/A", "-"):
                    continue

                item = QTreeWidgetItem(time_parent)
                item.setText(0, label)
                item.setText(1, str(value))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )

        # ── 大小信息（仅在有数据时显示） ──
        has_size = any(
            data.get(k) is not None
            for k in (
                "req_size",
                "req_headers_size",
                "res_size",
                "res_headers_size",
                "total_size",
            )
        )
        if has_size:
            size_parent = QTreeWidgetItem(self)
            size_parent.setText(0, self.tr("大小"))

            bold_font = QFont()
            bold_font.setBold(True)
            size_parent.setFont(0, bold_font)

            for label, key_or_func in self.SIZE_FIELDS:
                if isinstance(key_or_func, str):
                    value = data.get(key_or_func)
                else:
                    value = key_or_func(data)

                if value in (None, "", "N/A", "-"):
                    continue

                item = QTreeWidgetItem(size_parent)
                item.setText(0, label)
                item.setText(1, str(value))
                item.setTextAlignment(
                    0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                item.setTextAlignment(
                    1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )


class FlowContextMenu(RoundMenu):
    """Flow 上下文菜单 - 提供复制、删除、查看等操作。"""

    delete_requested = Signal(int)  # 删除请求信号
    replay_file_requested = Signal()  # 从文件回放请求信号

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
            parent=self, icon=FluentIcon.SYNC, text=self.tr("重发")
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
        self.export_menu = FlowExportMenu(self, self.controller)
        self.view_menu = FlowSubViewMenu(self)

    def __init_action(self):
        """初始化菜单动作"""
        self.addMenu(self.view_menu)
        if self.capabilities.can_replay:
            self.addAction(self.client_replay_action)
            self.addAction(self.replay_from_file_action)
        self.addMenu(self.export_menu)
        if self.capabilities.can_delete:
            self.addAction(self.delete_action)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.client_replay_action.triggered.connect(self.__on_client_replay_triggered)
        self.replay_from_file_action.triggered.connect(self.replay_file_requested.emit)
        self.delete_action.triggered.connect(self.__on_delete_triggered)
        self.view_menu.urlViewRequested.connect(self.__show_url_window)

    def _refresh_replay_label(self) -> None:
        """根据当前选中数量刷新重发动作文案：单选=重发，多选=重发 N 条。"""
        count = len(self.flows)
        if count <= 1:
            self.client_replay_action.setText(self.tr("重发"))
        else:
            self.client_replay_action.setText(self.tr("重发 {} 条").format(count))

    @Slot()
    def __on_delete_triggered(self):
        """删除动作触发时"""
        if self.row_index != -1:
            self.delete_requested.emit(self.row_index)

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
                self.tr("警告"), self.tr("请先选中要导出的流量"), self.main_window
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
