from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import (
    QCoreApplication,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QActionGroup,
    QBrush,
    QColor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    PushButton,
    RoundMenu,
    TableItemDelegate,
    TableView,
    TransparentDropDownPushButton,
    TreeItemDelegate,
    TreeView,
    getFont,
    isDarkTheme,
)

from ferret.apps.common.flow.column_settings import ColumnSettingsDialog
from ferret.apps.common.flow.columns import (
    COLUMNS,
    DEFAULT_ORDER,
    RESPONSIVE_KEYS,
    ColumnLayout,
    default_layout,
    is_fixed_width,
    load_layout,
    logical_index,
    save_layout,
)
from ferret.apps.common.flow.menus import (
    FlowContextMenu,
    FlowExportMenu,  # noqa: F401  re-export：菜单搬去 menus.py，外部照旧从 views 导入
    FlowSubViewMenu,  # noqa: F401
)
from ferret.apps.common.flow.models import (
    HIGHLIGHT_ROLE,
    FlowConnProxyModel,
    FlowConnTreeModel,
    FlowProxyModel,
    FlowTableModel,
)
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    FlowViewCapabilities,
)
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.splitter import OrientationSplitter
from ferret.core.log import get_logger
from ferret.core.mitm import FlowRow

log = get_logger("flow")

if TYPE_CHECKING:
    from collections.abc import Callable

    from ferret.apps.common.flow.detail import FlowDataPanel
    from ferret.apps.common.flow.models import FlowSource

    # 类型检查专用基类：mixin 运行时是纯 `object`，但它调用的 `tr` / `setColumnWidth`
    # / `setColumnHidden` 与 `column_layout_changed` 信号都由具体视图子类（QTableView /
    # QTreeView 子类）提供。挂 QWidget 供类型检查拿到 `tr` 与 QWidget 身份（对话框
    # parent），另在类体声明表格/树两者共有、但 QWidget 没有的方法 —— 不挂
    # QAbstractItemView 是因为它与 qfw TableBase 的 setCurrentIndex/setItemDelegate
    # 签名冲突，会误报 invalid-method-override。
    _MixinBase = QWidget
else:
    _MixinBase = object


