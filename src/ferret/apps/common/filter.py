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

        self.field_box = ComboBox(self)
        self.field_box.setMinimumWidth(96)
        self.field_box.setMaximumWidth(132)
        self.field_box.addItems(["全部", "URL", "Method", "Header", "Body"])

        self.logic_box = ComboBox(self)
        self.logic_box.setMinimumWidth(104)
        self.logic_box.setMaximumWidth(140)
        self.logic_box.addItems(["包含", "不包含", "正则表达式", "等于"])

        self.value_input = LineEdit(self)
        self.value_input.setMinimumWidth(160)
        self.value_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.value_input.setPlaceholderText("搜索内容...")

        self.remove_btn = TransparentToolButton(FluentIcon.REMOVE_FROM, self)
        self.add_btn = TransparentToolButton(FluentIcon.ADD_TO, self)
        for button, tooltip in (
            (self.remove_btn, self.tr("删除条件")),
            (self.add_btn, self.tr("添加条件")),
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
        self.field_box.currentIndexChanged.connect(lambda _: self.filterChanged.emit())
        self.logic_box.currentIndexChanged.connect(lambda _: self.filterChanged.emit())
        self.value_input.textChanged.connect(lambda _: self.filterChanged.emit())

    def get_condition(self) -> dict | None:
        """返回当前行的过滤条件，未启用或无值则返回 None"""
        if not self.check_box.isChecked():
            return None
        text = self.value_input.text().strip()
        if not text:
            return None
        return {
            "field": self.field_box.currentText(),
            "logic": self.logic_box.currentText(),
            "value": text,
        }

    def reset_condition(self) -> None:
        """Reset this row without emitting intermediate condition changes."""
        blocked = self.blockSignals(True)
        self.check_box.setChecked(True)
        self.field_box.setCurrentIndex(0)
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
        self.clear_btn = TransparentPushButton(FluentIcon.CLEAR_SELECTION, "清除全部", self)
        self.close_btn = TransparentPushButton(FluentIcon.UP, "收起", self)
        self.clear_btn.setToolTip(self.tr("清除全部筛选条件"))
        self.close_btn.setToolTip(self.tr("收起筛选面板"))
        self.clear_btn.setAccessibleName(self.tr("清除全部筛选条件"))
        self.close_btn.setAccessibleName(self.tr("收起筛选面板"))

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
        self.summary_label.setText(self.tr("{} 个有效条件").format(count))
        self.clear_btn.setEnabled(count > 0 or len(self._rows()) > 1)

    @Slot()
    def _on_condition_changed(self) -> None:
        self._update_summary()
        self.conditionsChanged.emit()
