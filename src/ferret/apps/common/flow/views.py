from PySide6.QtCore import (
    QModelIndex,
    QPoint,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    TableView,
)

# 详情面板搬去 detail.py，但两个挂载点（capture / session）照旧从 views 导入 ——
# `FlowViewerPane` 自己就要用，这一行同时当转出口。
from ferret.apps.common.flow.detail import FlowDataPanel
from ferret.apps.common.flow.menus import (
    FlowContextMenu,
    FlowExportMenu,  # noqa: F401  re-export：菜单搬去 menus.py，外部照旧从 views 导入
    FlowSubViewMenu,  # noqa: F401
)
from ferret.apps.common.flow.models import FlowProxyModel, FlowTableModel
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.splitter import OrientationSplitter
from ferret.core.log import get_logger
from ferret.core.mitm import HTTPFlow

log = get_logger("flow")


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
        widths = [80, 80, 420, 65, 100, 80, 80]
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

    def row_detail(self, row: int) -> dict:
        """行号 → 详情字典，由 controller 在 mitm 线程内构建。

        改造前这一步是 `FlowTableModel._build_row_data`，在 **Qt 线程**上直接读活
        flow（AGENTS.md §3 的红线），还顺手做了 body 美化和证书解析。现在表格模型
        只管七列，详情整份由 `FlowViewController.flow_detail` 产出。

        没有 controller 只发生在裸构造 `FlowViewerPane()` 的场合（测试）——
        此时没有任何流量可查，空字典就是正确答案。
        """
        flow = self.source_model.get_flow(row)
        if flow is None or self.controller is None:
            return {}
        return self.controller.flow_detail(flow.id)

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
            data = self.row_detail(row)
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
        row_data = self.row_detail(row)  # ← 就来自这里
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
        return self.row_detail(source_index.row())

    @Slot(QModelIndex)
    def __on_row_double_clicked(self, index: QModelIndex):
        """双击行时触发

        Args:
            index: 被双击的索引
        """
        source_index = self.proxy_model.mapToSource(index)
        row = source_index.row()
        data = self.row_detail(row)
        self.row_double_clicked.emit(data)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_responsive_columns(e.size().width())

    def _apply_responsive_columns(self, width: int) -> None:
        narrow = width < 900
        self.setColumnHidden(4, narrow)
        self.setColumnHidden(5, narrow)


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
        self.panel = FlowDataPanel(self, controller, capabilities)
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
            if not self.panel.isVisible():
                self.panel.setVisible(True)
                self.collapse_panel()
            return

        self.table_stack.setCurrentWidget(self.empty_state)
        self.panel.setVisible(False)
        if total > 0:
            self.empty_state.set_text(
                self.tr("No matches"),
                self.tr("{} active condition(s)").format(filters),
            )
        elif state in ("running", "starting"):
            self.empty_state.set_text(
                self.tr("Waiting for traffic"),
                str(self._capture_context["endpoint"]),
            )
        else:
            subtitle = (
                self.tr("Proxy stopped")
                if self._capture_mode
                else self.tr("This session has no HTTP traffic")
            )
            self.empty_state.set_text(self.tr("No traffic yet"), subtitle)


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
        self.set_text(self.tr("No traffic yet"), self.tr("Proxy stopped"))

    def set_text(self, title: str, subtitle: str) -> None:
        self.title.setText(title)
        self.subtitle.setText(subtitle)