class _ColumnLayoutMixin(_MixinBase):
    """列布局应用（顺序 / 显隐 / 宽度 / Mark 固定宽 / 响应式），全按稳定 key。

    平铺表格与连接树共用（`docs/design.md#ui`）。逻辑列/模型
    `_headers` 恒定不动，重排只经 `QHeaderView.moveSection`（纯视觉），排序天然跟随
    稳定逻辑列。子类须：声明 `column_layout_changed = Signal(object)`、提供
    `_column_header()` 返回其 `QHeaderView`、在视图初始化里调 `_init_columns(layout)`。

    宽度用户拖动经 `sectionResized` 落到 `column_layout_changed` 信号，由
    `FlowViewerPane` 持久化并同步另一视图；程序化应用布局时 `_applying_layout` 闸门
    短路回写，避免启动即 churn / 把响应式临时状态写进配置（§4.2）。
    """

    if TYPE_CHECKING:
        # 具体视图子类提供的方法，用注解声明（不用 `def ...: ...` 内联体：
        # pyside6-lupdate 的 Python 扫描器会在类体内联体上错算缩进，把其后类的
        # `tr()` 甩进空 context，英文界面静默退回中文，§7）。
        # 这里声明类级描述符；实例访问时 PySide6 将其绑定为 SignalInstance，
        # 直接声明 SignalInstance 会与子类的 Signal(object) 定义冲突。
        column_layout_changed = Signal(object)
        setColumnWidth: Callable[[int, int], None]
        setColumnHidden: Callable[[int, bool], None]

    NARROW_WIDTH = 900

    def _column_header(self) -> QHeaderView:
        raise NotImplementedError

    def _init_columns(self, layout: ColumnLayout) -> None:
        self._column_layout = layout
        self._column_menu: RoundMenu | None = None
        self._responsive_hidden: set[str] = set()
        self._applying_layout = False
        header = self._column_header()
        # 表头 section 全程不可拖动：重排只经列设置对话框（§0：连接树装饰绑逻辑列 0，
        # 原生拖拽把 index 拖离视觉 0 会让树形装饰跟着跑）。
        header.setSectionsMovable(False)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_menu)
        header.sectionResized.connect(self._on_section_resized)
        self._apply_column_layout(layout)

    def apply_column_layout(self, layout: ColumnLayout) -> None:
        """外部（FlowViewerPane）驱动：换一份布局并刷新表头。"""
        self._column_layout = layout
        self._apply_column_layout(layout)

    def _apply_column_layout(self, layout: ColumnLayout) -> None:
        header = self._column_header()
        self._applying_layout = True
        try:
            for col in COLUMNS:
                lg = logical_index(col.key)
                width = col.default_width if col.fixed_width else layout.width(col.key)
                self.setColumnWidth(lg, width)
            # Mark 固定宽：按 key 取逻辑列（moveSection 不改逻辑列，仍是它本来的位置）。
            header.setSectionResizeMode(
                logical_index("mark"), QHeaderView.ResizeMode.Fixed
            )
            # 重排：不动点算法，逐个把第 k 个视觉位钉死后不再动它（§4.2）。
            for visual_pos, key in enumerate(layout.order):
                lg = logical_index(key)
                current = header.visualIndex(lg)
                if current != visual_pos:
                    header.moveSection(current, visual_pos)
            self._refresh_hidden()
        finally:
            self._applying_layout = False

    def _refresh_hidden(self) -> None:
        """按「用户可见性 ∨ 响应式隐藏」重算每列显隐（用户可见性是权威，§4.2）。"""
        layout = self._column_layout
        for col in COLUMNS:
            hidden = (not layout.is_visible(col.key)) or (
                col.key in self._responsive_hidden
            )
            self.setColumnHidden(logical_index(col.key), hidden)

    def _apply_responsive_columns(self, width: int) -> None:
        """窄窗临时隐藏 Status/Type（按 key，不写死索引）；只读用户配置、绝不回写。"""
        narrow = width < self.NARROW_WIDTH
        self._responsive_hidden = set(RESPONSIVE_KEYS) if narrow else set()
        self._refresh_hidden()

    @Slot(int, int, int)
    def _on_section_resized(self, logical: int, _old: int, new: int) -> None:
        if self._applying_layout or new <= 0:
            return
        if not (0 <= logical < len(DEFAULT_ORDER)):
            return
        key = DEFAULT_ORDER[logical]
        if is_fixed_width(key):  # Mark 固定宽，不记
            return
        self._column_layout = self._column_layout.with_width(key, new)
        self.column_layout_changed.emit(self._column_layout)

    @Slot(QPoint)
    def _on_header_menu(self, pos: QPoint) -> None:
        # 只保留一份菜单并随视图释放：关闭时仍可能有原生事件引用菜单，不能在
        # 此时销毁；复用又能避免每次打开都在视图下累积菜单与 QAction。
        if self._column_menu is None:
            self._column_menu = self._create_column_menu()
        self._column_menu.exec(self._column_header().mapToGlobal(pos))

    def _create_column_menu(self) -> RoundMenu:
        # tr 走 QCoreApplication.translate 钉死 context：本方法在 mixin 里，self 运行时是
        # FlowDataTable/FlowConnTree，self.tr 的 runtime context 与 lupdate 静态提取的
        # mixin context 对不上会静默退回中文（§7）。用字面 context 让提取与查表一致。
        menu = RoundMenu(parent=self)
        settings_action = BaseAction(
            FluentIcon.SETTING,
            QCoreApplication.translate("FlowColumnMenu", "列设置…"),
            menu,
        )
        reset_action = BaseAction(
            FluentIcon.CANCEL,
            QCoreApplication.translate("FlowColumnMenu", "恢复默认列"),
            menu,
        )
        settings_action.triggered.connect(self._open_column_dialog)
        reset_action.triggered.connect(self._reset_columns)
        menu.addAction(settings_action)
        menu.addAction(reset_action)
        return menu

    def _open_column_dialog(self) -> None:
        dialog = ColumnSettingsDialog(self._column_layout, self)
        # 不在菜单鼠标事件里嵌套 exec，让点击立即退栈；保持模态显示，
        # 结果与释放都交给关闭信号处理，避免窗口关闭后再返回旧的原生调用栈。
        dialog.accepted.connect(
            lambda: self._commit_column_layout(dialog.result_layout())
        )
        dialog.finished.connect(dialog.deleteLater)
        dialog.setModal(True)
        dialog.show()

    def _reset_columns(self) -> None:
        self._commit_column_layout(default_layout())

    def _commit_column_layout(self, layout: ColumnLayout) -> None:
        """应用到本视图并广播（FlowViewerPane 落盘 + 同步另一视图）。"""
        self.apply_column_layout(layout)
        self.column_layout_changed.emit(layout)


def _visual_caps(header: QHeaderView | None, logical_col: int) -> tuple[bool, bool]:
    """(是否视觉首个可见列, 是否视觉末个可见列)。

    高亮圆角按当前**视觉**首/末可见列铺（隐藏列不计），重排或末列隐藏后不画错
    （§4.2）。header 为 None（脱离视图绘制，测试外几乎不发生）时退化到逻辑判断。
    """
    if header is None:
        return (logical_col == 0, False)
    visibles = [
        v
        for v in range(header.count())
        if not header.isSectionHidden(header.logicalIndex(v))
    ]
    if not visibles:
        return (False, False)
    visual = header.visualIndex(logical_col)
    return (visual == visibles[0], visual == visibles[-1])


