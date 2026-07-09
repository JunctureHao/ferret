from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QFont,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    MessageBoxBase,
    SimpleCardWidget,
    SpinBox,
    SubtitleLabel,
    TableView,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    TreeWidget,
)

from ferret.config.settings import CONFIG
from ferret.controllers.capture_controller import CaptureController
from ferret.core.model import PacketProxyModel, PacketTableModel
from ferret.utils.http_parser import (
    format_bytes,
    format_time,
)
from ferret.views.common.edit import JsonDualPanel, KVDualPanel, ToolPlainTextEdit
from ferret.views.common.filter import MultiFilterManager
from ferret.views.common.icon import BaseIcon
from ferret.views.common.info_bar import show_success, show_warning
from ferret.views.common.panel import TabPanel
from ferret.views.common.splitter import (
    OrientationSplitter,
)
from ferret.views.interface.capture.packet_menu import PacketContextMenu

if TYPE_CHECKING:
    from ferret.views.window import MainWindow
FieldKey = str | Callable[["dict"], str]


def _infer_body_lang(content_type: str) -> str:
    """根据 Content-Type 推断 body 高亮语言。"""
    ct = (content_type or "").lower()
    if "json" in ct:
        return "json"
    if "xml" in ct or "html" in ct:
        return "xml"
    return "http"


class CapturesInterface(SimpleCardWidget):
    """抓包主界面 - 包含工具栏、搜索面板和内容区域"""

    def __init__(self, parent: "MainWindow | None" = None):
        """初始化抓包界面

        Args:
            parent: 父窗口，通常是 MainWindow
        """
        super().__init__(parent)
        self.setObjectName("CapturesInterface")
        self.controller = CaptureController(self)

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.toolbar = CapturesToolBar(self)
        self.content = CapturesContentArea(self, self.controller)

    def __init_layout(self):
        """初始化布局结构"""
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.main_layout.addWidget(self.toolbar)
        self.main_layout.addWidget(self.content)

    def __connect_signal_to_slot(self):
        """协调层：连接组件业务信号到 Controller"""
        # 工具栏业务信号 → Controller
        self.toolbar.captureToggled.connect(self.__on_capture_toggled)

        # 简单点击：直接连接按钮原生信号
        self.toolbar.proxy_setting_btn.clicked.connect(self.__show_proxy_port_dialog)
        self.toolbar.locate_selection_btn.clicked.connect(
            self.content.table.on_locate_selection
        )
        self.toolbar.captures_delete_btn.clicked.connect(self.content.table.clear_all)

        # Controller 状态信号 → UI 更新
        self.controller.captureStateChanged.connect(self.__on_capture_state_changed)
        self.controller.packet_received.connect(
            self.content.table.source_model.set_data
        )
        self.controller.capture_started.connect(self.content.table.set_traffic_addon)

        # 搜索面板（通过 toolbar 暴露的信号）
        self.toolbar.conditionsChanged.connect(self.__on_search_changed)

        # 统计信息更新
        self.content.table.stats_updated.connect(self.toolbar.update_stats)

    @Slot(bool)
    def __on_capture_toggled(self, is_on: bool):
        """协调：toolbar 信号 → controller 操作"""
        self.toolbar.control_btn.setEnabled(False)
        try:
            self.controller.toggle_capture()
        finally:
            self.toolbar.control_btn.setEnabled(True)

    @Slot(bool)
    def __on_capture_state_changed(self, is_capturing: bool):
        """协调：controller 状态信号 → UI 更新"""
        if is_capturing:
            self.toolbar.control_btn.setIcon(FluentIcon.PAUSE)
            show_success("成功", "代理已启动", parent=self)
        else:
            self.toolbar.control_btn.setIcon(FluentIcon.PLAY)
            show_success("成功", "代理已关闭", parent=self)

    @Slot()
    def __on_search_changed(self):
        """搜索条件变更时更新过滤"""
        conditions = self.toolbar.search_panel.get_conditions()
        self.content.table.proxy_model.set_multi_search(conditions)

    @Slot()
    def __show_proxy_port_dialog(self):
        """弹出端口设置对话框"""
        w = ProxyPortDialog(self.controller.current_port, self.window())
        if w.exec():
            new_port = w.get_port()
            if new_port and new_port != self.controller.current_port:
                self.controller.update_port(new_port)

    def stop_capture(self):
        """停止抓包（供外部调用，如MainWindow.closeEvent）"""
        self.controller.stop_capture()


