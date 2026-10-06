"""Mock 响应池页：池表 + 匹配行为旋钮卡（docs/design.md#mock）。

布局 = 规则页骨架（命令栏 + 表格 + 空态页）加一个底部「匹配设置」折叠区。
折叠区里是 WinUI 设置卡（qfluentwidgets SettingCard 家族，与设置页同一套词汇）：
旋钮卡直接绑 CONFIG 项自持久化，控制器挂 valueChanged 负责下发与失败回滚。
默认收起、摘要常显 —— 旋钮决定 mock 的行为，但不能让它们把池表挤扁。
"""

from typing import Any

from PySide6.QtCore import QSize, Qt, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
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
    IndicatorPosition,
    OptionsSettingCard,
    PushButton,
    PushSettingCard,
    RoundMenu,
    ScrollArea,
    SwitchButton,
    SwitchSettingCard,
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.mock.controllers import EXTRA_VALUES, MockController
from ferret.apps.mock.dialogs import StringListDialog
from ferret.apps.mock.models import MockPoolFilterProxyModel, MockPoolTableModel
from ferret.core.settings import CONFIG
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 未命中策略的展示文案，顺序必须与 EXTRA_VALUES / CONFIG.mock_extra.options 一致。
# 逐条写字面量：lupdate 是静态扫描，循环变量里的标签提不出来（AGENTS §7）。
EXTRA_LABELS: dict[str, str] = {
    "forward": QT_TRANSLATE_NOOP("MockView", "直连放行"),
    "kill": QT_TRANSLATE_NOOP("MockView", "断开连接"),
    "204": QT_TRANSLATE_NOOP("MockView", "返回 204"),
    "400": QT_TRANSLATE_NOOP("MockView", "返回 400"),
    "404": QT_TRANSLATE_NOOP("MockView", "返回 404"),
    "500": QT_TRANSLATE_NOOP("MockView", "返回 500"),
}


def extra_label(value: str) -> str:
    return resolve_marker(EXTRA_LABELS, value, "MockView", str(value))


