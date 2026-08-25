from PySide6.QtCore import QSize, Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    TransparentPushButton,
    TransparentToolButton,
)

# 过滤字段的**取值**（不是界面文案）。`get_condition()` 送出去的就是这些，
# `apps/capture/services.py` 拿它映射 flowfilter 操作符。取值与文案必须分开：
# 早先两者是同一个中文串，翻译一开下游就会静默失配、筛选整条失效。
FILTER_FIELDS = ("all", "URL", "Method", "Header", "Body", "WebSocket")

# 不吃值的字段。原生 `~websocket`（`flowfilter.FWebSocket`）问的是「这条流量是不是
# WS」，压根不带参数，所以这一类字段只能问 是 / 不是，输入框整个用不上。
FILTER_FLAG_FIELDS = frozenset({"WebSocket"})

# 过滤逻辑的取值，理由同上。顺序即下拉框顺序，索引 0 是默认项。
FILTER_LOGICS = ("contains", "excludes", "regex", "equals")

# `FILTER_FLAG_FIELDS` 那类字段专用的逻辑集，选中时整组换掉上面那四个。
FILTER_FLAG_LOGICS = ("is", "is not")


class FilterRow(QWidget):
    """动态过滤行：包含复选框、下拉框、输入框和增减按钮"""

    addRequested = Signal()
    removeRequested = Signal()
    filterChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.check_box = CheckBox(self)
        self.check_box.setChecked(True)
        self.check_box.setFixedWidth(20)

        field_labels = {
            "all": self.tr("All"),
            "URL": "URL",
            "Method": "Method",
            "Header": "Header",
            "Body": "Body",
            # 协议名，和 URL / Method 一样不译。
            "WebSocket": "WebSocket",
        }
        self.field_box = ComboBox(self)
        self.field_box.setMinimumWidth(96)
        self.field_box.setMaximumWidth(132)
        for field in FILTER_FIELDS:
            self.field_box.addItem(field_labels[field], userData=field)

        # 存下来给 `_sync_field_mode` 复用。刻意留在方法里现算、不提到模块级：模块级
        # 会在 import 时求值，那会儿翻译器还没装上（AGENTS.md §5）。
        self._logic_labels = {
            "contains": self.tr("Contains"),
            "excludes": self.tr("Excludes"),
            "regex": self.tr("Regex"),
            "equals": self.tr("Equals"),
            "is": self.tr("Is"),
            "is not": self.tr("Is not"),
        }
        self.logic_box = ComboBox(self)
        self.logic_box.setMinimumWidth(104)
        self.logic_box.setMaximumWidth(140)
        for logic in FILTER_LOGICS:
            self.logic_box.addItem(self._logic_labels[logic], userData=logic)

        self._value_placeholder = self.tr("Search content...")
        self._flag_placeholder = self.tr("No value needed")
        self.value_input = LineEdit(self)
        self.value_input.setMinimumWidth(160)
        self.value_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.value_input.setPlaceholderText(self._value_placeholder)

        self.remove_btn = TransparentToolButton(FluentIcon.REMOVE_FROM, self)
        self.add_btn = TransparentToolButton(FluentIcon.ADD_TO, self)
        for button, tooltip in (
            (self.remove_btn, self.tr("Remove condition")),
            (self.add_btn, self.tr("Add condition")),
        ):
            button.setFixedSize(28, 28)
            button.setIconSize(QSize(16, 16))
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)

    def __init_layout(self):
        """初始化布局结构 - 输入框占据最大空间，所有控件垂直居中"""
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(0, 0, 0, 0)
        self.h_layout.setSpacing(6)

        self.h_layout.addWidget(self.check_box)
        self.h_layout.addWidget(self.field_box)
        self.h_layout.addWidget(self.logic_box)
        self.h_layout.addWidget(self.value_input, 1)  # 输入框占据剩余空间
        self.h_layout.addWidget(self.remove_btn)
        self.h_layout.addWidget(self.add_btn)

    def __connect_signal_to_slot(self):
        self.remove_btn.clicked.connect(self.removeRequested.emit)
        self.add_btn.clicked.connect(self.addRequested.emit)

        self.check_box.stateChanged.connect(lambda _: self.filterChanged.emit())
        self.field_box.currentIndexChanged.connect(self._on_field_changed)
        self.logic_box.currentIndexChanged.connect(lambda _: self.filterChanged.emit())
        self.value_input.textChanged.connect(lambda _: self.filterChanged.emit())

    @Slot(int)
    def _on_field_changed(self, _index: int) -> None:
        self._sync_field_mode()
        self.filterChanged.emit()

    def _sync_field_mode(self) -> None:
        """把逻辑下拉框和输入框调成当前字段该有的样子。

        标志字段（见 :data:`FILTER_FLAG_FIELDS`）没有值可填，所以输入框禁掉、逻辑
        换成 是 / 不是。留一个能打字但下游根本不读的输入框，比禁掉更让人困惑。
        """
        is_flag = self.field_box.currentData() in FILTER_FLAG_FIELDS
        logics = FILTER_FLAG_LOGICS if is_flag else FILTER_LOGICS

        # 只在逻辑集真的换了时候重建：字段在同一类里换（URL → Body）不该把用户选好的
        # 「排除」偷偷打回「包含」。
        if tuple(item.userData for item in self.logic_box.items) != logics:
            blocked = self.logic_box.blockSignals(True)
            self.logic_box.clear()
            for logic in logics:
                self.logic_box.addItem(self._logic_labels[logic], userData=logic)
            self.logic_box.setCurrentIndex(0)
            self.logic_box.blockSignals(blocked)

        self.value_input.setEnabled(not is_flag)
        self.value_input.setPlaceholderText(
            self._flag_placeholder if is_flag else self._value_placeholder
        )

    def get_condition(self) -> dict | None:
        """返回当前行的过滤条件，未启用或无值则返回 None"""
        if not self.check_box.isChecked():
            return None
        field = self.field_box.currentData()
        text = self.value_input.text().strip()
        # 标志字段本来就无值，拿「文本为空」判无效会让它永远生效不了。
        if not text and field not in FILTER_FLAG_FIELDS:
            return None
        # 送取值、不送界面文案 —— 见 FILTER_FIELDS 上面那段。
        return {
            "field": field,
            "logic": self.logic_box.currentData(),
            "value": text,
        }

    def reset_condition(self) -> None:
        """Reset this row without emitting intermediate condition changes."""
        blocked = self.blockSignals(True)
        self.check_box.setChecked(True)
        self.field_box.setCurrentIndex(0)
        # 字段本来就在 0 时上一行不会触发 `_on_field_changed`，逻辑集可能还停在标志
        # 那一组，所以这里补一次；`_sync_field_mode` 是幂等的。
        self._sync_field_mode()
        self.logic_box.setCurrentIndex(0)
        self.value_input.clear()
        self.blockSignals(blocked)