class CapturesContentArea(OrientationSplitter):
    """抓包内容区域 - 包含数据表格和详情面板的分割视图"""

    def __init__(
        self,
        parent: "CapturesInterface|None" = None,
        controller=None,
    ):
        """初始化内容区域

        Args:
            parent: 父组件，通常是 CapturesInterface
            controller: 抓包控制器实例
        """
        super().__init__(parent=parent)
        self.controller = controller  # 保存 controller 引用

        self.__init_widget()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.table = CapturesDataTable(self)
        self.panel = CapturesDataPanel(self, self.controller)
        self.addWidget(self.table)
        self.addWidget(self.panel)

        self.setStretchFactor(0, 1)  # ← table 占 1 份
        self.setStretchFactor(
            1, 0
        )  # ← panel 初始不占空间（折叠态），与 setSizes([1,0]) 一致，避免 stretch 与初始尺寸博弈
        self.setSizes([1, 0])  # ← 默认仅显示表格，右侧面板收起

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.table.row_double_clicked.connect(self.__on_show_panel)
        self.table.row_selected.connect(self.__on_select_row)  # ← 新增
        self.panel.collapseRequested.connect(lambda: self.setSizes([1, 0]))

    @Slot(dict)
    def __on_show_panel(self, data):
        """双击：打开面板，严格 50/50

        Args:
            data: 行数据字典
        """
        self.panel.set_data(data)
        # 延迟设置尺寸，确保布局已完成
        QTimer.singleShot(0, self.__apply_equal_sizes)

    def __apply_equal_sizes(self):
        """确保 table 和 panel 严格 50:50"""
        if self.orientation() == Qt.Orientation.Horizontal:
            w = self.width()
            if w > 0:
                self.setSizes([w // 2, w // 2])
        else:
            h = self.height()
            if h > 0:
                self.setSizes([h // 2, h // 2])

    @Slot(dict)  # ← 新增方法
    def __on_select_row(self, data):
        """单击：面板已打开时，切换数据

        Args:
            data: 行数据字典
        """
        if self.sizes()[1] > 0:  # 面板可见（宽度 > 0）
            self.panel.set_data(data)


class CapturesToolBar(QWidget):
    """自定义工具栏 - 内嵌搜索面板，自管理显隐，对外暴露业务信号"""

    # 业务信号
    captureToggled = Signal(bool)
    conditionsChanged = Signal()  # 搜索面板条件变更（透传）

    def __init__(self, parent: "CapturesInterface"):
        """初始化工具栏

        Args:
            parent: 父组件，通常是 CapturesInterface
        """
        super().__init__(parent)

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        # 垂直方向只占所需空间，不被 VBoxLayout 拉伸
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        # 左侧：过滤按钮
        self.search_btn = TransparentToolButton(FluentIcon.FILTER, self)
        self.search_btn.setCheckable(True)
        self.search_btn.setToolTip(self.tr("高级搜索") + " (Ctrl+F)")
        self.search_btn.installEventFilter(
            ToolTipFilter(self.search_btn, 1000, ToolTipPosition.TOP)
        )
        self.search_btn.setFixedSize(32, 32)
        self.search_btn.setIconSize(QSize(20, 20))

        # 统计标签
        self.stats_label = CaptionLabel("0", self)

        # 右侧：操作按钮
        self.proxy_setting_btn = TransparentToolButton(FluentIcon.GLOBE, self)
        self.proxy_setting_btn.setToolTip(self.tr("端口设置"))
        self.proxy_setting_btn.installEventFilter(
            ToolTipFilter(self.proxy_setting_btn, 1000, ToolTipPosition.TOP)
        )
        self.proxy_setting_btn.setFixedSize(32, 32)
        self.proxy_setting_btn.setIconSize(QSize(20, 20))

        self.locate_selection_btn = TransparentToolButton(
            BaseIcon.LOCATION_TARGET, self
        )
        self.locate_selection_btn.setToolTip(self.tr("定位选中"))
        self.locate_selection_btn.installEventFilter(
            ToolTipFilter(self.locate_selection_btn, 1000, ToolTipPosition.TOP)
        )
        self.locate_selection_btn.setFixedSize(32, 32)
        self.locate_selection_btn.setIconSize(QSize(20, 20))

        self.control_btn = TransparentToolButton(FluentIcon.PLAY, self)
        self.control_btn.setCheckable(True)
        self.control_btn.setToolTip(self.tr("系统代理"))
        self.control_btn.installEventFilter(
            ToolTipFilter(self.control_btn, 1000, ToolTipPosition.TOP)
        )
        self.control_btn.setFixedSize(32, 32)
        self.control_btn.setIconSize(QSize(20, 20))

        self.captures_delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.captures_delete_btn.setToolTip(self.tr("清空数据"))
        self.captures_delete_btn.installEventFilter(
            ToolTipFilter(self.captures_delete_btn, 1000, ToolTipPosition.TOP)
        )
        self.captures_delete_btn.setFixedSize(32, 32)
        self.captures_delete_btn.setIconSize(QSize(20, 20))

        # 搜索面板（内嵌）
        self.search_panel = MultiFilterManager(self)
        self.search_panel.setContentsMargins(0, 0, 0, 4)

        # Ctrl+F 快捷键（应用级事件过滤）
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def __init_layout(self):
        """初始化布局结构 - 按钮行 + 搜索面板（垂直）"""
        v_layout = QVBoxLayout(self)
        v_layout.setContentsMargins(0, 0, 0, 0)
        v_layout.setSpacing(0)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(0, 0, 0, 0)
        btn_layout.addWidget(self.search_btn)
        btn_layout.addWidget(self.stats_label)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.proxy_setting_btn)
        btn_layout.addWidget(self.locate_selection_btn)
        btn_layout.addWidget(self.control_btn)
        btn_layout.addWidget(self.captures_delete_btn)

        v_layout.addLayout(btn_layout)
        v_layout.addWidget(self.search_panel)

    def __connect_signal_to_slot(self):
        """组件内部事件管理"""
        self.control_btn.toggled.connect(self.captureToggled.emit)
        self.search_btn.toggled.connect(self.__toggle_search_panel)
        self.search_panel.conditionsChanged.connect(self.conditionsChanged.emit)
        self.search_panel.panelCloseRequested.connect(self.__on_search_panel_close)

    @Slot()
    def __toggle_search_panel(self):
        """切换搜索面板显示/隐藏"""
        visible = not self.search_panel.isVisible()
        self.search_panel.setVisible(visible)
        self.search_btn.blockSignals(True)
        self.search_btn.setChecked(visible)
        self.search_btn.blockSignals(False)
        if visible:
            self.search_panel.focus_first_input()

    @Slot()
    def __on_search_panel_close(self):
        """搜索面板请求关闭（最后一行被删除）"""
        self.search_panel.setHidden(True)
        self.search_btn.blockSignals(True)
        self.search_btn.setChecked(False)
        self.search_btn.blockSignals(False)

    def eventFilter(self, obj, event):
        """应用级事件过滤：拦截 Ctrl+F 快捷键"""
        if event.type() == QEvent.Type.KeyPress:
            if (
                event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_F
            ):
                if self.window().isActiveWindow():
                    self.__toggle_search_panel()
                    return True
        return super().eventFilter(obj, event)

    @Slot(int, int, int)
    def update_stats(self, total: int, shown: int, selected: int):
        """更新统计标签"""
        if shown == total:
            self.stats_label.setText(str(total))
        else:
            self.stats_label.setText(f"{shown}/{total}")


class ProxyPortDialog(MessageBoxBase):
    """代理端口设置对话框"""

    PORT_MIN = 1024
    PORT_MAX = 65535

    def __init__(self, current_port, parent: QWidget):
        """初始化端口设置对话框

        Args:
            current_port: 当前端口号
            parent: 父组件
        """
        super().__init__(parent)
        self.__init_widget(current_port)
        self.__init_layout()

    def __init_widget(self, current_port):
        """初始化界面组件

        Args:
            current_port: 当前端口号
        """
        self.title_label = SubtitleLabel(self)
        self.title_label.setText(self.tr("设置代理端口"))
        self.port_spin = SpinBox(self)
        self.port_spin.setRange(self.PORT_MIN, self.PORT_MAX)
        self.port_spin.setValue(current_port)
        self.port_spin.setSingleStep(1)

    def __init_layout(self):
        """初始化布局结构"""
        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.port_spin)
        self.widget.setMinimumWidth(350)

    def get_port(self) -> int:
        """获取用户设置的端口号

        Returns:
            int: 用户设置的端口号
        """
        return self.port_spin.value()


class CapturesDataTable(TableView):
    """抓包数据表格 - 显示网络请求数据"""

    row_double_clicked = Signal(dict)  # 双击行信号
    row_selected = Signal(dict)  # 选中行信号
    stats_updated = Signal(int, int, int)  # 统计更新信号：总条数、显示条数、选中条数

    def __init__(self, parent: "CapturesContentArea | None" = None):
        """初始化数据表格

        Args:
            parent: 父组件，通常是 CapturesContentArea
        """
        super().__init__(parent)

        self.__init_widget()
        self.__init_view()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        """初始化界面组件"""
        self.source_model = PacketTableModel(self)
        self.proxy_model = PacketProxyModel(self)

        self.context_menu = PacketContextMenu(self)
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
        widths = [80, 100, 500, 100, 100, 0]
        h_header = self.horizontalHeader()
        h_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate(widths):
            self.setColumnWidth(i, w)
        h_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        # self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

    def __connect_signal_to_slot(self):
        """连接信号与槽函数"""
        self.proxy_model.rowsInserted.connect(self.__on_sync_visual)
        self.proxy_model.layoutChanged.connect(self.__on_sync_visual)
        self.proxy_model.rowsRemoved.connect(self.__on_sync_visual)
        self.proxy_model.modelReset.connect(self.__on_sync_visual)
        # 同时监听 source_model，确保过滤条件排除所有行时 total 仍能正确更新
        self.source_model.rowsInserted.connect(self.__on_sync_visual)

        self.customContextMenuRequested.connect(self.__on_show_context_menu)
        self.context_menu.delete_requested.connect(self.source_model.remove_row)

        self.selectionModel().selectionChanged.connect(self.__on_selection_changed)

        self.doubleClicked.connect(self.__on_row_double_clicked)

    @Slot()
    def __on_selection_changed(self, selected):
        """选择变更时触发

        Args:
            selected: 选中的项
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
        self.context_menu.update_context(row, row_data)
        self.context_menu.exec(self.viewport().mapToGlobal(pos))

    @Slot()
    def __on_sync_visual(self):
        """视图更新（动态的插入需要）"""
        QTimer.singleShot(0, self.updateSelectedRows)
        QTimer.singleShot(0, self.__emit_stats_updated)

    def __emit_stats_updated(self):
        """发出统计更新信号"""
        total = self.source_model.rowCount()
        shown = self.proxy_model.rowCount()
        selected = len(self.selectionModel().selectedRows())
        self.stats_updated.emit(total, shown, selected)

    @Slot()
    def clear_all(self):
        """清除所有数据"""
        self.source_model.clear_data()

    def set_traffic_addon(self, traffic_addon):
        """设置 UITrafficAddon 实例

        Args:
            traffic_addon: 流量插件实例
        """
        self.source_model._traffic_addon = traffic_addon

    @Slot()
    def on_locate_selection(self):
        """定位 滑动到选中"""
        index = self.selectionModel().currentIndex()
        if not index.isValid():
            return
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.horizontalScrollBar().setValue(0)

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


class CapturesDataPanel(SimpleCardWidget):
    """抓包数据面板 - 显示请求和响应详情"""

    collapseRequested = Signal()  # 请求折叠面板

    def __init__(self, parent: "CapturesContentArea", controller=None):
        super().__init__(parent=parent)
        self.controller = controller  # 保存 controller 引用
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

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
        self.detail_page.setStretchFactor(0, 1)
        self.detail_page.setStretchFactor(1, 1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.empty_page)  # index 0
        self.stack.addWidget(self.detail_page)  # index 1

        self.setBorderRadius(0)  # ← 去掉圆角，与表格对齐

        self.__update_close_buttons()

    def __init_layout(self):
        """初始化布局结构"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

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

    @Slot()
    def __update_close_buttons(self):
        """更新关闭按钮显示状态"""
        if self.detail_page.orientation() == Qt.Orientation.Vertical:
            self.req_panel.close_button.show()
            self.res_panel.close_button.hide()
        else:
            self.req_panel.close_button.hide()
            self.res_panel.close_button.show()

    @Slot()
    def __on_layout_changed(self):
        """布局方向切换时，延迟重新设置 50:50 比例"""
        if self.stack.currentIndex() == 1:  # 详情页可见
            QTimer.singleShot(100, self.__apply_detail_equal_sizes)

    @Slot()
    def __collapse_panel(self):
        """折叠面板"""
        self.collapseRequested.emit()

    def set_data(self, data: dict):
        """有数据时调用，切换到详情页并填充

        Args:
            data: 数据字典
        """
        self.req_panel.set_data(data)  # 请求面板
        self.res_panel.set_data(data)  # 响应面板
        self.stack.setCurrentIndex(1)
        # detail_page 刚切为当前页时尚未完成 layout，width 可能未就绪，
        # 故延迟到下一事件循环（布局算完）再设一次 50:50，避免抖动循环。
        QTimer.singleShot(0, self.__apply_detail_equal_sizes)

    def eventFilter(self, obj, e):
        """保留空实现以兼容父类；detail 页比例已改由 singleShot(0) 单次设定。"""
        return super().eventFilter(obj, e)

    def __apply_detail_equal_sizes(self):
        """确保请求面板和响应面板严格 50:50"""
        w = self.detail_page.width()
        h = self.detail_page.height()
        if self.detail_page.orientation() == Qt.Orientation.Horizontal:
            if w > 0:
                self.detail_page.setSizes([w // 2, w // 2])
        else:
            if h > 0:
                self.detail_page.setSizes([h // 2, h // 2])


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

    def set_cookies(self, cookies: dict):
        """设置 cookie 数据 {name: value, ...}

        Args:
            cookies: Cookie 字典
        """
        self.cookies = cookies
        self.tree.clear()

        for key, value in cookies.items():
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
        self.params_widget = KVDualPanel()
        self.params_widget.set_read_only(True)
        self.header_card = KVDualPanel()
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
        self.addTab("总览", self.overview, self.tr("总览"))
        self.addTab("原始", self.raw_edit, "原始")
        self.addTab("请求头", self.header_card, "请求头")
        self.addTab("请求体", self.body_card, "请求体")
        self.addTab("Cookies", self.cookie_card, "Cookies")
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
        body = data.get("Request Body", b"")
        self._fill_raw(body, content_type, flow_id)
        self._fill_body(data)

        # 消费预解析的 URL 参数，动态决定是否显示"参数"tab
        params = data.get("Request Params", {})
        self.params_widget.set_items(params)
        if params:
            if "参数" not in self.pivot.items:
                self.addTab("参数", self.params_widget, "参数", index=2)
        else:
            if "参数" in self.pivot.items:
                if self.pivot.currentRouteKey() == "参数":
                    self.pivot.setCurrentItem("原始")
                self.pivot.removeWidget("参数")
                idx = self.stacked.indexOf(self.params_widget)
                if idx >= 0:
                    self.stacked.removeWidget(self.params_widget)

        # 消费预解析的 Cookie，无 Cookie 时自动隐藏 Cookies 面板
        cookies = data.get("Request Cookies", {})
        self.cookie_widget.set_cookies(cookies)
        if cookies:
            if "Cookies" not in self.pivot.items:
                self.addTab("Cookies", self.cookie_card, "Cookies")
        else:
            if "Cookies" in self.pivot.items:
                if self.pivot.currentRouteKey() == "Cookies":
                    self.pivot.setCurrentItem("原始")
                self.pivot.removeWidget("Cookies")
                idx = self.stacked.indexOf(self.cookie_card)
                if idx >= 0:
                    self.stacked.removeWidget(self.cookie_card)

    def _fill_raw(self, body: bytes, content_type: str = "", flow_id: str = ""):
        """生成完整的原始HTTP请求格式

        Args:
            body: 请求体
            content_type: 内容类型
            flow_id: 流 ID
        """
        # 尝试使用controller获取原始HTTP请求
        if self.controller and flow_id:
            try:
                raw_data = self.controller.get_raw_request(flow_id)
                if raw_data:
                    if isinstance(raw_data, bytes):
                        text = raw_data.decode("utf-8", errors="replace")
                    else:
                        text = str(raw_data)
                    self.raw_edit.set_text(text)
                    return
            except Exception as e:
                print(f"获取原始HTTP数据失败: {e}")

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
                try:
                    text = body.decode("utf-8", errors="replace")
                except Exception:
                    text = str(body)
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

        self.body_card = JsonDualPanel()
        self.body_card.set_read_only(True)

        self.header_card = KVDualPanel()
        self.header_card.set_read_only(True)

    def __init_layout(self):
        """初始化布局结构"""
        self.addTab("原始", self.raw_edit, "原始")
        self.addTab("响应头", self.header_card, "响应头")
        self.addTab("响应体", self.body_card, "响应体")
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

        # 响应体（消费预解析字段）
        flow_id = data.get("Connection ID", "")
        content_type = data.get("Response Content-Type", "")
        body = data.get("Response Body", b"")
        self._fill_raw(body, content_type, flow_id)
        self._fill_body(data)

    def _fill_raw(self, body: bytes, content_type: str = "", flow_id: str = ""):
        """生成完整的原始HTTP响应格式

        Args:
            body: 响应体
            content_type: 内容类型
            flow_id: 流 ID
        """
        # 尝试使用controller获取原始HTTP响应
        if self.controller and flow_id:
            try:
                raw_data = self.controller.get_raw_response(flow_id)

                if raw_data:
                    # 如果成功获取到原始数据，直接使用
                    if isinstance(raw_data, bytes):
                        text = raw_data.decode("utf-8", errors="replace")
                    else:
                        text = str(raw_data)
                    self.raw_edit.set_text(text)
                    return
            except Exception as e:
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
                try:
                    text = body.decode("utf-8", errors="replace")
                except Exception:
                    text = str(body)
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
    FIELDS: list[tuple[str, FieldKey]] = [
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

    # 应用程序信息（仅在有数据时显示）
    APP_FIELDS = [
        ("名称", "App Name"),
        ("ID", "App ID"),
        ("路径", "App Path"),
        ("进程ID", "Process ID"),
    ]

    # 连接信息
    CONN_FIELDS = [
        ("ID", "Connection ID"),
        ("时间", "Connection Time"),
    ]
    CONN_FRONT_FIELDS = [
        ("客户端 地址", "Front Client Address"),
        ("客户端 端口", "Front Client Port"),
        ("服务端 地址", "Front Server Address"),
        ("服务端 端口", "Front Server Port"),
    ]
    CONN_BACK_FIELDS = [
        ("客户端 地址", "Back Client Address"),
        ("客户端 端口", "Back Client Port"),
        ("服务端 地址", "Back Server Address"),
        ("服务端 端口", "Back Server Port"),
    ]

    # TLS 信息
    TLS_FIELDS = [
        ("版本", "TLS Version"),
        ("SNI", "TLS SNI"),
        ("ALPN", "TLS ALPN Offers"),
        ("选择ALPN", "TLS ALPN Selected"),
        ("加密算法列表", "TLS Cipher List"),
        ("选择算法", "TLS Cipher"),
    ]

    # 证书信息 - Subject
    CERT_SUBJECT_FIELDS = [
        ("Common Name", "Subject Common Name"),
        ("国家", "Subject Country"),
        ("省（州）", "Subject State"),
        ("地区", "Subject Locality"),
        ("组织", "Subject Organization"),
        ("单位", "Subject Organizational Unit"),
    ]

    # 证书信息 - 签发者
    CERT_ISSUER_FIELDS = [
        ("Common Name", "Issuer Common Name"),
        ("国家", "Issuer Country"),
        ("省（州）", "Issuer State"),
        ("地区", "Issuer Locality"),
        ("组织", "Issuer Organization"),
        ("单位", "Issuer Organizational Unit"),
    ]

    # 证书详细信息
    CERT_DETAIL_FIELDS: list[tuple[str, FieldKey]] = [
        ("开始时间", "Not Before"),
        ("截止时间", "Not After"),
        ("指纹", "Fingerprint SHA1"),
        ("序列号", "Serial Number Hex"),
    ]

    # 时间信息
    TIME_FIELDS: list[tuple[str, FieldKey]] = [
        ("请求开始", lambda d: format_time(d.get("req_time"))),
        ("请求结束", lambda d: format_time(d.get("req_timestamp_end"))),
        (
            "请求时长",
            lambda d: (
                f"{d.get('req_duration', 0):.1f} ms"
                if d.get("req_duration") is not None
                else "-"
            ),
        ),
        ("响应开始", lambda d: format_time(d.get("res_timestamp_start"))),
        ("响应结束", lambda d: format_time(d.get("res_time"))),
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
    SIZE_FIELDS: list[tuple[str, FieldKey]] = [
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

        # ── 应用程序信息（仅在有数据时显示） ──
        has_app = any(data.get(k) for _, k in self.APP_FIELDS)
        if has_app:
            parent = QTreeWidgetItem(self)
            parent.setText(0, self.tr("应用程序"))

            # 加粗父级标题，与子级 key 区分
            bold_font = QFont()
            bold_font.setBold(True)
            parent.setFont(0, bold_font)

            for label, key in self.APP_FIELDS:
                value = data.get(key)
                if value in (None, "", "N/A", "-", 0):
                    continue
                item = QTreeWidgetItem(parent)
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