class MockInterface(QWidget):
    """Mock 响应池页。总开关关 = 原生 flowmap 清空，所有请求照常直连。"""

    def __init__(self, controller: MockController, parent=None):
        super().__init__(parent)
        self.setObjectName("MockInterface")
        self.controller = controller
        self._search_text = ""  # titlebar 框回填源（协议 current_search_text，§4.2）

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self.controller.attach_config_watchers()
        self._on_pool_changed(self.controller.snapshot)

    # —— 构造 ——

    def __init_widget(self):
        self.toolbar = self.__build_toolbar()

        self.source_model = MockPoolTableModel(self)
        self.proxy_model = MockPoolFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)

        self.table = TableView(self)
        self.table.verticalHeader().hide()
        self.table.setModel(self.proxy_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate([80, 70, 90]):
            self.table.setColumnWidth(i, w)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.empty_page = self.__build_empty_page()
        self.content_stack = QStackedWidget(self)
        self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.empty_page)
        self.knob_panel = self.__build_knob_panel()

    def __build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.import_btn = PushButton(FluentIcon.ADD, self.tr("从 Flow 文件导入"), bar)

        self.count_label = CaptionLabel(self.tr("{} 条").format(0), bar)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, bar)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setIconSize(QSize(18, 18))
        self.delete_btn.setToolTip(self.tr("删除选中项"))
        self.delete_btn.setEnabled(False)

        self.more_btn = TransparentToolButton(FluentIcon.MORE, bar)
        self.more_btn.setFixedSize(32, 32)
        self.more_btn.setIconSize(QSize(18, 18))
        self.more_btn.setToolTip(self.tr("更多操作"))

        self.enable_switch = SwitchButton(bar, IndicatorPosition.LEFT)
        self.enable_switch.setOnText(self.tr("已启用"))
        self.enable_switch.setOffText(self.tr("已停用"))
        self.enable_switch.setToolTip(
            self.tr("关闭后所有请求照常直连；开启即用池里的响应顶上")
        )
        self._sync_switch(self.controller.enabled)

        layout.addWidget(self.import_btn)
        layout.addStretch(1)
        layout.addWidget(self.count_label)
        layout.addWidget(self.delete_btn)
        layout.addWidget(self.more_btn)
        layout.addSpacing(6)
        layout.addWidget(self.enable_switch)
        return bar

    def __build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("Mock 响应池是空的"), page)
        hint = CaptionLabel(
            self.tr(
                "在捕获页右键流量选择「加入 Mock 响应」，或从 Flow 文件导入；"
                "开启总开关后，匹配到的请求将直接用录好的响应顶回"
            ),
            page,
        )
        import_btn = PushButton(FluentIcon.ADD, self.tr("从 Flow 文件导入"), page)
        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(import_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        import_btn.clicked.connect(self._on_import)
        return page

    def __build_knob_panel(self) -> QWidget:
        panel = QWidget(self)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(12, 0, 12, 8)
        outer.setSpacing(4)

        toggle_row = QWidget(panel)
        toggle_layout = QHBoxLayout(toggle_row)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        toggle_layout.setSpacing(4)
        self.knob_toggle_btn = TransparentToolButton(
            FluentIcon.CHEVRON_RIGHT_MED, toggle_row
        )
        self.knob_toggle_btn.setFixedSize(26, 26)
        self.knob_toggle_btn.setIconSize(QSize(14, 14))
        knob_title = CaptionLabel(self.tr("匹配设置"), toggle_row)
        self.summary_label = CaptionLabel("", toggle_row)
        toggle_layout.addWidget(self.knob_toggle_btn)
        toggle_layout.addWidget(knob_title)
        toggle_layout.addSpacing(8)
        toggle_layout.addWidget(self.summary_label, 1)
        outer.addWidget(toggle_row)

        # 卡片放进一个**定高可滚动**容器：六张卡全展开远超页面余量，而设置卡都是
        # 定高件，布局压不动它们 —— 直接竖排会像截图里那样整片重叠（设置卡在
        # 设置页不重叠，靠的就是整页 ScrollArea）。这里内部滚动兜底，表格最多被
        # 吃掉这一段固定高度，永远不会被叠压。
        self.cards_scroll = ScrollArea(panel)
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.cards_scroll.enableTransparentBackground()
        cards_inner = QWidget(self.cards_scroll)
        cards_layout = QVBoxLayout(cards_inner)
        cards_layout.setContentsMargins(0, 2, 8, 6)
        cards_layout.setSpacing(4)

        self.extra_card = OptionsSettingCard(
            CONFIG.mock_extra,
            FluentIcon.FILTER,
            self.tr("未命中策略"),
            self.tr("请求没匹配到任何 Mock 响应时的处理"),
            texts=[extra_label(value) for value in EXTRA_VALUES],
            parent=cards_inner,
        )
        self.reuse_card = SwitchSettingCard(
            icon=FluentIcon.SYNC,
            title=self.tr("可重复使用"),
            content=self.tr(
                "关闭后每条 Mock 响应只回一次；池耗尽后请求将直连上游，未命中策略不再生效"
            ),
            configItem=CONFIG.mock_reuse,
            parent=cards_inner,
        )
        self.refresh_card = SwitchSettingCard(
            icon=FluentIcon.DATE_TIME,
            title=self.tr("刷新日期头"),
            content=self.tr("命中后更新 Date/Expires/Last-Modified 与 Cookie 过期时间"),
            configItem=CONFIG.mock_refresh,
            parent=cards_inner,
        )
        self.ignore_host_card = SwitchSettingCard(
            icon=FluentIcon.GLOBE,
            title=self.tr("忽略主机"),
            content=self.tr("匹配时不比对请求的主机名"),
            configItem=CONFIG.mock_ignore_host,
            parent=cards_inner,
        )
        self.params_card = PushSettingCard(
            self.tr("配置…"),
            FluentIcon.FILTER,
            self.tr("忽略 Query 参数"),
            parent=cards_inner,
        )
        self.headers_card = PushSettingCard(
            self.tr("配置…"),
            FluentIcon.LABEL,
            self.tr("匹配指定请求头"),
            parent=cards_inner,
        )
        for card in (
            self.extra_card,
            self.reuse_card,
            self.refresh_card,
            self.ignore_host_card,
            self.params_card,
            self.headers_card,
        ):
            cards_layout.addWidget(card)
        cards_layout.addStretch(1)
        self.cards_scroll.setWidget(cards_inner)
        # 展开后的可视高度 ≈ 五张卡；「未命中策略」再展开时靠内部滚动看全。
        self.cards_scroll.setFixedHeight(336)
        self.cards_scroll.setVisible(False)
        outer.addWidget(self.cards_scroll)
        self._sync_summary()
        self._sync_list_cards()
        return panel

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.content_stack, 1)
        layout.addWidget(self.knob_panel)

    def __connect_signal_to_slot(self):
        self.import_btn.clicked.connect(self._on_import)
        self.delete_btn.clicked.connect(self._on_delete)
        self.more_btn.clicked.connect(self._on_more_menu)
        self.knob_toggle_btn.clicked.connect(self._on_toggle_knobs)
        self.enable_switch.checkedChanged.connect(self.controller.set_enabled)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._update_action_state)

        self.controller.pool_changed.connect(self._on_pool_changed)
        self.controller.enabled_changed.connect(self._sync_switch)
        self.controller.knobs_changed.connect(self._sync_summary)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)
        self.params_card.clicked.connect(self._on_edit_params)
        self.headers_card.clicked.connect(self._on_edit_headers)

        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table).activated.connect(
            self._on_delete
        )

    # —— helpers ——

    def _selected_entry_ids(self) -> list[str]:
        rows = self.table.selectionModel().selectedRows()
        ids: list[str] = []
        for index in sorted(rows):
            source = self.proxy_model.mapToSource(index).row()
            entry = self.source_model.entry_at(source)
            if entry is not None:
                ids.append(str(entry.get("id", "")))
        return [entry_id for entry_id in ids if entry_id]

    def _sync_switch(self, enabled: bool) -> None:
        self.enable_switch.blockSignals(True)
        self.enable_switch.setChecked(enabled)
        self.enable_switch.blockSignals(False)

    def _sync_summary(self) -> None:
        parts = [extra_label(str(CONFIG.get(CONFIG.mock_extra)))]
        parts.append(
            self.tr("可重复") if CONFIG.get(CONFIG.mock_reuse) else self.tr("单次消耗")
        )
        if CONFIG.get(CONFIG.mock_ignore_host):
            parts.append(self.tr("忽略主机"))
        params = list(CONFIG.get(CONFIG.mock_ignore_params))
        if params:
            parts.append(self.tr("忽略参数 {} 项").format(len(params)))
        headers = list(CONFIG.get(CONFIG.mock_use_headers))
        if headers:
            parts.append(self.tr("比对请求头 {} 项").format(len(headers)))
        self.summary_label.setText(" · ".join(parts))

    def _sync_list_cards(self) -> None:
        params = list(CONFIG.get(CONFIG.mock_ignore_params))
        headers = list(CONFIG.get(CONFIG.mock_use_headers))
        self.params_card.setContent("、".join(params) if params else self.tr("未配置"))
        self.headers_card.setContent(
            "、".join(headers) if headers else self.tr("未配置")
        )

    def _update_content_state(self) -> None:
        has_entries = self.source_model.rowCount() > 0
        self.content_stack.setCurrentWidget(
            self.table if has_entries else self.empty_page
        )

    @Slot()
    def _update_action_state(self) -> None:
        self.delete_btn.setEnabled(bool(self._selected_entry_ids()))

    @Slot()
    def _on_toggle_knobs(self) -> None:
        expanded = not self.cards_scroll.isVisible()
        self.cards_scroll.setVisible(expanded)
        self.knob_toggle_btn.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
        )

    # —— slots ——

    @Slot(dict)
    def _on_pool_changed(self, snapshot: dict[str, Any]) -> None:
        self.source_model.set_entries(snapshot.get("entries", []))
        self._sync_switch(self.controller.enabled)
        self.count_label.setText(self.tr("{} 条").format(int(snapshot.get("count", 0))))
        self._update_content_state()
        self._update_action_state()

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str) -> None:
        show_error(title, detail, self.window())

    @Slot(str)
    def _on_operation_succeeded(self, message: str) -> None:
        show_success(self.tr("成功"), message, self.window())

    # --- titlebar 搜索协议（规格 §4.2）---

    def search_placeholder(self) -> str:
        return self.tr("搜索响应池")

    def apply_search(self, text: str) -> None:
        self._search_text = text
        self.proxy_model.set_filter_text(text)
        self._update_action_state()

    def current_search_text(self) -> str:
        return self._search_text

    def search_focus_target(self) -> QWidget:
        return self.table

    @Slot()
    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 Flow 文件导入"),
            "",
            self.tr("Flow 文件 (*.flow)"),
        )
        if path:
            self.controller.import_file(path)

    @Slot()
    def _on_delete(self) -> None:
        entry_ids = self._selected_entry_ids()
        if entry_ids:
            self.controller.remove_entries(entry_ids)

    @Slot()
    def _on_more_menu(self) -> None:
        menu = RoundMenu(parent=self)
        menu.closedSignal.connect(menu.deleteLater)
        export_action = BaseAction(
            FluentIcon.SHARE, self.tr("导出池为 Flow 文件…"), menu
        )
        export_action.setEnabled(self.source_model.rowCount() > 0)
        clear_action = BaseAction(FluentIcon.DELETE, self.tr("清空全部"), menu)
        clear_action.setEnabled(self.source_model.rowCount() > 0)
        menu.addAction(export_action)
        menu.addAction(clear_action)
        export_action.triggered.connect(self._on_export)
        clear_action.triggered.connect(self.controller.clear_all)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    @Slot()
    def _on_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("导出 Mock 响应池"),
            "",
            self.tr("Flow 文件 (*.flow)"),
        )
        if path:
            self.controller.export_pool(path)

    @Slot()
    def _on_edit_params(self) -> None:
        self._edit_string_list(
            CONFIG.mock_ignore_params,
            self.tr("忽略 Query 参数"),
            self.tr("每行一个参数名，匹配时不比对这些参数的取值"),
        )

    @Slot()
    def _on_edit_headers(self) -> None:
        self._edit_string_list(
            CONFIG.mock_use_headers,
            self.tr("匹配指定请求头"),
            self.tr("每行一个请求头名，匹配时额外比对这些头的取值"),
        )

    def _edit_string_list(self, item, title: str, hint: str) -> None:
        dialog = StringListDialog(
            title, hint, list(CONFIG.get(item)), parent=self.window()
        )
        try:
            if dialog.exec():
                CONFIG.set(item, dialog.items())
            # valueChanged（含同值短路不触发的情况）都同步一次卡片文案。
            self._sync_list_cards()
        finally:
            dialog.deleteLater()

    @Slot()
    def _on_context_menu(self, pos) -> None:
        # 右键先选中光标下的行（issues #50）：qfw 表格默认不开右键选行，选 A 右键
        # B 时删除还落在 A 上。点中已选中的一行则保留多选、菜单作用于整批。
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        if not self.table.selectionModel().isSelected(index):
            self.table.selectRow(index.row())
        entry_ids = self._selected_entry_ids()
        if not entry_ids:
            return
        menu = RoundMenu(parent=self.table)
        menu.closedSignal.connect(menu.deleteLater)
        delete_action = BaseAction(
            FluentIcon.DELETE,
            self.tr("删除")
            if len(entry_ids) <= 1
            else self.tr("删除 {} 条").format(len(entry_ids)),
            menu,
        )
        delete_action.triggered.connect(self._on_delete)
        menu.addAction(delete_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))
