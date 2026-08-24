"""Breakpoint interface: the rule list, plus the way back to the breakpoint window."""

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
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
    IndicatorPosition,
    InfoBadge,
    InfoBadgePosition,
    LineEdit,
    PushButton,
    RoundMenu,
    SwitchButton,
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.intercept.controllers import InterceptController
from ferret.apps.intercept.dialogs import InterceptRuleDialog
from ferret.apps.intercept.models import (
    InterceptRuleFilterProxyModel,
    InterceptRuleTableModel,
)


class InterceptInterface(QWidget):
    """断点页：规则表 + 总开关。命中之后的处理不在这里。

    拦不拦由 mitmproxy 原生的 `Intercept` addon 决定（读一条 flowfilter 表达式），
    ferret 只负责把这张表编译成表达式。规则**不选阶段** —— 原生那两个钩子共用同一个
    过滤器，命中的流量在请求发出前停一次、响应回来后再停一次（见 `InterceptPhase`）。

    队列和编辑器长在独立的 `InterceptWindow` 里：断点一命中就得让人看见并动手，藏在
    一个要自己切过来的页面里等于没提醒。这一页只留一个带计数的入口按钮 —— 用户在关窗
    时选了「保持挂起并隐藏」之后，它是唯一能把那些还钉着客户端的流量找回来的路。
    """

    #: 用户点「拦截队列」，要求把断点窗口叫到前台。由 `MainWindow` 牵线到
    #: `InterceptWindow.pop_up()`，这一页不必认识那个窗口（沿用主窗口牵线的先例）。
    queue_requested = Signal()

    def __init__(self, controller: InterceptController, parent=None):
        super().__init__(parent)
        self.setObjectName("InterceptInterface")
        self.controller = controller

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._on_rules_changed(self.controller.rules)
        self._on_flows_changed(self.controller.flows)
        # 页面起来得比内核晚，起来时可能已经有拦下的流量在等着了。
        self.controller.refresh_flows()

    # —— 构造 ——

    def __init_widget(self):
        self.rules_page = self.__build_rules_page()

    def __build_rules_page(self) -> QWidget:
        page = QWidget(self)

        self.rule_model = InterceptRuleTableModel(self)
        self.rule_proxy = InterceptRuleFilterProxyModel(self)
        self.rule_proxy.setSourceModel(self.rule_model)

        self.rule_table = TableView(page)
        self.rule_table.verticalHeader().hide()
        self.rule_table.setModel(self.rule_proxy)
        self.rule_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rule_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.rule_table.setWordWrap(False)
        widths = [60, 110, 100, 260]
        header = self.rule_table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate(widths):
            self.rule_table.setColumnWidth(i, w)
        header.setSectionResizeMode(len(widths) - 1, QHeaderView.ResizeMode.Stretch)
        self.rule_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.rule_empty_page = self.__build_rules_empty_page(page)
        self.rule_stack = QStackedWidget(page)
        self.rule_stack.addWidget(self.rule_table)
        self.rule_stack.addWidget(self.rule_empty_page)

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.__build_rule_bar(page))
        layout.addWidget(self.rule_stack, 1)
        return page

    def __build_rule_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.add_btn = PushButton(FluentIcon.ADD, self.tr("Add rule"), bar)

        self.search_edit = LineEdit(bar)
        self.search_edit.setPlaceholderText(self.tr("Search rules"))
        self.search_edit.setFixedHeight(32)
        self.search_edit.setClearButtonEnabled(True)

        self.edit_btn = TransparentToolButton(FluentIcon.EDIT, bar)
        self.edit_btn.setFixedSize(32, 32)
        self.edit_btn.setIconSize(QSize(18, 18))
        self.edit_btn.setToolTip(self.tr("Edit") + " (F2)")
        self.edit_btn.setEnabled(False)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, bar)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setIconSize(QSize(18, 18))
        self.delete_btn.setToolTip(self.tr("Delete"))
        self.delete_btn.setEnabled(False)

        self.queue_btn = PushButton(FluentIcon.PAUSE_BOLD, self.tr("Held queue"), bar)
        self.queue_btn.setToolTip(
            self.tr("Open the breakpoint window and deal with the held traffic")
        )
        self.queue_btn.setEnabled(False)
        # 徽标挂在按钮右上角，父级得是条本身（按钮会被布局裁掉溢出的部分）。
        self.queue_badge = InfoBadge.attension(
            0, bar, self.queue_btn, InfoBadgePosition.TOP_RIGHT
        )
        self.queue_badge.hide()

        self.enable_switch = SwitchButton(bar, IndicatorPosition.LEFT)
        self.enable_switch.setOnText(self.tr("Enabled"))
        self.enable_switch.setOffText(self.tr("Disabled"))
        self.enable_switch.setToolTip(
            self.tr(
                "Breakpoint master switch. Turning it off stops holding new "
                "traffic; whatever is already held still needs handling."
            )
        )
        self._sync_switch(self.controller.enabled)

        layout.addWidget(self.add_btn)
        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.edit_btn)
        layout.addWidget(self.delete_btn)
        layout.addSpacing(6)
        layout.addWidget(self.queue_btn)
        layout.addSpacing(6)
        layout.addWidget(self.enable_switch)
        return bar

    def __build_rules_empty_page(self, parent: QWidget) -> QWidget:
        page = QWidget(parent)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("No breakpoint rules yet"), page)
        hint = CaptionLabel(
            self.tr(
                "Matching traffic is held twice: once before the request goes "
                "out and once after the response comes back"
            ),
            page,
        )
        add_btn = PushButton(FluentIcon.ADD, self.tr("Add rule"), page)
        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        add_btn.clicked.connect(self._on_add)
        return page

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.rules_page, 1)

    def __connect_signal_to_slot(self):
        self.queue_btn.clicked.connect(self.queue_requested)
        self.enable_switch.checkedChanged.connect(self.controller.set_intercept_enabled)

        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn.clicked.connect(self._on_delete)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.rule_table.doubleClicked.connect(self._on_rule_activated)
        self.rule_table.customContextMenuRequested.connect(self._on_rule_context_menu)
        self.rule_table.selectionModel().selectionChanged.connect(
            self._update_rule_action_state
        )

        self.rule_model.enabled_toggled.connect(self.controller.set_enabled)
        self.controller.rules_changed.connect(self._on_rules_changed)
        self.controller.enabled_changed.connect(self._sync_switch)
        self.controller.flows_changed.connect(self._on_flows_changed)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.search_edit.setFocus()
        )
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.rule_table).activated.connect(
            self._on_delete
        )
        QShortcut(QKeySequence(Qt.Key.Key_F2), self.rule_table).activated.connect(
            self._on_edit
        )

    # —— 规则辅助 ——

    def _selected_rule_rows(self) -> list[int]:
        rows = self.rule_table.selectionModel().selectedRows()
        return sorted(self.rule_proxy.mapToSource(index).row() for index in rows)

    @Slot()
    def _update_rule_action_state(self):
        rows = self._selected_rule_rows()
        self.edit_btn.setEnabled(len(rows) == 1)
        self.delete_btn.setEnabled(bool(rows))

    # —— 槽 ——

    @Slot(list)
    def _on_flows_changed(self, flows: list):
        """页面这边只剩一件事：把队列长度显示在入口按钮上。

        窗口自己会在空→非空时弹出来（`InterceptWindow._on_flows_changed`），这里
        不重复判那条边沿 —— 两处各判一次必然会有一处漏。
        """
        count = len(flows)
        self.queue_btn.setEnabled(count > 0)
        self.queue_badge.setText(str(count))
        self.queue_badge.setVisible(count > 0)
        self.queue_badge.adjustSize()
        self.queue_badge.raise_()

    @Slot(list)
    def _on_rules_changed(self, rules: list):
        self.rule_model.set_rules(rules)
        self.rule_stack.setCurrentWidget(
            self.rule_table if rules else self.rule_empty_page
        )
        self._update_rule_action_state()

    @Slot(str)
    def _on_search_changed(self, text: str):
        self.rule_proxy.set_filter_text(text)
        self._update_rule_action_state()

    @Slot()
    def _on_add(self):
        dialog = InterceptRuleDialog(
            self.tr("New breakpoint rule"), parent=self.window()
        )
        if dialog.exec():
            self.controller.add_rule(dialog.get_rule())

    @Slot(QModelIndex)
    def _on_rule_activated(self, index: QModelIndex):
        self._on_edit()

    @Slot()
    def _on_edit(self):
        rows = self._selected_rule_rows()
        if len(rows) != 1:
            return
        rule = self.controller.rule_at(rows[0])
        if rule is None:
            return
        dialog = InterceptRuleDialog(
            self.tr("Edit breakpoint rule"), rule=rule, parent=self.window()
        )
        if dialog.exec():
            self.controller.update_rule(rows[0], dialog.get_rule())

    @Slot()
    def _on_delete(self):
        rows = self._selected_rule_rows()
        if rows:
            self.controller.remove_rules(rows)

    @Slot(QPoint)
    def _on_rule_context_menu(self, pos: QPoint):
        rows = self._selected_rule_rows()
        if not rows:
            return
        menu = RoundMenu(parent=self.rule_table)
        if len(rows) == 1:
            row = rows[0]
            rule = self.controller.rule_at(row)
            edit_action = BaseAction(
                icon=FluentIcon.EDIT, text=self.tr("Edit"), parent=menu
            )
            edit_action.triggered.connect(self._on_edit)
            menu.addAction(edit_action)
            if rule is not None:
                target = not rule.enabled
                toggle_action = BaseAction(
                    icon=FluentIcon.VIEW if target else FluentIcon.HIDE,
                    text=self.tr("Enable") if target else self.tr("Disable"),
                    parent=menu,
                )
                toggle_action.triggered.connect(
                    lambda: self.controller.set_enabled(row, target)
                )
                menu.addAction(toggle_action)
        # 这里没有「上移/下移」：所有启用的规则会被 OR 成同一条 flowfilter 表达式，
        # 行序不影响拦不拦（详见 `InterceptRuleTableModel` 的说明）。
        delete_action = BaseAction(
            icon=FluentIcon.DELETE, text=self.tr("Delete"), parent=menu
        )
        delete_action.triggered.connect(self._on_delete)
        menu.addAction(delete_action)
        menu.exec(self.rule_table.viewport().mapToGlobal(pos))

    @Slot(bool)
    def _sync_switch(self, enabled: bool):
        # SwitchButton.setChecked 也会发 checkedChanged（`switch_button.py:219` 把
        # indicator.toggled 直连到了它），不挡住就会绕回控制器再来一轮。
        # 文字不用自己设：indicator.toggled 顺带触发的 `_updateText` 会按
        # setOnText/setOffText 换好。
        self.enable_switch.blockSignals(True)
        self.enable_switch.setChecked(enabled)
        self.enable_switch.blockSignals(False)

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str):
        show_error(title, detail, self.window())

    @Slot(str)
    def _on_operation_succeeded(self, message: str):
        show_success(self.tr("Success"), message, self.window())
