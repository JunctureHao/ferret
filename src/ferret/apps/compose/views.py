"""手工请求编辑页视图：顶栏（方法/URL/发送）+ 左右 Splitter（请求编辑 / 响应展示）。

顶栏独占一行；左侧是请求详情（参数/请求头/请求体，复用详情面板的可编辑组件），
右侧是响应区——`ResponsePane`（with_raw=False）承载 响应头/响应体/性能 三条标签，
「性能」页展示瀑布图和时间/流量两组详情。发送前显示空态，等待响应时显示进度
提示，避免把上次结果当成当前响应。
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from PySide6.QtCore import Qt, QThreadPool, QTimer, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    DotInfoBadge,
    FluentIcon,
    IconWidget,
    IndeterminateProgressRing,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SimpleCardWidget,
    SplitToolButton,
    SubtitleLabel,
    ToggleToolButton,
    ToolButton,
    ToolTipFilter,
)

from ferret.apps.common.edit import (
    ItemDualPanel,
)
from ferret.apps.common.edit.headers import HeaderDualPanel
from ferret.apps.common.flow.detail import ResponsePane
from ferret.apps.common.flow.fields import (
    Field,
    OverviewPane,
    Section,
    _decoded_size,
    _ms,
    _size_of,
    format_duration,
    format_time,
)
from ferret.apps.common.flow.timing import WaterfallBar, phases
from ferret.apps.common.http_methods import METHODS
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.panel import TabPanel
from ferret.apps.common.splitter import BaseSplitter, OrientationSplitter
from ferret.apps.common.tasks import FunctionTask
from ferret.apps.compose.body_editor import ComposeBodyEditor
from ferret.apps.compose.controllers import ComposeController
from ferret.apps.compose.curl_import import parse_curl
from ferret.apps.compose.library import ComposeLibraryPanel
from ferret.apps.compose.models import SavedBody, SavedRequest
from ferret.apps.compose.store import ComposeRequestStore
from ferret.core.mitm import ComposeResult, RequestEdit
from ferret.core.settings import CONFIG
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 「性能」页的两组卡片：键全部来自 `build_flow_detail` 的既有字段，渲染直接复用
# 概览页的声明式规格 + FieldCard（时间/流量两组与概览的 Timing/Size 同源，只是
# 挂在 compose 的详情字典上）。fields 里那几个下划线格式化器是同族模块的既有
# 积木，直接借力，不再抄一份。
_PERF_SECTIONS: tuple[Section, ...] = (
    Section(
        title=QT_TRANSLATE_NOOP("ComposePerf", "时间"),
        fields=(
            Field("Flow ID", "Flow ID", mono=True),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "流量创建"),
                "Flow Created",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "客户端 TLS 握手"),
                "Front TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求开始"),
                "req_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求结束"),
                "req_timestamp_end",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "请求时长"),
                "req_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "TCP 握手"),
                "Back TCP Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "服务端 TLS 握手"),
                "Back TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应开始"),
                "res_timestamp_start",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应结束"),
                "res_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "响应时长"),
                "res_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "总耗时"),
                "duration_ms",
                fmt=format_duration,
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("ComposePerf", "流量"),
        fields=(
            Field(QT_TRANSLATE_NOOP("ComposePerf", "请求"), _size_of("req_total_size")),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求头"),
                _size_of("req_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求体（线上）"),
                _size_of("req_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 请求体（解压）"),
                _decoded_size("req_wire_size", "req_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "响应"), _size_of("res_total_size")),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应头"),
                _size_of("res_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应体（线上）"),
                _size_of("res_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("ComposePerf", "- 响应体（解压）"),
                _decoded_size("res_wire_size", "res_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("ComposePerf", "总计"), _size_of("total_size")),
        ),
    ),
)


class SaveRequestDialog(MessageBoxBase):
    """保存到请求库：名称 + 文件夹 + 只读摘要（SavedRequest.summary 同款）。"""

    def __init__(
        self,
        default_name: str,
        folders: list[str],
        parent: QWidget,
        *,
        folder: str = "",
        summary: str = "",
    ):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(self.tr("保存到请求库"), self))
        self.name_edit = LineEdit(self)
        self.name_edit.setText(default_name)
        self.folder_combo = ComboBox(self)
        self.folder_combo.addItem(self.tr("未分组"))
        self.folder_combo.addItems(folders)
        if folder:
            index = self.folder_combo.findText(folder)
            if index >= 0:
                self.folder_combo.setCurrentIndex(index)
        form_layout = QVBoxLayout()
        form_layout.setSpacing(4)
        form_layout.addWidget(BodyLabel(self.tr("名称"), self))
        form_layout.addWidget(self.name_edit)
        form_layout.addWidget(BodyLabel(self.tr("文件夹"), self))
        form_layout.addWidget(self.folder_combo)
        self.viewLayout.addLayout(form_layout)
        if summary:
            self.viewLayout.addWidget(CaptionLabel(summary, self))
        self.widget.setMinimumWidth(420)
        self.yesButton.setText(self.tr("保存"))
        self.cancelButton.setText(self.tr("取消"))
        self.name_edit.textChanged.connect(
            lambda text: self.yesButton.setEnabled(bool(text.strip()))
        )
        self.yesButton.setEnabled(bool(default_name.strip()))

    def name(self) -> str:
        return self.name_edit.text().strip()

    def folder(self) -> str:
        # 第 0 项是「未分组」占位，落盘为空串。
        index = self.folder_combo.currentIndex()
        return "" if index <= 0 else self.folder_combo.currentText()


class _UnsavedDialog(MessageBoxBase):
    """三选一确认：保存并继续 / 放弃修改 / 取消（MessageBox 只有两键）。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.choice = "cancel"
        self.viewLayout.addWidget(SubtitleLabel(self.tr("未保存的修改"), self))
        self.viewLayout.addWidget(BodyLabel(self.tr("当前请求有未保存的修改。"), self))
        self.yesButton.setText(self.tr("保存并继续"))
        self.cancelButton.setText(self.tr("取消"))
        self.discard_button = PushButton(self.tr("放弃修改"), self)
        self.discard_button.clicked.connect(self._on_discard)
        self.buttonLayout.insertWidget(1, self.discard_button)
        self.widget.setMinimumWidth(380)
        self.yesButton.clicked.connect(self._on_save_choice)

    def _on_save_choice(self) -> None:
        self.choice = "save"

    def _on_discard(self) -> None:
        self.choice = "discard"
        self.accept()


