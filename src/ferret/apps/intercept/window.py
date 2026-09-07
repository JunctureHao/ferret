"""断点窗口：一个独立的、非模态的顶层窗口，装拦截队列和请求/响应编辑器。

**为什么不是模态框。** 断点一旦命中就可能同时攥住一大把流量 —— 原生 `Intercept` 的
`request` / `response` 两个钩子共用同一个过滤器，所以一条规则命中一次交互会停两次
（实测 12 个并发请求 = 请求期 12 条 + 响应期 12 条 = 24 次停顿）。模态会把「断点总开关」
锁在遮罩外面，用户写宽了一条规则就再也救不回来；也挡住「回捕获页抄个 token 再填进来」
这类真实动作。所以是一个抢焦点、但不阻塞的独立窗口，队列本身长在窗口里 —— 12 条并发是
**一个窗口 + 一列队列**，不是 12 个叠起来的对话框。

**为什么不能用 `MessageBoxBase`。** 仓库里的对话框基类是铺满父窗口的 `QDialog` +
60% 遮罩（`mask_dialog_base.py:26-29` 把自己 `setGeometry(0, 0, parent.width(),
parent.height())`），遮罩会吃掉主窗口的点击 —— 即使 `show()` 而不是 `exec()` 也一样挡。

基类取 `FluentWidget`（`BackgroundAnimationWidget` + `FramelessWindow`）：它自己就
`setTitleBar(FluentWidgetTitleBar(self))`、接好 `qconfig.themeChangedFinished`、
Win11 上开 mica，主题跟着主窗口走，不用另外接线。

**构造时 `parent` 恒为 None，这不是疏忽。** `qframelesswindow` 的
`WindowsFramelessWindowBase.updateFrameless()` 只做
`setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint | ...)` —— 它**不会**补上
`Qt.Window`。给它一个 Qt 父对象，它就成了嵌在父窗口里的子控件而不是顶层窗口。生命周期
由 `MainWindow` 持一个 Python 引用来管（见 `apps/window.py`）。
"""

