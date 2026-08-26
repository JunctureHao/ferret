from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication, QWidget


def center_window(widget: QWidget) -> None:
    """居中窗口"""
    desktop: QRect = QApplication.primaryScreen().availableGeometry()
    w, h = desktop.width(), desktop.height()
    widget.move(w // 2 - widget.width() // 2, h // 2 - widget.height() // 2)
