"""断点窗口：一个独立的、非模态的顶层窗口，装拦截队列和请求/响应编辑器。

**为什么不是模态框。** 断点一旦命中就可能同时攥住一大把流量 —— 原生 `Intercept` 的
`request` / `response` 两个钩子共用同一个过滤器，所以一条规则命中一次交互会停两次
（实测 12 个并发请求 = 请求期 12 条 + 响应期 12 条 = 24 次停顿）。模态会把「断点总开关」
和「放行全部」这两个逃生口一起锁在遮罩外面，用户写宽了一条规则就再也救不回来；也挡住
「回捕获页抄个 token 再填进来」这类真实动作。所以是一个抢焦点、但不阻塞的独立窗口，
队列本身长在窗口里 —— 12 条并发是**一个窗口 + 一列队列**，不是 12 个叠起来的对话框。

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
    QSize,
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
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.dialogs import HeldFlowsChoice, HeldFlowsCloseDialog
from ferret.apps.intercept.editors import RequestEditor, ResponseEditor
from ferret.apps.intercept.models import HeldFlowTableModel
from ferret.core.mitm import HTTPFlow

_REQUEST_KEY = "request"
_RESPONSE_KEY = "response"


# 写成函数：模块级求值赶在翻译器安装之前（`core/application.py` 顶层就 import 了
# 主窗口，那时 `_init_i18n()` 还没跑）。
def _title() -> str:
    return QCoreApplication.translate("InterceptWindow", "Breakpoints")


class InterceptWindow(FluentWidget):
    """命中断点的流量停在这里，改完再放行、丢弃，或者直接伪造一条响应回去。

    编辑器按流量所处的阶段分工，而不是按当前看的是哪个标签：

    - 请求期（还没有响应）：可改请求；「响应」标签是**伪造响应**的草稿，只由
      「直接返回」提交（原生手法是给 flow 挂一条 response，请求根本不发出去）。
      伪造出来的这条响应照样要走 response 钩子，所以它自己还会被拦一次 —— 规则两边
      都拦，这是它的直接后果，不是漏放行（实测：客户端拿到 418 之前停了两次）。
    - 响应期：只能改响应；请求早发出去了，「请求」标签锁成只读。

    这两条判的都是 `flow.response is None`，和规则写了什么无关 —— 规则不选阶段。
    """

    #: 队列从空变成非空、窗口刚刚自己弹出来时发一次，带上队列条数。
    #: 托盘提醒交给 `MainWindow` 去发：边沿判定只在这里做一次，两处各判一次必然对不齐。
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
        self.resize(1080, 700)
        self.setMinimumSize(880, 520)
        self._sync_title(0)

        self.flow_model = HeldFlowTableModel(self)
        self.flow_table = TableView(self)
        self.flow_table.verticalHeader().hide()
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
        for i, w in enumerate([80, 70, 320, 120]):
            self.flow_table.setColumnWidth(i, w)
        # URL 是这张表里唯一值得占满的列，其余三列宽度固定。
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.flow_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.request_editor = RequestEditor(self)
        self.response_editor = ResponseEditor(self)
        self.editor_panel = TabPanel(self)
        self.editor_panel.setTabFontSize(14)
        self.editor_panel.addTab(_REQUEST_KEY, self.request_editor, self.tr("Request"))
        self.editor_panel.addTab(
            _RESPONSE_KEY, self.response_editor, self.tr("Response")
        )
        # TabPanel 自带的关闭按钮是给会话页用的，这里没有「关掉这个面板」的语义。
        self.editor_panel.close_button.hide()

        self.queue_splitter = BaseSplitter(Qt.Orientation.Horizontal, self)
        self.queue_splitter.addWidget(self.flow_table)
        self.queue_splitter.addWidget(self.editor_panel)
        self.queue_splitter.setStretchFactor(0, 2)
        self.queue_splitter.setStretchFactor(1, 3)

        self.queue_empty_page = self.__build_queue_empty_page()
        self.queue_stack = QStackedWidget(self)
        self.queue_stack.addWidget(self.queue_splitter)
        self.queue_stack.addWidget(self.queue_empty_page)

        self.action_bar = self.__build_action_bar()

    def __build_action_bar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.release_btn = PrimaryPushButton(FluentIcon.SEND, self.tr("Release"), bar)
        self.release_btn.setToolTip(
            self.tr("Write the current edits back and release (Ctrl+Enter)")
        )
        self.apply_btn = PushButton(FluentIcon.SAVE, self.tr("Apply edits"), bar)
        self.apply_btn.setToolTip(
            self.tr("Write the edits back and keep the flow held (Ctrl+S)")
        )
        self.fake_btn = PushButton(FluentIcon.RETURN, self.tr("Answer directly"), bar)
        self.fake_btn.setToolTip(
            self.tr(
                "Answer the client with whatever the Response tab holds instead "
                "of sending the request to the server (rules hold both phases, "
                "so the faked response stops once more on its way back)"
            )
        )
        self.drop_btn = PushButton(FluentIcon.CANCEL, self.tr("Drop"), bar)
        self.drop_btn.setToolTip(
            self.tr("Kill this flow; the client receives nothing at all")
        )

        self.revert_btn = TransparentToolButton(FluentIcon.HISTORY, bar)
        self.revert_btn.setFixedSize(32, 32)
        self.revert_btn.setIconSize(QSize(18, 18))
        self.revert_btn.setToolTip(self.tr("Revert edits"))

        self.release_all_btn = PushButton(
            FluentIcon.SEND_FILL, self.tr("Release all"), bar
        )
        self.release_all_btn.setToolTip(
            self.tr("Release every held flow without writing any edits back")
        )

        layout.addWidget(self.release_btn)
        layout.addWidget(self.apply_btn)
        layout.addWidget(self.fake_btn)
        layout.addWidget(self.drop_btn)
        layout.addWidget(self.revert_btn)
        layout.addStretch(1)
        layout.addWidget(self.release_all_btn)
        return bar

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

    def __init_layout(self):
        layout = QVBoxLayout(self)
        # 顶上让出标题栏的高度：`FluentWidgetTitleBar` 是浮在窗口上的兄弟控件、不进
        # 布局，不留边距内容就会被它压住（`FluentWindow` 是同样的做法，只是它把 48
        # 写死了 —— 这里的标题栏按按钮高度自适应，读它更准）。
        layout.setContentsMargins(0, self.titleBar.height(), 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.action_bar)
        layout.addWidget(self.queue_stack, 1)

    def __connect_signal_to_slot(self):
        self.release_btn.clicked.connect(self._on_release)
        self.apply_btn.clicked.connect(self._on_apply)
        self.fake_btn.clicked.connect(self._on_fake)
        self.drop_btn.clicked.connect(self._on_drop)
        self.revert_btn.clicked.connect(self._on_revert)
        self.release_all_btn.clicked.connect(self.controller.release_all)
        self.flow_table.selectionModel().selectionChanged.connect(
            self._on_flow_selection_changed
        )
        self.flow_table.customContextMenuRequested.connect(self._on_flow_context_menu)

        self.controller.flows_changed.connect(self._on_flows_changed)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._on_apply)
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._on_release)

    # —— 弹出 / 收起 ——

    def pop_up(self) -> None:
        """把窗口摆到用户眼前。断点页的「拦截队列」入口也走这里。"""
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
            return
        self.request_editor.load(flow)
        self.response_editor.load(flow)
        # 响应期的请求早发出去了，改它没有任何效果，锁成只读比让人白改一场好。
        response_phase = flow.response is not None
        self.request_editor.set_read_only(response_phase)
        self.response_editor.set_read_only(False)
        self.editor_panel.setCurrentTab(
            _RESPONSE_KEY if response_phase else _REQUEST_KEY
        )

    @Slot()
    def _update_flow_action_state(self):
        flows = self._selected_flows()
        flow = self._current_flow()
        request_phase = flow is not None and flow.response is None
        self.release_btn.setEnabled(bool(flows))
        self.drop_btn.setEnabled(bool(flows))
        self.apply_btn.setEnabled(flow is not None)
        self.revert_btn.setEnabled(flow is not None and flow.modified())
        # 已经有响应的流量没法再「直接返回」——原生要求 flow.response 为空。
        self.fake_btn.setEnabled(request_phase)
        self.release_all_btn.setEnabled(self.flow_model.rowCount() > 0)

    def _write_back(self, *, release: bool) -> bool:
        """把当前编辑器的内容写回选中的那条流量，按它所处的阶段选编辑器。"""
        flow = self._current_flow()
        if flow is None:
            return False
        if flow.response is None:
            return self.controller.apply_request(
                flow.id, self.request_editor.edit(), release=release
            )
        return self.controller.apply_response(
            flow.id, self.response_editor.edit(), release=release
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
        self._update_flow_action_state()

    @Slot(list)
    def _on_flows_changed(self, flows: list):
        """整表换一批新快照，并把选中项按 id 挪回原来那条。

        换表会顺手清掉选中项，而清空选中项又会把编辑器清空 —— 别人的流量刚被拦下就
        把用户正在编辑的内容冲掉，所以整段用 `_restoring` 挡住，最后只在**真的换了
        一条流量**时才重载编辑器。
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
        if flow_id != self._current_id:
            self._current_id = flow_id
            self._load_editors(flow)
        self.queue_stack.setCurrentWidget(
            self.queue_splitter if flows else self.queue_empty_page
        )
        self._update_flow_action_state()
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
        """放行。单选时连带写回当前改动；多选时只放行 —— 改动属于哪一条不明确。"""
        flows = self._selected_flows()
        if not flows:
            return
        if len(flows) == 1:
            self._write_back(release=True)
            return
        self.controller.release_flows([flow.id for flow in flows])

    @Slot()
    def _on_apply(self):
        self._write_back(release=False)

    @Slot()
    def _on_fake(self):
        """直接返回：拿「响应」标签的草稿伪造一条响应，请求不发往服务器。

        内核那边写回完顺手就放行了（`MitmFacade.fake_response` 的 `release=True`），
        但这条流量马上会带着伪造的响应再回到队列里 —— 见类 docstring。
        """
        flow = self._current_flow()
        if flow is None or flow.response is not None:
            return
        self.controller.fake_response(flow.id, self.response_editor.edit())

    @Slot()
    def _on_drop(self):
        flows = self._selected_flows()
        if flows:
            self.controller.drop_flows([flow.id for flow in flows])

    @Slot()
    def _on_revert(self):
        flow = self._current_flow()
        if flow is None:
            return
        # 撤销后内核里的报文变了，必须强制重载 —— 选中的还是同一条，
        # `_on_flows_changed` 那条「id 没变就不重载」的规则正好会把它跳过。
        if self.controller.revert_flow(flow.id):
            self._load_editors(self._current_flow())
            self._update_flow_action_state()

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
        if len(flows) == 1:
            revert_action = BaseAction(
                icon=FluentIcon.HISTORY, text=self.tr("Revert edits"), parent=menu
            )
            revert_action.setEnabled(flows[0].modified())
            revert_action.triggered.connect(self._on_revert)
            menu.addAction(revert_action)
        release_all_action = BaseAction(
            icon=FluentIcon.SEND_FILL, text=self.tr("Release all"), parent=menu
        )
        release_all_action.triggered.connect(self.controller.release_all)
        menu.addAction(release_all_action)
        menu.exec(self.flow_table.viewport().mapToGlobal(pos))

    # —— 关窗 ——

    def _confirm_close(self) -> HeldFlowsChoice:
        """问一句「还挂着的怎么办」。抽成方法是为了测试能替掉，不必真跑 `exec()`。"""
        dialog = HeldFlowsCloseDialog(self.flow_model.rowCount(), self)
        dialog.exec()
        # 读 `choice` 而不是 `exec()` 的返回值：三个出口里有两个都算「关得掉」。
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
        if choice == HeldFlowsChoice.RELEASE_ALL:
            # 放行会把队列清空 → `_on_flows_changed` 顺手 `hide()`，这里再 accept 一次
            # 也只是把已经隐藏的窗口再隐藏一次，没有副作用。
            self.controller.release_all()
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