class ComposeInterface(QWidget):
    """编辑页：自己拼请求发出去，可选不进流量列表。"""

    def __init__(
        self,
        controller: ComposeController,
        parent=None,
        *,
        store: ComposeRequestStore | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("ComposeInterface")
        self.controller = controller
        self.store = store if store is not None else ComposeRequestStore(self)
        self._saved_id: str | None = None
        self._form_dirty = False
        self._sending = False
        self._prepare_task: FunctionTask | None = None
        self._prepare_tasks: set[FunctionTask] = set()
        self._pending_request: tuple[str, str, list[tuple[str, str]], bool] | None = (
            None
        )

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._update_save_state()

        if self.store.warning:
            show_error(self.tr("请求库"), self.store.warning, self)

    # ── 组件 ──────────────────────────────────

    def __init_widget(self):
        # 顶栏。方法下拉不可编辑：prefill 可能带来词表外的方法（PROPFIND 等），
        # 写入统一走 _set_method（缺项动态补条目）。
        self.method_combo = ComboBox(self)
        self.method_combo.addItems(METHODS)
        self.method_combo.setCurrentText("GET")
        self.method_combo.setFixedWidth(110)

        # 合并参数会重编码 query；程序化装载不是用户编辑。正文自己的保真状态
        # 由 ComposeBodyEditor 管理，文件读取不在 Qt 主线程执行。
        self._url_dirty = False
        self._params_dirty = False
        self._loading_form = False

        self.url_edit = LineEdit(self)
        self.url_edit.setPlaceholderText("https://example.com/api")
        self.url_edit.setClearButtonEnabled(True)

        # 次要操作使用定宽图标，记录状态仍由按钮的选中态直接表达。
        self.record_btn = ToggleToolButton(FluentIcon.HISTORY, self)
        self.record_btn.setFixedSize(32, 32)
        self.record_btn.setAccessibleName(self.tr("记录流量"))
        self.record_btn.setChecked(True)
        self.record_btn.toggled.connect(self._update_record_hint)
        self._update_record_hint()
        self.record_btn.installEventFilter(ToolTipFilter(self.record_btn, 700))

        # 按当前语言的最长状态文案预留宽度，发送中不挤动 URL，也不截断英文。
        self.send_btn = PrimaryPushButton(FluentIcon.SEND, self.tr("发送中…"), self)
        sending_width = self.send_btn.sizeHint().width()
        self.send_btn.setText(self.tr("发送"))
        self.send_btn.setFixedWidth(
            max(96, sending_width, self.send_btn.sizeHint().width())
        )
        self.send_btn.setToolTip(self.tr("发送这条请求"))
        self.send_btn.installEventFilter(ToolTipFilter(self.send_btn, 700))

        # cURL 粘贴导入：读剪贴板灌表单，解析错误就地 show_error（v1 不做
        # 粘贴编辑对话框）。
        self.paste_curl_btn = ToolButton(FluentIcon.PASTE, self)
        self.paste_curl_btn.setFixedSize(32, 32)
        self.paste_curl_btn.setToolTip(self.tr("从剪贴板导入 cURL 命令"))
        self.paste_curl_btn.setAccessibleName(self.tr("导入 cURL"))
        self.paste_curl_btn.installEventFilter(ToolTipFilter(self.paste_curl_btn, 700))

        # 顶栏最左：请求库显隐开关（持久化到 CONFIG.compose_library_visible）。
        self.library_btn = ToolButton(FluentIcon.LIBRARY, self)
        self.library_btn.setCheckable(True)
        self.library_btn.setToolTip(self.tr("请求库"))
        self.library_btn.installEventFilter(ToolTipFilter(self.library_btn, 700))

        # 保存：主钮直接保存（有归属则原地更新），下拉里是「另存为…」。
        self.save_btn = SplitToolButton(FluentIcon.SAVE, self)
        self.save_btn.button.setFixedSize(32, 32)
        self.save_btn.dropButton.setFixedSize(20, 32)
        self.save_btn.dropButton.setToolTip(self.tr("另存为…"))
        self.save_btn.dropButton.setAccessibleName(self.tr("另存为…"))
        for button in (self.save_btn.button, self.save_btn.dropButton):
            button.installEventFilter(ToolTipFilter(button, 700))
        # 徽标放在定宽主按钮内部，避免被父控件裁掉，也不随脏状态挤动布局。
        self.save_dirty_badge = DotInfoBadge(self.save_btn.button)
        self.save_dirty_badge.setFixedSize(6, 6)
        self.save_dirty_badge.move(23, 3)
        self.save_dirty_badge.hide()
        self.save_menu = RoundMenu(parent=self.save_btn)
        save_as = BaseAction(FluentIcon.SAVE_AS, self.tr("另存为…"), self.save_menu)
        save_as.triggered.connect(self._on_save_as)
        self.save_menu.addAction(save_as)
        self.save_btn.setFlyout(self.save_menu)

        # 左侧请求库面板；默认隐藏，状态由 CONFIG 恢复。
        self.library_panel = ComposeLibraryPanel(self.store, self)

        # 左侧：请求详情（参数/请求头/请求体，复用详情面板的可编辑组件）。
        self.request_panel = TabPanel(self)
        self.request_panel.setTabFontSize(12)
        self.request_panel.close_button.hide()

        self.params_card = ItemDualPanel(True, self)
        self.headers_card = HeaderDualPanel(True, self)
        self.body_editor = ComposeBodyEditor(self.headers_card, self)

        self.request_panel.addTab("Params", self.params_card, self.tr("参数"))
        self.request_panel.addTab("Headers", self.headers_card, self.tr("请求头"))
        self.request_panel.addTab("Body", self.body_editor, self.tr("请求体"))
        for button in (self.record_btn, self.paste_curl_btn, self.save_btn):
            self.request_panel.tab_layout.addWidget(button)
        # 请求头(N)：条数挂标签，随编辑实时变（参数变化实时写回 URL，见下）。
        self.headers_card.changed.connect(self._update_header_count)
        self._params_display_values = tuple(self.params_card.items())
        self.params_card.changed.connect(self._on_params_edited)
        self._update_header_count()

        # 右侧：响应区。`with_raw=False`：compose 的结果不需要原始报文兜底，
        # 标签只剩 响应头/响应体，「性能」由本页追加到末位。
        self.response_pane = ResponsePane(self, with_raw=False)

        self.perf_overview = OverviewPane(sections=_PERF_SECTIONS)
        # 瀑布条压在「时间」组卡片头部（滚动区之上）：compose 回放
        # 的连接段嵌在等待里（lane="nested"，虚线框），与详情页时序同一模型。
        self.perf_waterfall = WaterfallBar(self)
        perf_page = QWidget(self)
        perf_layout = QVBoxLayout(perf_page)
        perf_layout.setContentsMargins(0, 0, 0, 0)
        perf_layout.setSpacing(8)
        perf_layout.addWidget(self.perf_waterfall)
        perf_layout.addWidget(self.perf_overview, 1)

        self.response_pane.addTab("Perf", perf_page, self.tr("性能"))

        # 与 FlowEmptyState 同一层级：小图标、标题、说明。等待时只切换图标
        # 和文案，QStackedWidget 管住子控件，避免悬空控件浮到左上角。
        self.empty_hint = SimpleCardWidget(self)
        self.empty_icon_stack = QStackedWidget(self.empty_hint)
        self.empty_icon_stack.setFixedSize(32, 32)
        self.empty_icon = IconWidget(FluentIcon.SEND, self.empty_icon_stack)
        self.loading_ring = IndeterminateProgressRing(
            self.empty_icon_stack, start=False
        )
        self.loading_ring.setFixedSize(32, 32)
        self.loading_ring.setStrokeWidth(3)
        self.empty_icon_stack.addWidget(self.empty_icon)
        self.empty_icon_stack.addWidget(self.loading_ring)
        self.empty_title = BodyLabel(self.empty_hint)
        self.empty_subtitle = CaptionLabel(self.empty_hint)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setWordWrap(True)
        hint_layout = QVBoxLayout(self.empty_hint)
        hint_layout.setContentsMargins(16, 16, 16, 16)
        hint_layout.addStretch(1)
        hint_layout.addWidget(self.empty_icon_stack, 0, Qt.AlignmentFlag.AlignCenter)
        hint_layout.addSpacing(8)
        hint_layout.addWidget(self.empty_title)
        hint_layout.addWidget(self.empty_subtitle)
        hint_layout.addStretch(1)
        self._set_waiting(False)

        # 右侧：空态 ↔ 响应内容 整页切换。
        self.response_stack = QStackedWidget(self)
        self.response_stack.addWidget(self.response_pane)
        self.response_stack.addWidget(self.empty_hint)
        self.response_stack.setCurrentWidget(self.empty_hint)

    def __init_layout(self):
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.library_btn)
        top.addWidget(self.method_combo)
        top.addWidget(self.url_edit, stretch=1)
        top.addWidget(self.send_btn)

        # 跟随全局布局：设置里水平 → 左右排，垂直 → 上下排（本页不反转）。
        self.splitter = OrientationSplitter(parent=self)
        self.splitter.addWidget(self.request_panel)
        self.splitter.addWidget(self.response_stack)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        # 初始 50/50 不能指望构造期：QSplitter 初始分位只认两侧 sizeHint 的
        # 比例（本页 384:510），Ignored 策略 / stretch / setSizes([1, 1]) 实测
        # 都掰不动它；要拿真实宽度换算（set_equal_sizes）才生效，所以挂到
        # 首次 showEvent 的下一拍（与 flow 页 _apply_equal_sizes 同一模式）。
        self._splitter_normalized = False

        # 请求库面板挂在页面级水平 Splitter 左列，原内容（顶栏+编辑/响应
        # Splitter）整包移到右列容器，内部布局逻辑不动。
        self._content = QWidget(self)
        content_layout = QVBoxLayout(self._content)
        content_layout.setContentsMargins(12, 8, 12, 12)
        content_layout.setSpacing(8)
        content_layout.addLayout(top)
        content_layout.addWidget(self.splitter, stretch=1)

        self.library_splitter = BaseSplitter(Qt.Orientation.Horizontal, self)
        self.library_panel.setMinimumWidth(180)
        self.library_panel.setMaximumWidth(320)
        self.library_splitter.addWidget(self.library_panel)
        self.library_splitter.addWidget(self._content)
        self.library_splitter.setStretchFactor(0, 0)
        self.library_splitter.setStretchFactor(1, 1)
        self.library_splitter.setChildrenCollapsible(False)
        raw_width = CONFIG.get(CONFIG.compose_library_width)
        self._library_width = (
            max(180, min(320, raw_width))
            if isinstance(raw_width, int) and not isinstance(raw_width, bool)
            else 240
        )

        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self.library_splitter)

        library_visible = bool(CONFIG.get(CONFIG.compose_library_visible))
        self.library_btn.setChecked(library_visible)
        self.library_panel.setVisible(library_visible)
        self._restore_library_width()

    def __connect_signal_to_slot(self):
        self.send_btn.clicked.connect(self._on_send)
        self.paste_curl_btn.clicked.connect(self._on_paste_curl)
        self.save_btn.clicked.connect(self._on_save)
        self.library_btn.toggled.connect(self._on_library_toggled)
        self.library_panel.open_requested.connect(self._on_open_saved)
        self.library_panel.create_requested.connect(self._on_create_draft)
        self.store.changed.connect(self._on_store_changed)
        self.store.save_failed.connect(self._on_store_save_failed)
        self.library_splitter.splitterMoved.connect(self._on_library_resized)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.save_shortcut.activated.connect(self._on_save)
        self.url_edit.returnPressed.connect(self._on_send)
        self.url_edit.textChanged.connect(self._on_url_edited)
        self.controller.sending_changed.connect(self._on_sending_changed)
        self.controller.result_ready.connect(self._on_result)
        self.controller.send_failed.connect(self._on_send_failed)
        # 表单脏标记：任一编辑（非程序化装载）都标记为未保存。
        self.method_combo.currentIndexChanged.connect(self._mark_dirty)
        self.record_btn.toggled.connect(self._mark_dirty)
        self._header_display_values = (
            tuple(self.headers_card.items()),
            self.headers_card.saved_managed_rows(),
        )
        self.headers_card.changed.connect(self._on_headers_changed)
        self._body_display_text = self.body_editor.text.text()
        self.body_editor.text.changed.connect(self._on_body_text_changed)
        self.body_editor.form.changed.connect(self._mark_dirty)
        self.body_editor.multipart.changed.connect(self._mark_dirty)
        self.body_editor.file_picker.changed.connect(self._mark_dirty)
        self.body_editor.kind_combo.currentIndexChanged.connect(self._mark_dirty)

    # ── 行为 ──────────────────────────────────

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 只在首次可见时平分一次：singleShot 等布局把这页的真实宽度定下来，
        # 之后用户手动拖过分隔条，再切页回来不再打扰。
        if not self._splitter_normalized:
            self._splitter_normalized = True
            QTimer.singleShot(0, self.splitter.set_equal_sizes)
            QTimer.singleShot(0, self._restore_library_width)

    def _restore_library_width(self) -> None:
        if self.library_btn.isChecked():
            self.library_splitter.setSizes(
                [self._library_width, max(1, self.width() - self._library_width - 12)]
            )

    @Slot(bool)
    def _on_library_toggled(self, visible: bool) -> None:
        self.library_panel.setVisible(visible)
        self._restore_library_width()
        CONFIG.set(CONFIG.compose_library_visible, visible, save=False)
        CONFIG.defer_save()

    @Slot(int, int)
    def _on_library_resized(self, position: int, index: int) -> None:
        if self.library_btn.isChecked():
            self._library_width = max(180, min(320, self.library_splitter.sizes()[0]))
            CONFIG.set(CONFIG.compose_library_width, self._library_width, save=False)
            CONFIG.defer_save()

    @Slot()
    def _on_headers_changed(self) -> None:
        values = (
            tuple(self.headers_card.items()),
            self.headers_card.saved_managed_rows(),
        )
        if values != self._header_display_values:
            self._header_display_values = values
            self._mark_dirty()

    def _reset_display_baselines(self) -> None:
        self._body_display_text = self.body_editor.text.text()
        self._params_display_values = tuple(self.params_card.items())
        self._header_display_values = (
            tuple(self.headers_card.items()),
            self.headers_card.saved_managed_rows(),
        )

    @Slot()
    def _on_body_text_changed(self) -> None:
        # Qt rehighlighting emits textChanged without an edit (theme/show events).
        text = self.body_editor.text.text()
        if text != self._body_display_text:
            self._body_display_text = text
            self._mark_dirty()

    @Slot()
    def _mark_dirty(self) -> None:
        if not self._loading_form:
            self._form_dirty = True
            self._update_save_state()

    @Slot()
    def _update_record_hint(self) -> None:
        self.record_btn.setToolTip(
            self.tr("记录流量：已开启，点击关闭")
            if self.record_btn.isChecked()
            else self.tr("记录流量：已关闭，点击开启")
        )

    def _update_save_state(self) -> None:
        self.save_dirty_badge.setVisible(self._form_dirty)
        current = self.store.get(self._saved_id) if self._saved_id else None
        hint = (
            self.tr("保存「{}」 (Ctrl+S)").format(current.name)
            if current is not None
            else self.tr("保存到请求库 (Ctrl+S)")
        )
        self.save_btn.setToolTip(hint)
        self.save_btn.button.setToolTip(hint)
        self.save_btn.button.setAccessibleName(
            self.tr("保存（有未保存的修改）") if self._form_dirty else self.tr("保存")
        )
        self.library_panel.set_current_request(self._saved_id, dirty=self._form_dirty)

    @Slot()
    def _on_store_changed(self) -> None:
        # 重命名/移动只更新归属；不得用库内旧快照覆盖正在编辑的表单。
        if self._saved_id is not None and self.store.get(self._saved_id) is None:
            self._saved_id = None
            self._form_dirty = True
        self._update_save_state()

    @Slot(str)
    def _on_store_save_failed(self, message: str) -> None:
        show_error(self.tr("保存失败"), message, self)

    def _snapshot_request(self, name: str, folder: str) -> SavedRequest:
        # items() 提交仍在编辑的表格单元格；参数提交可能同步更新 URL，
        # 所以 URL 必须在这些提交完成后读取。
        params = tuple(self.params_card.items())
        body = self.body_editor.saved_body()
        headers = tuple(self.headers_card.items())
        try:
            url = self._collect_url()
        except ValueError:
            # A saved draft may contain an unfinished URL; validate on send.
            url = self.url_edit.text()
        return SavedRequest.create(
            name=name,
            folder=folder,
            method=self.method_combo.currentText(),
            # URL 栏保存整串；参数是可编辑投影，装载时不重编码原始 query。
            url=url,
            params=params,
            headers=headers,
            managed_headers=self.headers_card.saved_managed_rows(),
            body=body,
            record=self.record_btn.isChecked(),
        )

    @Slot()
    def _on_save(self) -> bool:
        return self._save_request()

    @Slot()
    def _on_save_as(self) -> bool:
        return self._save_request(save_as=True)

    def _save_request(self, *, save_as: bool = False) -> bool:
        if self._sending:
            return False
        current = self.store.get(self._saved_id) if self._saved_id else None
        name = current.name if current is not None else self.tr("新请求")
        folder = current.folder if current is not None else ""
        if current is None or save_as:
            summary = SavedRequest(
                method=self.method_combo.currentText(), url=self.url_edit.text()
            ).summary()
            dialog = SaveRequestDialog(
                name,
                self.store.folders(),
                self.window(),
                folder=folder,
                summary=summary,
            )
            try:
                if not dialog.exec() or not dialog.name():
                    return False
                name, folder = dialog.name(), dialog.folder()
            finally:
                dialog.deleteLater()
        saved = self._snapshot_request(name, folder)
        if current is not None and not save_as:
            saved = saved.touched(id=current.id)
        # 失败后仍保留同一 id，重试保存不会在库中产生第二条记录。
        self._saved_id = saved.id
        self._form_dirty = True
        self.store.upsert(saved)
        if not self.store.flush():
            self._update_save_state()
            return False
        self._form_dirty = False
        self._update_save_state()
        return True

    def _can_replace_form(self) -> bool:
        if self._sending:
            return False
        # 工具栏与外部 prefill 不保证抢走单元格焦点；先提交编辑器再判断脏状态。
        self.params_card.items()
        self.headers_card.items()
        self.body_editor.form.items()
        self.body_editor.multipart.fields()
        if not self._form_dirty:
            return True
        dialog = _UnsavedDialog(self.window())
        try:
            dialog.exec()
            choice = dialog.choice
        finally:
            dialog.deleteLater()
        if choice == "save":
            return self._save_request()
        return choice == "discard"

    @Slot(str)
    def _on_open_saved(self, request_id: str) -> None:
        if request_id == self._saved_id and not self._form_dirty:
            self._update_save_state()
            return
        request = self.store.get(request_id)
        if request is None or not self._can_replace_form():
            self._update_save_state()
            return
        # 确认中可能保存当前请求；重新打开同一条时要读取保存后的版本。
        request = self.store.get(request_id)
        if request is None:
            self._update_save_state()
            return
        self._load_saved(request)

    def _load_saved(self, request: SavedRequest) -> None:
        self._loading_form = True
        try:
            self._set_method(request.method)
            self.url_edit.setText(request.url)
            self.params_card.set_items(self._query_pairs(request.url))
            self.headers_card.set_items(list(request.headers))
            self.headers_card.restore_managed_rows(request.managed_headers)
            self.body_editor.load_saved(request.body)
            self.record_btn.setChecked(request.record)
            self._url_dirty = False
            self._params_dirty = False
            self._form_dirty = False
            self._saved_id = request.id or None
        finally:
            self._loading_form = False
        self._reset_display_baselines()
        self._update_header_count()
        self._update_save_state()
        self.response_stack.setCurrentWidget(self.empty_hint)
        self._set_waiting(False)

    @staticmethod
    def _query_pairs(url: str) -> list[tuple[str, str]]:
        try:
            return parse_qsl(urlsplit(url).query, keep_blank_values=True)
        except ValueError:
            # 草稿允许尚未写完的 URL；校验留给发送入口。
            return []

    @Slot()
    def _on_create_draft(self) -> None:
        if self._can_replace_form():
            self._load_saved(SavedRequest(body=SavedBody()))

    @Slot()
    def _update_header_count(self) -> None:
        """`请求头(N)`：条数挂标签，随编辑实时变。"""
        count = len([k for k, _ in self.headers_card.items() if k.strip()])
        self.request_panel.setTabText(
            "Headers", self.tr("请求头 ({count})").format(count=count)
        )

    @Slot()
    def _on_url_edited(self) -> None:
        if not self._loading_form:
            self._url_dirty = True
            self._mark_dirty()

    @Slot()
    def _on_params_edited(self) -> None:
        values = tuple(self.params_card.items())
        if values == self._params_display_values:
            return
        self._params_display_values = values
        if not self._loading_form:
            self._params_dirty = True
            self._sync_params_to_url()
            self._mark_dirty()

    @Slot()
    def _sync_params_to_url(self) -> None:
        """参数变化实时写回 URL 栏：参数页是 query 的权威源。

        只在有键值时合并（幂等，urlencode 的结果再合并还是它自己）；参数清空时
        不动 URL —— 「删光参数」和「还没填」在空列表里是同一个信号，前者想让
        URL 上的查询串消失应该在 URL 栏里删。
        """
        if self._loading_form:
            return
        pairs = [(k, v) for k, v in self.params_card.items() if k.strip()]
        if pairs:
            self.url_edit.setText(
                self._merge_query(self.url_edit.text().strip(), pairs)
            )

    def _merge_query(self, url: str, pairs: list[tuple[str, str]]) -> str:
        """参数合并进 URL 查询串（与断点面板 `_merge_query` 同一套端口规范：
        `http://…:443` / `https://…:80` 这类跨协议笔误还原为默认端口，
        本协议显式端口（含 443 本尊、8080）不动。"""
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if (parts.scheme == "http" and port == 443) or (
            parts.scheme == "https" and port == 80
        ):
            # 从 netloc 尾部剥掉端口段，而不是拿 hostname 重建 netloc ——
            # hostname 不含 userinfo，重建会把 URL 内嵌的 user:pass@ 一并丢掉。
            host, sep, port_part = parts.netloc.rpartition(":")
            if sep and port_part.isdigit() and int(port_part) == port:
                parts = parts._replace(netloc=host)
        pairs = [(k, v) for k, v in pairs if k.strip()]
        query = urlencode(pairs) if pairs else parts.query
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
        )

    def _collect_headers(self) -> list[tuple[str, str]]:
        return [(k, v) for k, v in self.headers_card.items() if k.strip()]

    def _collect_url(self) -> str:
        """发送时的 URL = URL 栏 + 参数页合并。参数页是权威 query：编辑页语义上
        「参数」就是 URL 的查询串，两边各存一份只会互相打脸。

        未编辑保真（issues #78）：URL 栏与参数页都没动过时整串原样返回，不走
        parse_qsl/urlencode 的重编码路径 —— `?q=a%20b&flag` 重排成 `q=a+b&flag=`
        会让签名类参数失效。编辑过才合并（端口规范见 `_merge_query`）。
        """
        pairs = self.params_card.items()
        if not self._url_dirty and not self._params_dirty:
            return self.url_edit.text()
        return self._merge_query(self.url_edit.text().strip(), pairs)

    def _set_method(self, method: str) -> None:
        """写入方法下拉。纯 ComboBox 有两个坑：setText 只改按钮字面、不动选中项，
        发送时读的 currentText() 会留在旧方法上（显示 PROPFIND、按 GET 发出）；
        setCurrentText 又只认词表内项，落空静默不动。所以缺项先补进条目列表
        再选中，补过的条目留在下拉里。
        """
        if self.method_combo.findText(method) < 0:
            self.method_combo.addItem(method)
        self.method_combo.setCurrentText(method)

    def prefill(self, edit: RequestEdit) -> bool:
        """把一份请求草稿灌进表单（右键「Edit in Compose」/ cURL 导入）。

        替换前确认未保存修改；发送与文件准备期间保留当前请求和响应的对应关系。
        """
        if not self._can_replace_form():
            return False
        # 程序化装载：setText 的 textChanged 不算用户编辑，脏标记全部归零。
        self._loading_form = True
        try:
            self._set_method(edit.method)
            self.url_edit.setText(edit.url)
            # query 拆进参数页（断点面板 `RequestPanel.load` 同款）：URL 栏留整串，
            # 参数页是权威源，发送时再合并回去。
            self.params_card.set_items(self._query_pairs(edit.url))
            self.headers_card.set_items(list(edit.headers))
            self._update_header_count()
            self.body_editor.load(edit.content, list(edit.headers))
            self._url_dirty = False
            self._params_dirty = False
            self._saved_id = None
            self._form_dirty = False
        finally:
            self._loading_form = False
        self._reset_display_baselines()
        self.response_stack.setCurrentWidget(self.empty_hint)
        if not self._sending:
            self._set_waiting(False)
        self._update_save_state()
        return True

    @Slot()
    def _on_paste_curl(self) -> None:
        if self._sending:
            return
        text = QApplication.clipboard().text().strip()
        # 空剪贴板 / 非 curl 开头在这里挡；语法细节错误由解析器报。
        if not text:
            show_error(self.tr("导入失败"), self.tr("剪贴板是空的"), self)
            return
        if not text.lower().startswith(("curl ", "curl.exe")):
            show_error(
                self.tr("导入失败"),
                self.tr("剪贴板里没有 cURL 命令"),
                self,
            )
            return
        try:
            edit = parse_curl(text)
        except ValueError as exc:
            show_error(self.tr("导入失败"), str(exc), self)
            return
        if self.prefill(edit):
            show_success(self.tr("成功"), self.tr("已从 cURL 导入请求"), self)

    @Slot()
    def _on_send(self):
        if self._sending:
            return
        method = self.method_combo.currentText().strip()
        try:
            url = self._collect_url()
        except ValueError as exc:
            self._on_send_failed(self.tr("发送失败"), str(exc))
            return
        if not url:
            self._on_send_failed(self.tr("发送失败"), self.tr("URL 为空"))
            return

        try:
            # Multipart can update a managed boundary: collect headers afterwards.
            draft = self.body_editor.draft()
            headers = self._collect_headers()
        except ValueError as exc:
            self._on_send_failed(self.tr("发送失败"), str(exc))
            return
        record = self.record_btn.isChecked()
        if not draft.needs_worker:
            self.controller.send(method, url, headers, draft.content, record=record)
            return
        self._pending_request = (method, url, headers, record)
        task = FunctionTask(draft.build)
        self._prepare_task = task
        self._prepare_tasks.add(task)
        task.signals.succeeded.connect(self._on_body_prepared)
        task.signals.failed.connect(self._on_body_prepare_failed)
        task.signals.finished.connect(self._on_body_prepare_finished)
        self._on_sending_changed(True)
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_body_prepared(self, content: bytes) -> None:
        request = self._pending_request
        self._pending_request = None
        if request is None:
            return
        method, url, headers, record = request
        self.controller.send(method, url, headers, content, record=record)

    @Slot(str)
    def _on_body_prepare_failed(self, message: str) -> None:
        self._pending_request = None
        self._on_send_failed(self.tr("发送失败"), message)

    @Slot()
    def _on_body_prepare_finished(self) -> None:
        # A synchronous send failure can enable a new prepare before the old
        # worker's queued finished arrives. Release only that worker's owner.
        sender = self.sender()
        task = next(
            (task for task in self._prepare_tasks if task.signals is sender), None
        )
        if task is not None:
            self._prepare_tasks.discard(task)
            task.signals.finished.disconnect(self._on_body_prepare_finished)
            if self._prepare_task is task:
                self._prepare_task = None

    @Slot(bool)
    def _on_sending_changed(self, sending: bool):
        self._sending = sending
        for widget in (
            self.library_panel,
            self.paste_curl_btn,
            self.save_btn,
            self.request_panel,
            self.method_combo,
            self.url_edit,
            self.record_btn,
        ):
            widget.setDisabled(sending)
        self.save_shortcut.setEnabled(not sending)
        self.send_btn.setDisabled(sending)
        self.send_btn.setText(self.tr("发送中…") if sending else self.tr("发送"))
        self.send_btn.setIcon(FluentIcon.SYNC if sending else FluentIcon.SEND)
        self._set_waiting(sending)
        if sending:
            self.response_stack.setCurrentWidget(self.empty_hint)

    def _set_waiting(self, waiting: bool) -> None:
        if waiting:
            self.empty_icon_stack.setCurrentWidget(self.loading_ring)
            self.loading_ring.start()
            self.empty_title.setText(self.tr("正在等待响应"))
            self.empty_subtitle.setText(self.tr("收到响应后会显示在这里"))
        else:
            self.loading_ring.stop()
            self.empty_icon_stack.setCurrentWidget(self.empty_icon)
            self.empty_title.setText(self.tr("暂无响应"))
            self.empty_subtitle.setText(self.tr("编辑请求，然后点击「发送」"))

    @Slot(object)
    def _on_result(self, result: ComposeResult):
        self._on_sending_changed(False)
        detail = result.detail
        if result.error:
            show_error(self.tr("请求失败"), result.error, self)
        self.response_stack.setCurrentWidget(self.response_pane)
        self.response_pane.set_data(detail)
        self.perf_overview.set_data(detail)
        self.perf_waterfall.set_model(phases(detail))

    @Slot(str, str)
    def _on_send_failed(self, title: str, content: str):
        self._on_sending_changed(False)
        self.response_stack.setCurrentWidget(self.empty_hint)
        show_error(title, content, self)