from PySide6.QtCore import (
    QCoreApplication,
    QItemSelection,
    QPoint,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    FluentWidget,
    PushButton,
    RoundMenu,
    TableView,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.common.window import center_window
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.dialogs import HeldFlowsChoice, HeldFlowsCloseDialog
from ferret.apps.intercept.editors import RequestPanel, ResponsePanel
from ferret.apps.intercept.models import HeldFlowTableModel
from ferret.core.mitm import HTTPFlow


# 写成函数：模块级求值赶在翻译器安装之前（`core/application.py` 顶层就 import 了
# 主窗口，那时 `_init_i18n()` 还没跑）。
def _title() -> str:
    return QCoreApplication.translate("InterceptWindow", "断点")


class InterceptWindow(FluentWidget):
    """命中断点的流量停在这里，改完放行或丢弃。

    左列是断点列表（「请求方式 + URL + 阶段」，只读，选择驱动右侧），右侧是
    **当前那条流量所处阶段的面板**：请求期的流给请求面板（方法 + URL + 参数/
    请求头/请求体），响应期的流给响应面板（状态码 + 响应头/响应体）—— 来的是什么
    阶段就给什么面板，没有阶段切换标签、没有占位页、没有只读锁。放行/丢弃图标
    按钮长在面板页头行上，作用于当前选中的那条。

    停在哪个阶段判的是 `flow.response is None`，与规则选了什么阶段无关；写回来源
    就是当前显示的面板。

    这个窗口**不提供伪造响应**（内核 `MitmFacade.fake_response` 保留，供其他入口
    使用），也没有「撤销」。批量动作有两处：列表右键菜单（放行/丢弃选中项）和
    底部状态条的「放行全部」。
    """

    # 队列从空变成非空、窗口刚刚自己弹出来时发一次，带上队列条数。
    # 托盘提醒交给 `MainWindow` 去发：边沿判定只在这里做一次，两处各判一次必然对不齐。
    attention_requested = Signal(int)

    def __init__(self, controller: InterceptController) -> None:
        # parent 恒为 None，理由见模块 docstring。
        super().__init__()
        self.controller = controller
        # 当前编辑的流量 id。刷新队列时用它认人：快照每次都是新对象，只有 id 稳定。
        self._current_id: str = ""
        # 刷新队列时置位：换表会先清掉选中项，那一轮选中变化不该当成用户换了流量。
        self._restoring = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._on_flows_changed(self.controller.flows)

    # —— 构造 ——

    def __init_widget(self):
        self.setObjectName("InterceptWindow")
        self.setWindowIcon(QIcon(":/icon"))
        self._sync_title(0)

        self.flow_model = HeldFlowTableModel(self)
        self.flow_table = TableView(self)
        self.flow_table.verticalHeader().hide()
        self.flow_table.verticalHeader().setDefaultSectionSize(36)
        self.flow_table.setModel(self.flow_model)
        self.flow_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.flow_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.flow_table.setWordWrap(False)
        header = self.flow_table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate([80, 320, 90]):
            self.flow_table.setColumnWidth(i, w)
        # URL 是这张表里唯一值得占满的列，其余两列宽度固定。
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.flow_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.request_panel = RequestPanel(self)
        self.response_panel = ResponsePanel(self)
        # 没有选中项时的占位：不给一张能改却改不出去的空表单。
        self.no_selection_page = self.__build_no_selection_page()

        # 阶段面板：来的是什么阶段就显示什么，代码里没有任何手动切换入口。
        self.editor_panel = QStackedWidget(self)
        self.editor_panel.addWidget(self.request_panel)
        self.editor_panel.addWidget(self.response_panel)
        self.editor_panel.addWidget(self.no_selection_page)

        self.editor_box = QWidget(self)
        editor_layout = QVBoxLayout(self.editor_box)
        editor_layout.setContentsMargins(12, 8, 12, 12)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(self.editor_panel, 1)

        self.queue_splitter = BaseSplitter(Qt.Orientation.Horizontal, self)
        self.queue_splitter.addWidget(self.flow_table)
        self.queue_splitter.addWidget(self.editor_box)
        self.queue_splitter.setStretchFactor(0, 2)
        self.queue_splitter.setStretchFactor(1, 3)

        self.queue_empty_page = self.__build_queue_empty_page()
        self.queue_stack = QStackedWidget(self)
        self.queue_stack.addWidget(self.queue_splitter)
        self.queue_stack.addWidget(self.queue_empty_page)

        # 底部状态条：待处理数 + 当前选中 + 「放行全部」。批量动作原来只有右键菜单，
        # 给一个看得见的入口。
        self.status_label = CaptionLabel(self)
        self.release_all_button = PushButton(FluentIcon.SEND, self.tr("放行全部"), self)
        self.release_all_button.setToolTip(
            self.tr("放行全部挂起的流量，不套用任何编辑")
        )
        self.status_bar = QWidget(self)
        status_layout = QHBoxLayout(self.status_bar)
        status_layout.setContentsMargins(12, 4, 12, 4)
        status_layout.setSpacing(8)
        status_layout.addWidget(self.status_label)
        status_layout.addStretch(1)
        status_layout.addWidget(self.release_all_button)

    def __build_queue_empty_page(self) -> QWidget:
        """兜底页。队列清空会自动隐藏窗口，所以正常路径下它不该被看见。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("当前没有被拦下的流量"), page)
        hint = CaptionLabel(
            self.tr("命中断点规则的流量会停在这里，等你改完再放行"),
            page,
        )
        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return page

    def __build_no_selection_page(self) -> QWidget:
        """没有选中项时的占位。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = CaptionLabel(self.tr("选择一条挂起的流量进行编辑"), page)
        layout.addStretch(1)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return page

    def __init_layout(self):
        layout = QVBoxLayout(self)
        # 顶上让出标题栏的高度：`FluentWidgetTitleBar` 是浮在窗口上的兄弟控件、不进
        # 布局，不留边距内容就会被它压住（`FluentWindow` 是同样的做法，只是它把 48
        # 写死了 —— 这里的标题栏按按钮高度自适应，读它更准）。
        layout.setContentsMargins(0, self.titleBar.height(), 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.queue_stack, 1)
        layout.addWidget(self.status_bar)

    def __connect_signal_to_slot(self):
        self.flow_table.selectionModel().selectionChanged.connect(
            self._on_flow_selection_changed
        )
        self.flow_table.customContextMenuRequested.connect(self._on_flow_context_menu)

        self.request_panel.releaseRequested.connect(self._on_release)
        self.request_panel.dropRequested.connect(self._on_drop_current)
        self.response_panel.releaseRequested.connect(self._on_release)
        self.response_panel.dropRequested.connect(self._on_drop_current)

        self.release_all_button.clicked.connect(self.controller.release_all)

        self.controller.flows_changed.connect(self._on_flows_changed)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._on_release)

    # —— 弹出 / 收起 ——

    def pop_up(self) -> None:
        """把窗口摆到用户眼前。断点页的「拦截队列」入口也走这里。"""
        if not self.isVisible():
            self.setMinimumSize(1080, 700 + self.titleBar.height())
            center_window(self)
        self.show()
        self.raise_()
        self.activateWindow()

    def _sync_title(self, count: int) -> None:
        if not count:
            self.setWindowTitle(_title())
            return
        # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
        self.setWindowTitle(self.tr("断点 · {} 条待处理").format(count))

    # —— 队列辅助 ——

    def _selected_flows(self) -> list[HTTPFlow]:
        rows = self.flow_table.selectionModel().selectedRows()
        flows = (self.flow_model.flow_at(index.row()) for index in rows)
        return [flow for flow in flows if flow is not None]

    def _current_flow(self) -> HTTPFlow | None:
        """当前正在编辑的那一条：只有**单选**才算，多选时没有「哪一条」可言。"""
        flows = self._selected_flows()
        return flows[0] if len(flows) == 1 else None

    def _panel_matches(self, flow: HTTPFlow | None) -> bool:
        """当前摆出的面板是否就是这条流量该有的那一个。

        id 相同不代表不用重载：BOTH 规则下同一条流会先停请求期、放行后再停
        响应期 —— 阶段变了面板必须跟着换，不然用户看到的是旧一期的内容。
        """
        current = self.editor_panel.currentWidget()
        if flow is None:
            return current is self.no_selection_page
        expected = (
            self.response_panel if flow.response is not None else self.request_panel
        )
        return current is expected

    def _load_panel(self, flow: HTTPFlow | None) -> None:
        """按当前流量的阶段摆面板：来的是什么阶段就给什么面板。"""
        if flow is None:
            self.request_panel.clear()
            self.response_panel.clear()
            self.editor_panel.setCurrentWidget(self.no_selection_page)
            self._sync_status()
            return
        if flow.response is None:
            self.request_panel.load(flow)
            self.editor_panel.setCurrentWidget(self.request_panel)
        else:
            self.response_panel.load(flow)
            self.editor_panel.setCurrentWidget(self.response_panel)
        self._sync_status()

    # —— 写回 / 丢弃 ——

    def _write_back(self) -> bool:
        """把当前面板的内容写回选中的那条流量并放行。"""
        flow = self._current_flow()
        if flow is None:
            return False
        if flow.response is None:
            return self.controller.apply_request(
                flow.id, self.request_panel.edit(), release=True
            )
        return self.controller.apply_response(
            flow.id, self.response_panel.edit(), release=True
        )

    @Slot()
    def _on_drop_current(self):
        """面板页头的「丢弃」：只丢当前选中的那一条。"""
        flow = self._current_flow()
        if flow is not None:
            self.controller.drop_flows([flow.id])

    # —— 队列槽 ——

    @Slot(QItemSelection, QItemSelection)
    def _on_flow_selection_changed(self, *_args):
        if self._restoring:
            # 刷新队列时的选中变化是我们自己造的，交给 `_on_flows_changed` 收尾。
            return
        flow = self._current_flow()
        flow_id = flow.id if flow is not None else ""
        if flow_id != self._current_id or not self._panel_matches(flow):
            self._current_id = flow_id
            self._load_panel(flow)
        else:
            self._sync_status()

    @Slot(list)
    def _on_flows_changed(self, flows: list):
        """整表换一批新快照，并把选中项按 id 挪回原来那条。

        换表会顺手清掉选中项，而清空选中项又会把编辑器清空 —— 别人的流量刚被拦下就
        把用户正在编辑的内容冲掉，所以整段用 `_restoring` 挡住，最后只在**真的换了
        一条流量**时才重载面板。
        """
        was_empty = self.flow_model.rowCount() == 0

        self._restoring = True
        try:
            self.flow_model.set_flows(flows)
            row = self.flow_model.row_of(self._current_id) if self._current_id else -1
            if row < 0:
                row = 0 if flows else -1
            if row >= 0:
                self.flow_table.selectRow(row)
            else:
                self.flow_table.clearSelection()
        finally:
            self._restoring = False

        flow = self._current_flow()
        flow_id = flow.id if flow is not None else ""
        # 构造时 `_current_id` 是空串，与「无选中」同值；阶段变化（请求期→响应期）
        # 时 id 不变 —— 两头都不能只比 id，见 `_panel_matches`。
        if flow_id != self._current_id or not self._panel_matches(flow):
            self._current_id = flow_id
            self._load_panel(flow)
        self.queue_stack.setCurrentWidget(
            self.queue_splitter if flows else self.queue_empty_page
        )
        self._sync_title(len(flows))
        self._sync_status()

        if not flows:
            # 队列空了就收起来，不留一个空窗挡着主窗口。
            self.hide()
        elif was_empty:
            # 只在空→非空这一次抢焦点。后面每有一条被拦下就 activateWindow() 一次，
            # 会把正在打字的人从输入框里踢出去 —— 一条规则命中一次交互本来就要停两次。
            self.pop_up()
            self.attention_requested.emit(len(flows))

    @Slot()
    def _on_release(self):
        """放行（Ctrl+Enter / 面板按钮 / 右键菜单）。单选时连带写回当前改动再放行；
        多选时只放行 —— 改动属于哪一条不明确。"""
        flows = self._selected_flows()
        if not flows:
            return
        if len(flows) == 1:
            self._write_back()
            return
        self.controller.release_flows([flow.id for flow in flows])

    @Slot()
    def _on_drop(self):
        flows = self._selected_flows()
        if flows:
            self.controller.drop_flows([flow.id for flow in flows])

    @Slot(QPoint)
    def _on_flow_context_menu(self, pos: QPoint):
        flows = self._selected_flows()
        if not flows:
            return
        menu = RoundMenu(parent=self.flow_table)
        release_action = BaseAction(
            icon=FluentIcon.SEND, text=self.tr("放行"), parent=menu
        )
        release_action.triggered.connect(self._on_release)
        menu.addAction(release_action)
        drop_action = BaseAction(
            icon=FluentIcon.CANCEL, text=self.tr("丢弃"), parent=menu
        )
        drop_action.triggered.connect(self._on_drop)
        menu.addAction(drop_action)
        menu.exec(self.flow_table.viewport().mapToGlobal(pos))

    # —— 状态条 ——

    def _sync_status(self) -> None:
        """待处理数 + 当前选中。文案整句留在外面，变量交给 format。"""
        count = self.flow_model.rowCount()
        flow = self._current_flow()
        if flow is not None:
            summary = self.tr("{} 条待处理 · 正在编辑 {}").format(
                count, flow.request.pretty_url
            )
        elif count:
            summary = self.tr("{} 条待处理").format(count)
        else:
            summary = ""
        self.status_label.setText(summary)
        self.release_all_button.setEnabled(count > 0)

    # —— 关窗 ——

    def _confirm_close(self) -> HeldFlowsChoice:
        """问一句「还挂着的怎么办」。抽成方法是为了测试能替掉，不必真跑 `exec()`。"""
        dialog = HeldFlowsCloseDialog(self.flow_model.rowCount(), self)
        dialog.exec()
        # 读 `choice` 而不是 `exec()` 的返回值：「保持挂起」也算关得掉。
        return dialog.choice

    def closeEvent(self, event):
        """队列非空就不许默默关掉。

        窗口一藏，那几条流量还钉在 `wait_for_resume()` 上，客户端就那么转圈，而界面上
        再也看不到它们（`handle_hook` 在等待期间 `disarm()` 了看门狗，`tcp_timeout`
        不兜底，本轮也刻意没做超时自动放行）。所以「保持挂起」得是个明示的选择。
        """
        if self.flow_model.rowCount() == 0:
            super().closeEvent(event)
            return
        choice = self._confirm_close()
        if choice == HeldFlowsChoice.CANCEL:
            event.ignore()
            return
        super().closeEvent(event)

    # —— 提示 ——

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str):
        # 只在自己露着的时候弹：藏起来的窗口上弹 InfoBar 等于把反馈丢了，那种情况下
        # 动作是从断点页发起的，由断点页弹在主窗口上。
        if self.isVisible():
            show_error(title, detail, self)

    @Slot(str)
    def _on_operation_succeeded(self, message: str):
        if self.isVisible():
            show_success(self.tr("成功"), message, self)