class MultiFilterManager(QWidget):
    """管理多行 FilterRow 的容器"""

    MAX_ROWS = 5

    conditionsChanged = Signal()
    panelCloseRequested = Signal()  # 最后一行被删除时发出，请求关闭面板

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.setVisible(False)

        self.summary_label = CaptionLabel(self)
        self.clear_btn = TransparentPushButton(
            FluentIcon.CLEAR_SELECTION, self.tr("Clear all"), self
        )
        self.close_btn = TransparentPushButton(FluentIcon.UP, self.tr("Collapse"), self)
        self.clear_btn.setToolTip(self.tr("Clear every filter condition"))
        self.close_btn.setToolTip(self.tr("Collapse the filter panel"))
        self.clear_btn.setAccessibleName(self.tr("Clear every filter condition"))
        self.close_btn.setAccessibleName(self.tr("Collapse the filter panel"))

    def __init_layout(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(12, 8, 12, 8)
        root_layout.setSpacing(6)

        self.v_layout = QVBoxLayout()
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(4)
        root_layout.addLayout(self.v_layout)

        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)
        footer_layout.addWidget(self.summary_label)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.clear_btn)
        footer_layout.addWidget(self.close_btn)
        root_layout.addLayout(footer_layout)

        self.add_new_row()

    def __connect_signal_to_slot(self):
        self.clear_btn.clicked.connect(self.clear_conditions)
        self.close_btn.clicked.connect(self.panelCloseRequested.emit)

    def _update_add_buttons(self):
        """根据当前行数更新所有行的添加按钮状态"""
        rows = self._rows()
        at_limit = len(rows) >= self.MAX_ROWS
        for index, row in enumerate(rows):
            is_last = index == len(rows) - 1
            row.add_btn.setVisible(is_last)
            row.add_btn.setEnabled(is_last and not at_limit)
        self._update_summary()

    @Slot()
    def add_new_row(self):
        if self.v_layout.count() >= self.MAX_ROWS:
            return
        row = FilterRow(self)
        row.addRequested.connect(self.add_new_row)
        row.removeRequested.connect(lambda: self.remove_row(row))
        row.filterChanged.connect(self._on_condition_changed)
        self.v_layout.addWidget(row)
        self._update_add_buttons()
        self.updateGeometry()
        row.value_input.setFocus()

    @Slot()
    def remove_row(self, row):
        """删除过滤行，若只剩一行则清除条件并关闭面板"""
        if self.v_layout.count() <= 1:
            self.clear_conditions()
            self.panelCloseRequested.emit()
            return
        row.filterChanged.disconnect(self._on_condition_changed)
        row.deleteLater()
        self.v_layout.removeWidget(row)
        self.conditionsChanged.emit()
        self._update_add_buttons()
        self.updateGeometry()

    def get_conditions(self) -> list[dict]:
        """收集所有活跃的过滤条件"""
        conditions = []
        for i in range(self.v_layout.count()):
            item = self.v_layout.itemAt(i)
            if item is None:
                continue
            widget = item.widget()
            if isinstance(widget, FilterRow):
                cond = widget.get_condition()
                if cond:
                    conditions.append(cond)
        return conditions

    def active_condition_count(self) -> int:
        return len(self.get_conditions())

    def showEvent(self, event: QShowEvent) -> None:
        """面板展开时自动聚焦第一个输入框"""
        super().showEvent(event)
        self.focus_first_input()

    def focus_first_input(self):
        """聚焦第一个过滤行的输入框"""
        for i in range(self.v_layout.count()):
            item = self.v_layout.itemAt(i)
            if item is not None:
                w = item.widget()
                if isinstance(w, FilterRow):
                    w.value_input.setFocus()
                    return

    @Slot()
    def clear_conditions(self):
        """清除所有过滤条件，并恢复为一行空条件。"""
        rows = self._rows()
        if not rows:
            self.add_new_row()
            rows = self._rows()

        first = rows[0]
        first.reset_condition()
        for row in rows[1:]:
            row.filterChanged.disconnect(self._on_condition_changed)
            self.v_layout.removeWidget(row)
            row.deleteLater()

        self._update_add_buttons()
        self.conditionsChanged.emit()

    def _rows(self) -> list[FilterRow]:
        rows: list[FilterRow] = []
        for index in range(self.v_layout.count()):
            item = self.v_layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, FilterRow):
                rows.append(widget)
        return rows

    def _update_summary(self) -> None:
        count = self.active_condition_count()
        self.summary_label.setText(self.tr("{} active condition(s)").format(count))
        self.clear_btn.setEnabled(count > 0 or len(self._rows()) > 1)

    @Slot()
    def _on_condition_changed(self) -> None:
        self._update_summary()
        self.conditionsChanged.emit()
