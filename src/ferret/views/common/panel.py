from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    Pivot,
    TransparentToolButton,
)


class TabPanel(QWidget):
    """通用 Pivot + StackedWidget 面板"""

    currentChanged = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    # ── 公开接口 ──────────────────────────────

    def addTab(self, route_key: str, widget: QWidget, text: str):
        widget.setObjectName(route_key)
        self.stacked.addWidget(widget)
        self.pivot.addItem(
            routeKey=route_key,
            text=text,
            onClick=lambda _, w=widget: self.stacked.setCurrentWidget(w),
        )
        if self.stacked.count() == 1:
            self.pivot.setCurrentItem(route_key)

    def setTabFontSize(self, size: int):
        self.pivot.setItemFontSize(size)

    def setCurrentTab(self, route_key: str):
        self.pivot.setCurrentItem(route_key)

    # ── 内部方法 ──────────────────────────────

    def __init_widget(self):
        self.pivot = Pivot(self)
        self.stacked = QStackedWidget(self)
        self.close_button = TransparentToolButton(self)
        self.close_button.setIcon(FluentIcon.CLOSE)

    def __init_layout(self):
        self.tab_layout = QHBoxLayout()
        self.tab_layout.setContentsMargins(0, 0, 0, 0)
        self.tab_layout.setSpacing(4)
        self.tab_layout.addWidget(self.pivot, 0)
        self.tab_layout.addStretch(1)

        self._action_layout = QHBoxLayout()
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(4)
        self._action_layout.addWidget(self.close_button)
        self.tab_layout.addLayout(self._action_layout)

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self.v_layout.setSpacing(0)
        self.v_layout.addLayout(self.tab_layout)
        self.v_layout.addWidget(self.stacked)

    def __connect_signal_to_slot(self):
        self.pivot.currentItemChanged.connect(self.__on_pivot_changed)
        self.stacked.currentChanged.connect(self.__on_stacked_changed)

    @Slot(str)
    def __on_pivot_changed(self, route_key: str):
        w = self.stacked.findChild(QWidget, route_key)
        if w:
            self.stacked.setCurrentWidget(w)

    @Slot(int)
    def __on_stacked_changed(self, index: int):
        w = self.stacked.widget(index)
        if w:
            self.pivot.setCurrentItem(w.objectName())
        self.currentChanged.emit(index)
