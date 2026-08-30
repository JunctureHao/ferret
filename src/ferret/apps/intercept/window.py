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
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SegmentedWidget,
    TableView,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.common.window import center_window
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.dialogs import HeldFlowsChoice, HeldFlowsCloseDialog
from ferret.apps.intercept.editors import RequestEditor, ResponseEditor
from ferret.apps.intercept.models import HeldFlowTableModel
from ferret.core.mitm import HTTPFlow


# 写成函数：模块级求值赶在翻译器安装之前（`core/application.py` 顶层就 import 了
# 主窗口，那时 `_init_i18n()` 还没跑）。
def _title() -> str:
    return QCoreApplication.translate("InterceptWindow", "Breakpoints")


class InterceptWindow(FluentWidget):
    """命中断点的流量停在这里，改完放行或丢弃。

    上半是队列，每行「请求方式 + URL + 放行 / 丢弃按钮组」（参考 Reqable 的断点
    列表）；下半是编辑区，「请求 / 响应」两个标签（`SegmentedWidget`，内容仍是
    `RequestEditor` / `ResponseEditor` 那套 common/edit 组件）：

    - 请求期（还没有响应）：请求标签可编辑；响应标签还没有内容可看，显示一句提示。
    - 响应期：响应标签可编辑；请求早发出去了，请求标签锁成只读供查看。

    停在哪个阶段判的是 `flow.response is None`，与规则选了什么阶段无关。写回也按
    阶段选编辑器 —— 跟用户当前停在哪个标签没关系（响应期停在请求标签上按
    Ctrl+Enter，写回的仍是响应编辑器里的改动）。

    这个窗口**不提供伪造响应**（内核 `MitmFacade.fake_response` 保留，供其他入口
    使用）。同样没有「撤销」「放行全部」—— 这两个动作留在控制器上，窗口不调用。
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
        # self.resize(1080, 700)
        self._sync_title(0)

        self.flow_model = HeldFlowTableModel(self)
        self.flow_table = TableView(self)
        self.flow_table.verticalHeader().hide()
        # 行高放宽到 40，给行内按钮组留位置（见 `__build_row_actions`）。
        self.flow_table.verticalHeader().setDefaultSectionSize(40)
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
        for i, w in enumerate([70, 320, 180]):
            self.flow_table.setColumnWidth(i, w)
        # URL 是这张表里唯一值得占满的列，其余两列宽度固定。
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.flow_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.request_editor = RequestEditor(self)
        self.response_editor = ResponseEditor(self)
        # 请求期停在「响应」标签时看到的占位页：响应还不存在，没有可编的东西。
        self.response_pending_page = self.__build_response_pending_page()

        # 编辑区导航：与流量详情页同一套「SegmentedWidget + 栈」的做法，信号在
        # `__connect_signal_to_slot` 里接，这里两边各自先置一次。
        self.editor_nav = SegmentedWidget(self)
        self.editor_nav.addItem(routeKey="Request", text=self.tr("Request"))
        self.editor_nav.addItem(routeKey="Response", text=self.tr("Response"))
        self.editor_nav.setItemFontSize(12)

        self.editor_panel = QStackedWidget(self)
        self.editor_panel.addWidget(self.request_editor)
        self.editor_panel.addWidget(self.response_editor)
        self.editor_panel.addWidget(self.response_pending_page)
        self.editor_nav.setCurrentItem("Request")
        self.editor_panel.setCurrentWidget(self.request_editor)

        self.editor_box = QWidget(self)
        editor_layout = QVBoxLayout(self.editor_box)
        editor_layout.setContentsMargins(12, 8, 12, 12)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(self.editor_nav)
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

    def __build_queue_empty_page(self) -> QWidget:
        """兜底页。队列清空会自动隐藏窗口，所以正常路径下它不该被看见。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("Nothing is held right now"), page)
        hint = CaptionLabel(
            self.tr(
                "Traffic that matches a breakpoint rule stops here and waits "
                "until you release it"
            ),
            page,
        )
        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return page

    def __build_response_pending_page(self) -> QWidget:
        """请求期停在「响应」标签时看到的占位页。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = CaptionLabel(
            self.tr("No response yet — this flow is held before the request goes out"),
            page,
        )
        layout.addStretch(1)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        return page

    def __build_row_actions(self, flow: HTTPFlow) -> QWidget:
        """一行一组「放行 / 丢弃」，闭包直接攥住这一行的快照。

        快照每次刷新都换新对象，按钮组跟着整表重建（见 `_populate_row_buttons`），
        所以闭包不会攥到一条早已放行的旧流量。
        """
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        release_btn = PrimaryPushButton(FluentIcon.SEND, self.tr("Release"), host)
        drop_btn = PushButton(FluentIcon.CANCEL, self.tr("Drop"), host)
        for btn in (release_btn, drop_btn):
            btn.setFixedHeight(28)
        release_btn.setToolTip(self.tr("Write the edits back and release this flow"))
        drop_btn.setToolTip(self.tr("Kill this flow; the client receives nothing at all"))
        release_btn.clicked.connect(lambda: self._on_row_release(flow))
        drop_btn.clicked.connect(lambda: self._on_row_drop(flow))
        layout.addWidget(release_btn)
        layout.addWidget(drop_btn)
        return host

    def __init_layout(self):
        layout = QVBoxLayout(self)
        # 顶上让出标题栏的高度：`FluentWidgetTitleBar` 是浮在窗口上的兄弟控件、不进
        # 布局，不留边距内容就会被它压住（`FluentWindow` 是同样的做法，只是它把 48
        # 写死了 —— 这里的标题栏按按钮高度自适应，读它更准）。
        layout.setContentsMargins(0, self.titleBar.height(), 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.queue_stack, 1)

    def __connect_signal_to_slot(self):
        self.editor_nav.currentItemChanged.connect(self._on_nav_changed)
        self.flow_table.selectionModel().selectionChanged.connect(
            self._on_flow_selection_changed
        )
        self.flow_table.customContextMenuRequested.connect(self._on_flow_context_menu)

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
        self.setWindowTitle(self.tr("Breakpoints · {} pending").format(count))

    # —— 队列辅助 ——

    def _selected_flows(self) -> list[HTTPFlow]:
        rows = self.flow_table.selectionModel().selectedRows()
        flows = (self.flow_model.flow_at(index.row()) for index in rows)
        return [flow for flow in flows if flow is not None]

    def _current_flow(self) -> HTTPFlow | None:
        """当前正在编辑的那一条：只有**单选**才算，多选时没有「哪一条」可言。"""
        flows = self._selected_flows()
        return flows[0] if len(flows) == 1 else None

    def _load_editors(self, flow: HTTPFlow | None) -> None:
        if flow is None:
            self.request_editor.clear()
            self.response_editor.clear()
            self.request_editor.set_read_only(True)
            self.response_editor.set_read_only(True)
            self._set_current_tab("Request")
            return
        self.request_editor.load(flow)
        self.response_editor.load(flow)
        # 响应期的请求早发出去了，改它没有任何效果，锁成只读比让人白改一场好。
        # 请求期反过来：响应还不存在，响应编辑器只清空占位。
        response_phase = flow.response is not None
        self.request_editor.set_read_only(response_phase)
        self.response_editor.set_read_only(not response_phase)
        # 换流量时落在它的主场上：请求期开请求标签、响应期开响应标签。
        # 用户手动停在另一个标签的偏好不跨流量保留 —— 停错半边看不见可编辑的内容。
        self._set_current_tab("Response" if response_phase else "Request")

    # —— 编辑区标签 ——

    def _set_current_tab(self, key: str) -> None:
        """代码侧切标签。`setCurrentItem` 值没变时信号不来，页面得在这里补摆。"""
        self.editor_nav.setCurrentItem(key)
        self._apply_tab(key)

    def _apply_tab(self, key: str) -> None:
        if key != "Response":
            self.editor_panel.setCurrentWidget(self.request_editor)
            return
        flow = self._current_flow()
        if flow is not None and flow.response is not None:
            self.editor_panel.setCurrentWidget(self.response_editor)
        else:
            # 请求期没有响应可编：占位页说清楚，别给一张能改却改不出去的空表单。
            self.editor_panel.setCurrentWidget(self.response_pending_page)

    @Slot(str)
    def _on_nav_changed(self, key: str):
        """用户点标签（`currentItemChanged`：点选和代码置位两条路都走这里）。"""
        self._apply_tab(key)

    # —— 队列行内按钮 ——

    def _populate_row_buttons(self) -> None:
        """给每一行的操作格挂上「放行 / 丢弃」按钮组。"""
        for row in range(self.flow_model.rowCount()):
            flow = self.flow_model.flow_at(row)
            if flow is None:
                continue
            self.flow_table.setIndexWidget(
                self.flow_model.index(row, HeldFlowTableModel.ACTIONS_COLUMN),
                self.__build_row_actions(flow),
            )

    def _clear_row_buttons(self) -> None:
        """拆掉上一轮的按钮组。

        必须趁**模型还没重置**做：`setIndexWidget(index, None)` 会顺手删掉旧控件，
        而索引还有效；等 `set_flows` 重置完，旧索引全失效，就只剩指望视图自己回收了。
        """
        for row in range(self.flow_model.rowCount()):
            index = self.flow_model.index(row, HeldFlowTableModel.ACTIONS_COLUMN)
            if self.flow_table.indexWidget(index) is not None:
                self.flow_table.setIndexWidget(index, None)

    def _on_row_release(self, flow: HTTPFlow) -> None:
        """行内「放行」：先把这行选成当前流量（编辑器跟着切过去），再写回 + 放行。"""
        current = self._current_flow()
        if current is None or current.id != flow.id:
            row = self.flow_model.row_of(flow.id)
            if row < 0:
                # 刷新间隙这条流量已经不在队列里了，别把别的流量放出去。
                return
            self.flow_table.selectRow(row)
        self._write_back()

    def _on_row_drop(self, flow: HTTPFlow) -> None:
        self.controller.drop_flows([flow.id])

    def _write_back(self) -> bool:
        """把当前编辑器的内容写回选中的那条流量并放行，按它所处的阶段选编辑器。"""
        flow = self._current_flow()
        if flow is None:
            return False
        if flow.response is None:
            return self.controller.apply_request(
                flow.id, self.request_editor.edit(), release=True
            )
        return self.controller.apply_response(
            flow.id, self.response_editor.edit(), release=True
        )

    # —— 队列槽 ——

    @Slot(QItemSelection, QItemSelection)
    def _on_flow_selection_changed(self, *_args):
        if self._restoring:
            # 刷新队列时的选中变化是我们自己造的，交给 `_on_flows_changed` 收尾。
            return
        flow = self._current_flow()
        flow_id = flow.id if flow is not None else ""
        if flow_id != self._current_id:
            self._current_id = flow_id
            self._load_editors(flow)

    @Slot(list)
    def _on_flows_changed(self, flows: list):
        """整表换一批新快照，并把选中项按 id 挪回原来那条。

        换表会顺手清掉选中项，而清空选中项又会把编辑器清空 —— 别人的流量刚被拦下就
        把用户正在编辑的内容冲掉，所以整段用 `_restoring` 挡住，最后只在**真的换了
        一条流量**时才重载编辑器。行内按钮组趁换表重建：闭包攥着的是旧快照。
        """
        was_empty = self.flow_model.rowCount() == 0

        self._restoring = True
        try:
            self._clear_row_buttons()
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
        self._populate_row_buttons()

        flow = self._current_flow()
        flow_id = flow.id if flow is not None else ""
        if flow_id != self._current_id:
            self._current_id = flow_id
            self._load_editors(flow)
        self.queue_stack.setCurrentWidget(
            self.queue_splitter if flows else self.queue_empty_page
        )
        self._sync_title(len(flows))

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
        """放行（Ctrl+Enter / 右键菜单）。单选时连带写回当前改动再放行；多选时只
        放行 —— 改动属于哪一条不明确。"""
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
            icon=FluentIcon.SEND, text=self.tr("Release"), parent=menu
        )
        release_action.triggered.connect(self._on_release)
        menu.addAction(release_action)
        drop_action = BaseAction(
            icon=FluentIcon.CANCEL, text=self.tr("Drop"), parent=menu
        )
        drop_action.triggered.connect(self._on_drop)
        menu.addAction(drop_action)
        menu.exec(self.flow_table.viewport().mapToGlobal(pos))

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
            show_success(self.tr("Success"), message, self)
