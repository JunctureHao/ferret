from PySide6.QtCore import QRectF, Qt, Slot
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QSplitter, QSplitterHandle
from qfluentwidgets import isDarkTheme

from ferret.config.settings import CONFIG, Layout


class BaseHandle(QSplitterHandle):
    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self._is_hovered = False
        self._is_pressed = False

    def enterEvent(self, event):
        self._is_hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._is_hovered = False
        self._is_pressed = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._is_pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._is_pressed = False
        self.update()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        dark = isDarkTheme()

        is_vertical_splitter = self.orientation() == Qt.Orientation.Vertical

        if self._is_pressed:
            alpha, long_side, short_side = 255, 40, 6
        elif self._is_hovered:
            alpha, long_side, short_side = 200, 36, 5
        else:
            alpha, long_side, short_side = 100, 30, 4

        if is_vertical_splitter:
            pw, ph = long_side, short_side
        else:
            pw, ph = short_side, long_side

        base_color = QColor(255, 255, 255) if dark else QColor(0, 0, 0)
        color = QColor(base_color.red(), base_color.green(), base_color.blue(), alpha)

        if self._is_hovered or self._is_pressed:
            bg_color = QColor(255, 255, 255, 20) if dark else QColor(0, 0, 0, 10)
            painter.fillRect(self.rect(), bg_color)

        pill_rect = QRectF((w - pw) / 2, (h - ph) / 2, pw, ph)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        radius = short_side / 2
        painter.drawRoundedRect(pill_rect, radius, radius)


class BaseSplitter(QSplitter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setHandleWidth(12)

    def createHandle(self):
        return BaseHandle(self.orientation(), self)


class OrientationSplitter(BaseSplitter):
    def __init__(self, inverted: bool = False, parent=None):
        super().__init__(parent)
        self.inverted = inverted
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.__apply_layout_config(CONFIG.layout.value)

    def __init_layout(self):
        pass

    def __connect_signal_to_slot(self):
        CONFIG.layout.valueChanged.connect(self.__on_layout_changed)

    @Slot(Layout)
    def __on_layout_changed(self, layout_enum: Layout):
        self.__apply_layout_config(layout_enum)

    def __apply_layout_config(self, layout_enum: Layout):
        """将配置枚举转换为 Qt 的方向，方向切换时保持比例"""
        target_layout = layout_enum
        if self.inverted:
            target_layout = (
                Layout.VERTICAL
                if layout_enum == Layout.HORIZONTAL
                else Layout.HORIZONTAL
            )

        old_sizes = self.sizes()
        old_total = sum(old_sizes)
        new_orientation = (
            Qt.Orientation.Horizontal
            if target_layout == Layout.HORIZONTAL
            else Qt.Orientation.Vertical
        )

        if self.orientation() == new_orientation:
            return

        self.setOrientation(new_orientation)

        if old_total > 0 and len(old_sizes) == self.count():
            if new_orientation == Qt.Orientation.Horizontal:
                new_total = self.width()
            else:
                new_total = self.height()
            if new_total > 0:
                new_sizes = [int(old_sizes[i] / old_total * new_total) for i in range(len(old_sizes))]
                self.setSizes(new_sizes)
