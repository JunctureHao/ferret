from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.card_widget import SimpleCardWidget


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
        self.check_box.setFixedWidth(20)  # 减去文字占地宽度

        self.field_box = ComboBox(self)
        self.field_box.setFixedWidth(140)
        self.field_box.addItems(["全部", "URL", "Method", "Header", "Body"])

        self.logic_box = ComboBox(self)
        self.logic_box.setFixedWidth(140)
        self.logic_box.addItems(["包含", "不包含", "正则表达式", "等于"])

        self.value_input = LineEdit(self)
        self.value_input.setPlaceholderText("搜索内容...")

        self.remove_btn = TransparentToolButton(FluentIcon.REMOVE_FROM, self)
        self.add_btn = TransparentToolButton(FluentIcon.ADD_TO, self)

    def __init_layout(self):
        """初始化布局结构 - 输入框占据最大空间，所有控件垂直居中"""
        self.h_layout = QHBoxLayout(self)
        self.h_layout.setContentsMargins(0, 0, 0, 0)
        self.h_layout.setSpacing(4)

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


class MultiFilterManager(SimpleCardWidget):
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

    def __init_layout(self):
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(6, 6, 0, 0)
        self.v_layout.setSpacing(2)
        self.add_new_row()

    def __connect_signal_to_slot(self):
        pass

    def _update_add_buttons(self):
        """根据当前行数更新所有行的添加按钮状态"""
        at_limit = self.v_layout.count() >= self.MAX_ROWS
        for i in range(self.v_layout.count()):
            item = self.v_layout.itemAt(i)
            if item is not None:
                w = item.widget()
                if isinstance(w, FilterRow):
                    w.add_btn.setEnabled(not at_limit)

    @Slot()
    def add_new_row(self):
        if self.v_layout.count() >= self.MAX_ROWS:
            return
        row = FilterRow(self)
        row.addRequested.connect(self.add_new_row)
        row.removeRequested.connect(lambda: self.remove_row(row))
        row.filterChanged.connect(self.conditionsChanged.emit)
        self.v_layout.addWidget(row)
        self._update_add_buttons()
        self.updateGeometry()
        row.value_input.setFocus()

    @Slot()
    def remove_row(self, row):
        """删除过滤行，若只剩一行则清除条件并关闭面板"""
        if self.v_layout.count() <= 1:
            self.clear_conditions()
            self.conditionsChanged.emit()
            self.panelCloseRequested.emit()
            return
        row.filterChanged.disconnect(self.conditionsChanged.emit)
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

    def clear_conditions(self):
        """清除所有过滤条件"""
        for i in range(self.v_layout.count()):
            item = self.v_layout.itemAt(i)
            if item is not None:
                w = item.widget()
                if isinstance(w, FilterRow):
                    w.check_box.setChecked(True)
                    w.field_box.setCurrentIndex(0)
                    w.logic_box.setCurrentIndex(0)
                    w.value_input.clear()