class HighlightRowDelegate(TableItemDelegate):
    """命中搜索表达式的行整行垫一层琥珀底。

    继承 qfw `TableItemDelegate` 以保住 Fluent 的选中/hover/斑马纹与左侧指示条 ——
    这些都由基类按 `self.delegate` 驱动，换掉委托后 `TableBase.setItemDelegate` 会同步
    更新该引用（见 qfw table_view.py），所以选中同步不受影响。

    只在「命中且未选中」时，先按 qfw 同一套圆角行背景规则铺一层半透明琥珀，再交回
    基类画文字与其余装饰。选中态短路 → 命中行被选中时让位给 Fluent 高亮，不叠双层底色。
    """

    # 琥珀，light/dark 两档；alpha 压到只染底不糊字（与 models._semantic_color 同姿态）。
    _LIGHT = QColor(255, 185, 0, 48)
    _DARK = QColor(255, 196, 0, 44)

    def paint(
        self,
        painter: QPainter,
        option,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        if index.data(HIGHLIGHT_ROLE) and not (
            option.state & QStyle.StateFlag.State_Selected
        ):
            self._fill_highlight(painter, option, index)
        super().paint(painter, option, index)

    def _fill_highlight(
        self, painter: QPainter, option, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setClipRect(option.rect)
        painter.setBrush(self._DARK if isDarkTheme() else self._LIGHT)
        # 复刻 qfw TableItemDelegate._drawBackground 的圆角规则（含 2px 行距 margin），
        # 让高亮底与选中/hover 的胶囊形状严丝合缝（adjusted 返回新矩形，不动 option）。
        rect = option.rect.adjusted(0, self.margin, 0, -self.margin)
        radius = 5
        header = option.widget.horizontalHeader() if option.widget is not None else None
        is_first, is_last = _visual_caps(header, index.column())
        if is_first:
            painter.drawRoundedRect(rect.adjusted(4, 0, radius + 1, 0), radius, radius)
        elif is_last:
            painter.drawRoundedRect(
                rect.adjusted(-radius - 1, 0, -4, 0), radius, radius
            )
        else:
            painter.drawRect(rect.adjusted(-1, 0, 1, 0))
        painter.restore()


# qfw TableBase 收窄了 QAbstractItemView 的 setCurrentIndex / setItemDelegate 签名
# （库侧 stub 既有事实）；多继承把这对 QAbstractItemView 内部的冲突暴露到本类，
# 与本改动无关，定向忽略。
class FlowDataTable(_ColumnLayoutMixin, TableView):  # ty: ignore[invalid-method-override]
    """Flow 数据表格 - 显示网络请求数据。"""

    row_double_clicked = Signal(dict)  # 仅携带 flow id，由宿主按需读取详情
    row_selected = Signal(dict)
    stats_updated = Signal(int, int, int)  # 统计更新信号：总条数、显示条数、选中条数
    column_layout_changed = Signal(object)  # 列布局变更（FlowViewerPane 落盘 + 同步树）

    def _column_header(self) -> QHeaderView:
        return self.horizontalHeader()

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
        h_header = self.horizontalHeader()
        h_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h_header.setMinimumSectionSize(44)
        # 列宽 / Mark 固定宽 / 顺序 / 显隐全由列布局收敛（消除写死的 widths 数组与
        # setSectionResizeMode(1, Fixed)）；持久布局的读取与应用在 FlowViewerPane。
        self._init_columns(default_layout())
        h_header.setFixedHeight(36)
        self.verticalHeader().setDefaultSectionSize(34)
        self.setMinimumWidth(360)
        self.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        # 搜索高亮模式的整行染色委托（替换 qfw 默认委托，选中同步由基类透传保住）。
        self.setItemDelegate(HighlightRowDelegate(self))
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
        self.context_menu.delete_requested.connect(self.remove_selected)

        # 菜单 action 上的 shortcut 挂不到表格，Delete 键得在表格侧另接一条 ——
        # 仅 capture 能力集才允许删除（会话页 can_delete=False，快捷键同菜单一起缺席）。
        if self.capabilities.can_delete:
            QShortcut(QKeySequence.StandardKey.Delete, self).activated.connect(
                self.remove_selected
            )

        self.selectionModel().selectionChanged.connect(self.__on_selection_changed)

        self.doubleClicked.connect(self.__on_row_double_clicked)

    def set_controller(self, controller) -> None:
        """更新 Flow 查看控制器并同步到关联菜单。"""
        self.controller = controller
        self.context_menu.controller = controller
        self.context_menu.export_menu.controller = controller

    def row_summary(self, row: int) -> dict:
        """行号 → 轻量摘要字典，由 controller 在 mitm 线程内构建。

        右键菜单只消费 method/host/url/type/comment 这几个标量，curl 改为点击
        复制时按需生成（见 FlowExportMenu.__export_text）。此前这里走
        `flow_detail`：双侧 body 解码美化 + curl 全算一遍，大 body 流量上右键
        这个高频操作每次都白付几十毫秒。

        没有 controller 只发生在裸构造 `FlowViewerPane()` 的场合（测试）——
        此时没有任何流量可查，空字典就是正确答案。
        """
        snapshot = self.source_model.get_row(row)
        if snapshot is None or self.controller is None:
            return {}
        return self.controller.flow_summary(snapshot.id)

    def _selection_for(self, index: QModelIndex | QPersistentModelIndex) -> dict:
        """普通选择只传不可变行的 id，不在折叠状态下解码、美化 body。"""
        source_index = self.proxy_model.mapToSource(index)
        row = self.source_model.get_row(source_index.row())
        return {"id": row.id} if row is not None else {}

    def get_selected_rows(self) -> list[FlowRow]:
        """获取当前选中的行快照列表(单选/多选通用)"""
        rows = []
        for index in self.selectionModel().selectedRows():
            source_index = self.proxy_model.mapToSource(index)
            row = self.source_model.get_row(source_index.row())
            if row:
                rows.append(row)
        return rows

    @Slot()
    def __on_selection_changed(self, selected):
        """选择变更时触发

        :param selected: 选中的项
        """
        indexes = selected.indexes()
        if indexes:
            self.row_selected.emit(self._selection_for(indexes[0]))

        # 更新统计信息
        QTimer.singleShot(0, self, self.__emit_stats_updated)

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
        row_data = self.row_summary(row)
        selected_rows = self.get_selected_rows()
        self.context_menu.update_context(row, row_data, selected_rows)
        self.context_menu.exec(self.viewport().mapToGlobal(pos))

    @Slot()
    def __on_sync_visual(self):
        """视图更新（动态的插入需要）"""
        QTimer.singleShot(0, self, self.updateSelectedRows)
        QTimer.singleShot(0, self, self.__emit_stats_updated)

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
        self.source_model.clear_data()
        self.clearSelection()
        QTimer.singleShot(0, self, self.__emit_stats_updated)

    @Slot()
    def remove_selected(self) -> None:
        """删除当前选中的 flow（单选/多选同一条路）。"""
        rows = self.get_selected_rows()
        if rows:
            self.source_model.remove_flows([row.id for row in rows])

    def set_source(self, source) -> None:
        """注入数据源（满足 FlowSource 协议：View 本体或其适配器）"""
        self.source_model.set_source(source)

    def set_highlight_ids(self, ids: set[str]) -> None:
        """回推搜索高亮命中集到模型（透传，见 FlowTableModel.set_highlight_ids）。"""
        self.source_model.set_highlight_ids(ids)

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
        """当前选区的轻量描述；详情由 FlowViewerPane 按需获取。"""
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return {}
        index = self.selectionModel().currentIndex()
        if not index.isValid() or not self.selectionModel().isSelected(index):
            index = indexes[0]
        return self._selection_for(index)

    @Slot(QModelIndex)
    def __on_row_double_clicked(self, index: QModelIndex):
        """双击行时触发

        Args:
            index: 被双击的索引
        """
        self.row_double_clicked.emit(self._selection_for(index))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_responsive_columns(e.size().width())


class HighlightTreeDelegate(TreeItemDelegate):
    """连接树版的整行命中高亮委托（平铺侧 `HighlightRowDelegate` 的树孪生）。

    继承 qfw `TreeItemDelegate` 保住 Fluent 的选中/hover 指示条与字体/前景色读取
    （`initStyleOption` 读 FontRole/ForegroundRole，父节点粗体、状态语义色都靠它）。
    命中且未选中时先铺一层琥珀底，再交回基类画文字；圆角规则复刻基类
    `_drawBackground`（首列自带缩进/展开箭头，起点 x=4 与之对齐）。
    """

    _LIGHT = QColor(255, 185, 0, 48)
    _DARK = QColor(255, 196, 0, 44)

    def paint(
        self,
        painter: QPainter,
        option,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        if index.data(HIGHLIGHT_ROLE) and not (
            option.state & QStyle.StateFlag.State_Selected
        ):
            self._fill_highlight(painter, option, index)
        super().paint(painter, option, index)

    def sizeHint(self, option, index: QModelIndex | QPersistentModelIndex) -> QSize:
        # 与平铺表格 34px 行高对齐（树无 verticalHeader，靠委托定高）。
        size = super().sizeHint(option, index)
        size.setHeight(34)
        return size

    def initStyleOption(
        self, option, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        # qfw TreeItemDelegate.initStyleOption 把 ForegroundRole 当 QBrush 直接调
        # `.color()`；我们的模型（与平铺共用 flow_cell）返回的是 QColor，QColor 没有
        # `.color()` 会崩。平铺侧 TableItemDelegate 先 `QBrush(x)` 再 `.color()` 才没事，
        # 这里照它的姿势重写，绕开树委托的 bug（字体/前景色行为保持一致）。
        super(TreeItemDelegate, self).initStyleOption(option, index)
        option.font = index.data(Qt.ItemDataRole.FontRole) or getFont(13)
        text_color = Qt.GlobalColor.white if isDarkTheme() else Qt.GlobalColor.black
        brush = index.data(Qt.ItemDataRole.ForegroundRole)
        if brush is not None:
            text_color = QBrush(brush).color()
        option.palette.setColor(QPalette.ColorRole.Text, text_color)
        option.palette.setColor(QPalette.ColorRole.HighlightedText, text_color)

    def _fill_highlight(
        self, painter: QPainter, option, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setClipRect(option.rect)
        painter.setBrush(self._DARK if isDarkTheme() else self._LIGHT)
        # 复刻 qfw TreeItemDelegate._drawBackground 的圆角规则（含 2px 行距 margin）。
        radius = 4.0
        header = option.widget.header() if option.widget is not None else None
        is_first, is_last = _visual_caps(header, index.column())
        rect = QRectF(option.rect)
        rect.setTop(option.rect.y() + 2)
        rect.setHeight(option.rect.height() - 4)
        if is_first:
            rect.setX(4)
        path = QPainterPath()
        if is_first and is_last:
            path.addRoundedRect(rect, radius, radius)
        elif is_first:
            path.moveTo(rect.right(), rect.top())
            path.lineTo(rect.right(), rect.bottom())
            path.lineTo(rect.x() + radius, rect.bottom())
            path.arcTo(
                rect.x(), rect.bottom() - 2 * radius, 2 * radius, 2 * radius, 270, -90
            )
            path.lineTo(rect.x(), rect.top() + radius)
            path.arcTo(rect.x(), rect.top(), 2 * radius, 2 * radius, 180, -90)
            path.closeSubpath()
        elif is_last:
            path.moveTo(rect.x(), rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.arcTo(
                rect.right() - 2 * radius, rect.top(), 2 * radius, 2 * radius, 90, -90
            )
            path.lineTo(rect.right(), rect.bottom() - radius)
            path.arcTo(
                rect.right() - 2 * radius,
                rect.bottom() - 2 * radius,
                2 * radius,
                2 * radius,
                0,
                -90,
            )
            path.lineTo(rect.x(), rect.bottom())
            path.closeSubpath()
        else:
            path.addRect(rect)
        painter.drawPath(path)
        painter.restore()


class FlowConnTree(_ColumnLayoutMixin, TreeView):
    """按客户端连接分组的树视图（平铺 `FlowDataTable` 的树孪生）。

    与平铺表格并列，公共 API（set_source / on_flow_* / set_highlight_ids /
    get_selected_rows / stats_updated…）对齐，好让 `FlowViewerPane` 一层 fan-out
    同时喂两个模型。统计走自有通道：shown = 可见子流数（连接节点不计入），
    非 `proxy.rowCount()`（那是顶层连接数，见方案 §4.3）。
    """

    row_double_clicked = Signal(dict)
    row_selected = Signal(dict)
    stats_updated = Signal(int, int, int)  # 总条数、显示条数（子流）、选中条数
    column_layout_changed = Signal(
        object
    )  # 列布局变更（FlowViewerPane 落盘 + 同步表格）

    def _column_header(self) -> QHeaderView:
        return self.header()

    def __init__(
        self,
        parent: QWidget | None,
        controller,
        capabilities: FlowViewCapabilities | None = None,
    ):
        super().__init__(parent)
        self.controller = controller
        self.capabilities = capabilities or CAPTURE_CAPABILITIES
        self.__init_widget()
        self.__init_view()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.source_model = FlowConnTreeModel(self)
        self.proxy_model = FlowConnProxyModel(self)
        self.context_menu = FlowContextMenu(self, self.controller, self.capabilities)
        self.proxy_model.setSourceModel(self.source_model)
        self.setModel(self.proxy_model)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def __init_view(self):
        self.setSortingEnabled(True)
        self.setWordWrap(False)
        self.setUniformRowHeights(True)
        self.setExpandsOnDoubleClick(True)  # 父节点双击=展开/折叠（Qt 默认）
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.scrollDelagate.verticalSmoothScroll.setDynamicEngineEnabled(False)

        h_header = self.header()
        h_header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        h_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h_header.setMinimumSectionSize(44)
        # 列宽 / Mark 固定宽 / 顺序 / 显隐全由列布局收敛（与平铺表格共用同一份布局，
        # 持久布局的读取与应用在 FlowViewerPane）。
        self._init_columns(default_layout())
        h_header.setFixedHeight(36)
        self.setMinimumWidth(360)
        # 用户点列头才切聚合排序（默认锁首见序，见 FlowConnProxyModel）。
        self.header().sortIndicatorChanged.connect(self.__on_sort_requested)
        self.setItemDelegate(HighlightTreeDelegate(self))

    def __connect_signal_to_slot(self):
        self.proxy_model.rowsInserted.connect(self.__on_sync_visual)
        self.proxy_model.rowsRemoved.connect(self.__on_sync_visual)
        self.proxy_model.modelReset.connect(self.__on_sync_visual)
        self.source_model.rowsInserted.connect(self.__on_sync_visual)
        self.source_model.rowsRemoved.connect(self.__on_sync_visual)
        self.source_model.modelReset.connect(self.__on_sync_visual)

        self.customContextMenuRequested.connect(self.__on_show_context_menu)
        self.context_menu.delete_requested.connect(self.remove_selected)
        if self.capabilities.can_delete:
            QShortcut(QKeySequence.StandardKey.Delete, self).activated.connect(
                self.remove_selected
            )
        self.selectionModel().selectionChanged.connect(self.__on_selection_changed)
        self.doubleClicked.connect(self.__on_row_double_clicked)

    @Slot()
    def __on_sort_requested(self, *_):
        # 首次点列头：从「锁首见序」切到聚合排序（组间聚合、组内按列）。
        self.proxy_model.mark_user_sorted()

    def set_controller(self, controller) -> None:
        self.controller = controller
        self.context_menu.controller = controller
        self.context_menu.export_menu.controller = controller

    def _detail_for(self, proxy_index: QModelIndex | QPersistentModelIndex) -> dict:
        """proxy index → 右键菜单用的字典：连接节点走 connection_detail，
        子流走轻量 summary（菜单不读 body 文本，同 FlowViewerPane.row_summary）。"""
        selection = self._selection_for(proxy_index)
        if selection.get("kind") == "connection":
            return selection
        flow_id = selection.get("id")
        if not flow_id or self.controller is None:
            return {}
        return self.controller.flow_summary(flow_id)

    def _selection_for(self, proxy_index: QModelIndex | QPersistentModelIndex) -> dict:
        """子流只交 id，连接节点的摘要直接来自行快照。"""
        source_index = self.proxy_model.mapToSource(proxy_index)
        node = self.source_model.node_at(source_index)
        if node is not None:
            return self.source_model.connection_detail(node)
        flow = self.source_model.flow_at(source_index)
        return {"id": flow.id} if flow is not None else {}

    def get_selected_rows(self) -> list[FlowRow]:
        """选区 → 行快照列表：连接节点展开为其全部子行（parent→children 映射）。"""
        rows: list[FlowRow] = []
        seen: set[str] = set()
        for index in self.selectionModel().selectedRows():
            source_index = self.proxy_model.mapToSource(index)
            for row in self.source_model.flows_under(source_index):
                if row.id not in seen:
                    seen.add(row.id)
                    rows.append(row)
        return rows

    @Slot()
    def __on_selection_changed(self, selected):
        indexes = selected.indexes()
        if indexes:
            self.row_selected.emit(self._selection_for(indexes[0]))
        QTimer.singleShot(0, self, self.__emit_stats_updated)

    @Slot(QPoint)
    def __on_show_context_menu(self, pos: QPoint):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        if not self.selectionModel().isSelected(index):
            self.setCurrentIndex(index)
        row_data = self._detail_for(index)
        selected_rows = self.get_selected_rows()
        self.context_menu.update_context(-1, row_data, selected_rows)
        self.context_menu.exec(self.viewport().mapToGlobal(pos))

    @Slot()
    def __on_sync_visual(self):
        QTimer.singleShot(0, self, self.__emit_stats_updated)

    def __emit_stats_updated(self):
        total = self.controller.total_count() if self.controller else 0
        shown = self.source_model.child_count()  # 可见子流数，非顶层连接数
        selected = len(self.selectionModel().selectedRows())
        self.stats_updated.emit(total, shown, selected)

    def emit_stats(self) -> None:
        self.__emit_stats_updated()

    @Slot()
    def clear_all(self):
        self.source_model.clear_data()
        self.clearSelection()
        QTimer.singleShot(0, self, self.__emit_stats_updated)

    @Slot()
    def remove_selected(self) -> None:
        flows = self.get_selected_rows()
        if flows:
            self.source_model.remove_flows([row.id for row in flows])

    def set_source(self, source) -> None:
        self.source_model.set_source(source)

    def set_highlight_ids(self, ids: set[str]) -> None:
        self.source_model.set_highlight_ids(ids)

    def on_flow_added(self, flow):
        self.source_model.handle_add(flow)

    def on_flow_updated(self, flow):
        self.source_model.handle_update(flow)

    def on_flow_removed(self, flow, index):
        self.source_model.handle_remove(flow, index)

    def on_view_refreshed(self):
        self.source_model.handle_refresh()

    @Slot()
    def on_locate_selection(self):
        index = self.selectionModel().currentIndex()
        if not index.isValid():
            return
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.horizontalScrollBar().setValue(0)

    def selected_row_data(self) -> dict:
        indexes = self.selectionModel().selectedRows()
        if not indexes:
            return {}
        index = self.selectionModel().currentIndex()
        if not index.isValid() or not self.selectionModel().isSelected(index):
            index = indexes[0]
        return self._selection_for(index)

    @Slot(QModelIndex)
    def __on_row_double_clicked(self, index: QModelIndex):
        # 连接节点双击交给 Qt 展开/折叠，不发详情；仅子流双击开详情。
        source_index = self.proxy_model.mapToSource(index)
        if self.source_model.node_at(source_index) is not None:
            return
        self.row_double_clicked.emit(self._selection_for(index))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_responsive_columns(e.size().width())


class FlowViewerPane(OrientationSplitter):
    """Shared Flow table/detail viewer with consistent interaction semantics."""

    # 当前模式视图的统计转发（平铺/连接树两路都汇到这里再冒泡给消费方）。
    stats_updated = Signal(int, int, int)
    replay_file_requested = Signal()
    block_host_requested = Signal(str)
    edit_in_compose_requested = Signal(str)
    add_to_mock_requested = Signal(list)

    def __init__(
        self,
        parent: QWidget | None = None,
        controller=None,
        capabilities: FlowViewCapabilities | None = None,
        *,
        grouping_menu: bool = False,
    ) -> None:
        super().__init__(parent=parent)
        self.controller = controller
        self._capabilities = capabilities
        self._source: FlowSource | None = None
        self._highlight_ids: set[str] = set()
        self._panel_expanded = False
        self._capture_mode = capabilities is None or capabilities.can_delete
        self._grouping_mode = "flat"
        self._grouping_actions: dict[str, Action] = {}
        self._capture_context = {
            "capture_state": "stopped",
            "endpoint": "",
            "total_count": 0,
            "shown_count": 0,
            "active_filter_count": 0,
        }
        self.table_container = QWidget(self)
        # 模式与菜单勾选始终由 pane 同步。抓包命令栏使用显式选项的下拉菜单，
        # 会话工具栏沿用单击翻转；按钮由宿主领入自己的工具栏。
        if grouping_menu:
            self.mode_button = TransparentDropDownPushButton(
                FluentIcon.MENU, self.tr("视图"), self.table_container
            )
            menu = RoundMenu(self.tr("视图"), self.mode_button)
            group = QActionGroup(menu)
            for mode, text, icon in (
                ("flat", self.tr("平铺"), FluentIcon.MENU),
                ("conn", self.tr("按连接"), FluentIcon.TILES),
            ):
                action = Action(icon, text, menu)
                action.setCheckable(True)
                action.setChecked(mode == self._grouping_mode)
                group.addAction(action)
                action.triggered.connect(
                    lambda _checked=False, mode=mode: self.set_grouping_mode(mode)
                )
                menu.addAction(action)
                self._grouping_actions[mode] = action
            self.mode_button.setMenu(menu)
        else:
            self.mode_button = PushButton(
                FluentIcon.MENU, self.tr("平铺"), self.table_container
            )
            self.mode_button.clicked.connect(self._toggle_grouping_mode)
        self.mode_button.setToolTip(self.tr("切换显示模式：平铺 / 按连接"))

        self.table_stack = QStackedWidget(self.table_container)
        self.table = FlowDataTable(self.table_container, controller, capabilities)
        self.tree: FlowConnTree | None = None
        self.empty_state = FlowEmptyState(self.table_container)
        self.table_stack.addWidget(self.table)
        self.table_stack.addWidget(self.empty_state)
        container_layout = QVBoxLayout(self.table_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(self.table_stack)
        self.panel: FlowDataPanel | None = None
        self.addWidget(self.table_container)
        self.addWidget(QWidget(self))  # 保留可拖动分割条，首次展开才创建编辑器。
        self.setStretchFactor(0, 1)
        self.setStretchFactor(1, 0)
        self.collapse_panel()

        self.splitterMoved.connect(self._on_splitter_moved)

        # 列布局：pane 是唯一读配置/落盘/同步两视图的枢纽（§4.2）。启动读一次持久布局
        # 应用到已创建的视图，连接树首次打开时取最新布局；任一视图变更仍统一落盘。
        self._column_layout = load_layout()
        self.table.apply_column_layout(self._column_layout)
        self._connect_view(self.table)

        self._refresh_empty_state()

    def _connect_view(self, view: FlowDataTable | FlowConnTree) -> None:
        view.row_selected.connect(self._on_row_selected)
        view.row_double_clicked.connect(self._on_row_double_clicked)
        view.stats_updated.connect(self._on_stats_updated)
        view.column_layout_changed.connect(self._on_column_layout_changed)
        menu = view.context_menu
        menu.replay_file_requested.connect(self.replay_file_requested)
        menu.block_host_requested.connect(self.block_host_requested)
        menu.edit_in_compose_requested.connect(self.edit_in_compose_requested)
        menu.add_to_mock_requested.connect(self.add_to_mock_requested)

    def _ensure_tree(self) -> FlowConnTree:
        if self.tree is None:
            tree = FlowConnTree(
                self.table_container, self.controller, self._capabilities
            )
            self.tree = tree
            self.table_stack.addWidget(tree)
            tree.apply_column_layout(self._column_layout)
            tree.set_highlight_ids(self._highlight_ids)
            self._connect_view(tree)
            # 未打开时不积压增量信号；数据源已保存最新快照，首次整树重建即可。
            if self._source is not None:
                tree.set_source(self._source)
        return self.tree

    def _ensure_panel(self) -> FlowDataPanel:
        if self.panel is None:
            from ferret.apps.common.flow.detail import FlowDataPanel

            placeholder = self.widget(1)
            assert placeholder is not None
            sizes = self.sizes()
            hidden = placeholder.isHidden()
            # 先挂占位件，避免 QSplitter 自动插入第三个直接子控件。
            panel = FlowDataPanel(placeholder, self.controller, self._capabilities)
            self.panel = panel
            self.replaceWidget(1, panel)
            placeholder.deleteLater()
            panel.collapseRequested.connect(self.collapse_panel)
            panel.setVisible(not hidden)
            self.setSizes(sizes)
        return self.panel

    @Slot(object)
    def _on_column_layout_changed(self, layout: ColumnLayout) -> None:
        """任一视图广播列布局变更：落盘 + 同步到另一视图。

        发起视图已在 `_commit_column_layout` / `_on_section_resized` 里应用过自身，
        这里只需把另一视图对齐（`apply_column_layout` 走 `_applying_layout` 闸门，
        不会反弹回本槽造成回环）。
        """
        self._column_layout = layout
        save_layout(layout)
        other = self.tree if self.sender() is self.table else self.table
        if other is not None:
            other.apply_column_layout(layout)

    def _current_view(self) -> FlowDataTable | FlowConnTree:
        """当前模式对应的视图（平铺表格 / 连接树）。"""
        if self._grouping_mode == "conn" and self.tree is not None:
            return self.tree
        return self.table

    @Slot()
    def _toggle_grouping_mode(self) -> None:
        """按钮点击：在平铺 / 按连接之间翻转到另一模式。"""
        self.set_grouping_mode("conn" if self._grouping_mode == "flat" else "flat")

    @Slot(str)
    def set_grouping_mode(self, mode: str) -> None:
        """切换显示模式：flat（平铺）⇄ conn（按连接分组）。"""
        if mode not in ("flat", "conn") or mode == self._grouping_mode:
            return
        if mode == "conn":
            self._ensure_tree()
        self._grouping_mode = mode
        self._sync_mode_button()
        # 换页后按当前视图重算统计（shown 口径不同：平铺=行数，树=子流数）。
        self._current_view().emit_stats()
        self._refresh_empty_state()

    def _sync_mode_button(self) -> None:
        """让按钮的图标 / 文案反映当前模式。"""
        if self._grouping_actions:
            for mode, action in self._grouping_actions.items():
                action.setChecked(mode == self._grouping_mode)
            return
        if self._grouping_mode == "conn":
            self.mode_button.setIcon(FluentIcon.TILES)
            self.mode_button.setText(self.tr("按连接"))
        else:
            self.mode_button.setIcon(FluentIcon.MENU)
            self.mode_button.setText(self.tr("平铺"))

    # ------------------------------------------------------------------
    # fan-out：一层转发同时喂平铺与连接树两个模型（见方案 §3.3）
    # ------------------------------------------------------------------
    def set_source(self, source) -> None:
        self._source = source
        self.table.set_source(source)
        if self.tree is not None:
            self.tree.set_source(source)

    def set_highlight_ids(self, ids: set[str]) -> None:
        self._highlight_ids = set(ids)
        self.table.set_highlight_ids(self._highlight_ids)
        if self.tree is not None:
            self.tree.set_highlight_ids(self._highlight_ids)

    def on_flow_added(self, flow) -> None:
        self.table.on_flow_added(flow)
        if self.tree is not None:
            self.tree.on_flow_added(flow)

    def on_flow_updated(self, flow) -> None:
        self.table.on_flow_updated(flow)
        if self.tree is not None:
            self.tree.on_flow_updated(flow)

    def on_flow_removed(self, flow, index) -> None:
        self.table.on_flow_removed(flow, index)
        if self.tree is not None:
            self.tree.on_flow_removed(flow, index)

    def on_view_refreshed(self) -> None:
        self.table.on_view_refreshed()
        if self.tree is not None:
            self.tree.on_view_refreshed()

    def on_locate_selection(self) -> None:
        self._current_view().on_locate_selection()

    def clear_all(self) -> None:
        # 平铺侧清源（source.clear()）+ 复位，连接树再从空源整树重建。
        self.table.clear_all()
        if self.tree is not None:
            self.tree.on_view_refreshed()

    def set_controller(self, controller) -> None:
        self.controller = controller
        self.table.set_controller(controller)
        if self.tree is not None:
            self.tree.set_controller(controller)
        if self.panel is not None:
            self.panel.set_controller(controller)
        self._refresh_empty_state()

    def is_panel_expanded(self) -> bool:
        sizes = self.sizes()
        return len(sizes) >= 2 and sizes[1] > 0

    @Slot(dict)
    def _on_row_selected(self, data: dict) -> None:
        """Update details only when the outer detail panel is already open."""
        if self.sender() is not self._current_view():
            return
        if self.is_panel_expanded():
            self._show_selection(data)

    def _show_selection(self, selection: dict) -> None:
        panel = self._ensure_panel()
        data = self._detail_for_selection(selection)
        if data:
            panel.set_data(data)
        else:
            # 无选区时保持中性空页，并撤下旧 id，避免继续接收旧流量的消息。
            panel.set_data({})

    def _detail_for_selection(self, selection: dict) -> dict:
        if selection.get("kind") == "connection":
            return selection
        flow_id = selection.get("id")
        if not flow_id or self.controller is None:
            return {}
        return self.controller.flow_summary(flow_id)

    @Slot(dict)
    def _on_row_double_clicked(self, data: dict) -> None:
        """Open details and normalize the outer splitter to 50/50."""
        if self.sender() is not self._current_view():
            return
        self._open_detail(data)

    def _open_detail(self, selection: dict) -> None:
        data = self._detail_for_selection(selection)
        if not data:
            return
        # 空态会把面板整个隐藏（4ee294f），而 QSplitter 对隐藏件不分配尺寸；
        # 有行可双击就说明面板不该再藏着，先恢复显示再等分。
        panel = self._ensure_panel()
        panel.setVisible(True)
        panel.set_data(data)
        self._panel_expanded = True
        QTimer.singleShot(0, self, self._apply_equal_sizes)

    @Slot(int, int)
    def _on_splitter_moved(self, _position: int, _index: int) -> None:
        expanded = self.is_panel_expanded()
        if expanded and not self._panel_expanded:
            self._show_selection(self._current_view().selected_row_data())
        self._panel_expanded = expanded

    @Slot()
    def collapse_panel(self) -> None:
        self._panel_expanded = False
        self.collapse(1)

    def _apply_equal_sizes(self) -> None:
        self.set_equal_sizes()

    @Slot()
    def open_selected(self) -> None:
        data = self._current_view().selected_row_data()
        if data:
            self._open_detail(data)

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
    def _on_stats_updated(self, total: int, shown: int, selected: int) -> None:
        # 隐藏视图的增量/选区信号仍会到达，但不能覆盖当前视图的选中计数与空态。
        if self.sender() is not self._current_view():
            return
        self._capture_context["total_count"] = total
        self._capture_context["shown_count"] = shown
        self._refresh_empty_state()
        self.stats_updated.emit(total, shown, selected)

    def _refresh_empty_state(self) -> None:
        detail_widget = self.widget(1)
        assert detail_widget is not None
        total = int(self._capture_context["total_count"])
        shown = int(self._capture_context["shown_count"])
        state = str(self._capture_context["capture_state"])
        filters = int(self._capture_context["active_filter_count"])

        # 命令菜单空表时保留位置并置灰，避免第一条流量到来时按钮横向跳动。
        self.mode_button.setVisible(bool(self._grouping_actions) or shown > 0)
        self.mode_button.setEnabled(shown > 0)

        if shown > 0:
            self.table_stack.setCurrentWidget(self._current_view())
            # isHidden() 只认「被刻意隐藏」；isVisible() 会把窗口未显示误判进来，
            # 导致显示前每次 stats 更新都白白 collapse 一次。
            if detail_widget.isHidden():
                detail_widget.setVisible(True)
                self.collapse_panel()
            return

        self.table_stack.setCurrentWidget(self.empty_state)
        self._panel_expanded = False
        detail_widget.setVisible(False)
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
