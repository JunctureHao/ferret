"""Rewrite-rule interface: toolbar + rule table."""

from PySide6.QtCore import QCoreApplication, QModelIndex, QPoint, QSize, Qt, Slot
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
    ComboBox,
    FluentIcon,
    IndicatorPosition,
    LineEdit,
    PushButton,
    RoundMenu,
    SwitchButton,
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.rewrite.controllers import RewriteController
from ferret.apps.rewrite.dialogs import RewriteRuleDialog
from ferret.apps.rewrite.models import (
    KIND_LABELS,
    RewriteRuleFilterProxyModel,
    RewriteRuleTableModel,
)
from ferret.core.mitm import RewriteKind
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 类型筛选下拉的第 0 项；其余项按 `RewriteKind` 枚举序跟在后面（§4.2）。
_ALL_KINDS_MARKER = QT_TRANSLATE_NOOP("RewriteView", "全部类型")


class RewriteInterface(QWidget):
    """重写规则页：八种重写类型由自研统一引擎执行（行序＝执行序）。

    类型列与对话框的类型下拉都由 `RewriteKind` 生成；工具栏带类型筛选、
    批量启停走右键菜单、总开关在最右（Fluent 改版清单 §4）。
    """

    def __init__(self, controller: RewriteController, parent=None):
        super().__init__(parent)
        self.setObjectName("RewriteInterface")
        self.controller = controller

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._on_rules_changed(self.controller.rules)

    def __init_widget(self):
        self.toolbar = self.__build_toolbar()

        self.source_model = RewriteRuleTableModel(self)
        self.proxy_model = RewriteRuleFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)

        self.table = TableView(self)
        self.table.verticalHeader().hide()
        self.table.setModel(self.proxy_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        widths = [60, 110, 90, 250, 160, 240]
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)
        header.setSectionResizeMode(len(widths) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.empty_page = self.__build_empty_page()
        self.content_stack = QStackedWidget(self)
        self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.empty_page)

    def __build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.add_btn = PushButton(FluentIcon.ADD, self.tr("新增规则"), bar)

        self.search_edit = LineEdit(bar)
        self.search_edit.setPlaceholderText(self.tr("搜索规则"))
        self.search_edit.setFixedHeight(32)
        self.search_edit.setClearButtonEnabled(True)

        # 类型筛选（§4.2）：与文字搜索 AND。文案表只存标记，使用点求值。
        self.kind_combo = ComboBox(bar)
        self.kind_combo.addItem(
            QCoreApplication.translate("RewriteView", _ALL_KINDS_MARKER)
        )
        self.kind_combo.addItems(
            [
                QCoreApplication.translate("RewriteKind", KIND_LABELS[kind])
                for kind in RewriteKind
            ]
        )
        self.kind_combo.setFixedHeight(32)
        self.kind_combo.setCurrentIndex(0)

        self.edit_btn = TransparentToolButton(FluentIcon.EDIT, bar)
        self.edit_btn.setFixedSize(32, 32)
        self.edit_btn.setIconSize(QSize(18, 18))
        self.edit_btn.setToolTip(self.tr("编辑") + " (F2)")
        self.edit_btn.setEnabled(False)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, bar)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setIconSize(QSize(18, 18))
        self.delete_btn.setToolTip(self.tr("删除"))
        self.delete_btn.setEnabled(False)

        # 总开关（§4.4）：Off ＝ 所有重写规则一律不生效，流量原样转发；
        # 不碰各行规则的 enabled 落盘值。
        self.enable_switch = SwitchButton(bar, IndicatorPosition.LEFT)
        self.enable_switch.setOnText(self.tr("已启用"))
        self.enable_switch.setOffText(self.tr("已停用"))
        self.enable_switch.setToolTip(
            self.tr("关闭后所有重写规则一律不生效，流量原样转发")
        )
        self._sync_switch(self.controller.enabled)

        layout.addWidget(self.add_btn)
        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.kind_combo)
        layout.addWidget(self.edit_btn)
        layout.addWidget(self.delete_btn)
        layout.addSpacing(6)
        layout.addWidget(self.enable_switch)
        return bar

    def __build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("暂无重写规则"), page)
        hint = CaptionLabel(
            self.tr("可改写请求/响应的头与体，也可把请求重定向到别的地址或本地文件"),
            page,
        )
        add_btn = PushButton(FluentIcon.ADD, self.tr("新增规则"), page)
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
        layout.addWidget(self.toolbar)
        layout.addWidget(self.content_stack, 1)

    def __connect_signal_to_slot(self):
        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn.clicked.connect(self._on_delete)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.kind_combo.currentIndexChanged.connect(self._on_kind_filter_changed)
        self.enable_switch.checkedChanged.connect(self.controller.set_rewrite_enabled)
        self.table.doubleClicked.connect(self._on_row_activated)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._update_action_state)

        self.source_model.enabled_toggled.connect(self.controller.set_enabled)
        self.controller.rules_changed.connect(self._on_rules_changed)
        self.controller.enabled_changed.connect(self._sync_switch)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.search_edit.setFocus()
        )
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table).activated.connect(
            self._on_delete
        )
        QShortcut(QKeySequence(Qt.Key.Key_F2), self.table).activated.connect(
            self._on_edit
        )

    # --- helpers ---

    def _selected_rows(self) -> list[int]:
        rows = self.table.selectionModel().selectedRows()
        return sorted(self.proxy_model.mapToSource(index).row() for index in rows)

    def _update_content_state(self):
        has_rules = self.source_model.rowCount() > 0
        self.content_stack.setCurrentWidget(
            self.table if has_rules else self.empty_page
        )

    @Slot()
    def _update_action_state(self):
        rows = self._selected_rows()
        self.edit_btn.setEnabled(len(rows) == 1)
        self.delete_btn.setEnabled(bool(rows))

    # --- slots ---

    @Slot(list)
    def _on_rules_changed(self, rules: list):
        self.source_model.set_rules(rules)
        self._sync_switch(self.controller.enabled)
        self._update_content_state()
        self._update_action_state()

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str):
        show_error(title, detail, self.window())

    @Slot(str)
    def _on_operation_succeeded(self, message: str):
        show_success(self.tr("成功"), message, self.window())

    @Slot(str)
    def _on_search_changed(self, text: str):
        self.proxy_model.set_filter_text(text)
        self._update_action_state()

    @Slot(int)
    def _on_kind_filter_changed(self, index: int):
        """下拉第 0 项 = 全部类型；其余按 `RewriteKind` 枚举序一一对应。"""
        kind = RewriteKind(index - 1) if index > 0 else None
        self.proxy_model.set_filter_kind(kind)
        self._update_action_state()

    def _sync_switch(self, enabled: bool):
        """总开关与控制器状态对齐；blockedSignals 防止回环触发 checkedChanged。"""
        self.enable_switch.blockSignals(True)
        self.enable_switch.setChecked(enabled)
        self.enable_switch.blockSignals(False)

    @Slot()
    def _on_add(self):
        dialog = RewriteRuleDialog(self.tr("新增重写规则"), parent=self.window())
        if dialog.exec():
            self.controller.add_rule(dialog.get_rule())

    @Slot(QModelIndex)
    def _on_row_activated(self, index: QModelIndex):
        self._on_edit()

    @Slot()
    def _on_edit(self):
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        rule = self.controller.rule_at(rows[0])
        if rule is None:
            return
        dialog = RewriteRuleDialog(
            self.tr("编辑重写规则"), rule=rule, parent=self.window()
        )
        if dialog.exec():
            self.controller.update_rule(rows[0], dialog.get_rule())

    @Slot()
    def _on_delete(self):
        rows = self._selected_rows()
        if rows:
            self.controller.remove_rules(rows)

    @Slot(QPoint)
    def _on_context_menu(self, pos: QPoint):
        rows = self._selected_rows()
        if not rows:
            return
        menu = RoundMenu(parent=self.table)
        if len(rows) == 1:
            row = rows[0]
            rule = self.controller.rule_at(row)
            edit_action = BaseAction(
                icon=FluentIcon.EDIT, text=self.tr("编辑"), parent=menu
            )
            edit_action.triggered.connect(self._on_edit)
            menu.addAction(edit_action)
            if rule is not None:
                target = not rule.enabled
                toggle_action = BaseAction(
                    icon=FluentIcon.VIEW if target else FluentIcon.HIDE,
                    text=self.tr("启用") if target else self.tr("停用"),
                    parent=menu,
                )
                toggle_action.triggered.connect(
                    lambda: self.controller.set_enabled(row, target)
                )
                menu.addAction(toggle_action)
            # 顺序即生效顺序：自研引擎对同一条流量逐条作用（没有短路），
            # 所以上下移动是有语义的操作，不只是排版。
            total = len(self.controller.rules)
            up_action = BaseAction(
                icon=FluentIcon.UP, text=self.tr("上移"), parent=menu
            )
            up_action.setEnabled(row > 0)
            up_action.triggered.connect(lambda: self.controller.move_rule(row, -1))
            menu.addAction(up_action)
            down_action = BaseAction(
                icon=FluentIcon.DOWN, text=self.tr("下移"), parent=menu
            )
            down_action.setEnabled(row < total - 1)
            down_action.triggered.connect(lambda: self.controller.move_rule(row, 1))
            menu.addAction(down_action)
        elif len(rows) > 1:
            # 批量启停（§4.3）：多选时给两个动作，一次 `_commit` 整批下发。
            enable_action = BaseAction(
                icon=FluentIcon.VIEW, text=self.tr("启用"), parent=menu
            )
            enable_action.triggered.connect(
                lambda: self.controller.set_rules_enabled(rows, True)
            )
            menu.addAction(enable_action)
            disable_action = BaseAction(
                icon=FluentIcon.HIDE, text=self.tr("停用"), parent=menu
            )
            disable_action.triggered.connect(
                lambda: self.controller.set_rules_enabled(rows, False)
            )
            menu.addAction(disable_action)
        delete_action = BaseAction(
            icon=FluentIcon.DELETE, text=self.tr("删除"), parent=menu
        )
        delete_action.triggered.connect(self._on_delete)
        menu.addAction(delete_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))
