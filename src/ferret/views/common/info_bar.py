"""自定义 InfoBar 位置管理器 - 底部中央显示"""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBar, InfoBarManager, InfoBarPosition


def show_success(title: str, content: str, parent=None):
    InfoBar.info(
        title=title,
        content=content,
        orient=Qt.Orientation.Horizontal,
        isClosable=True,
        position=InfoBarPosition.BOTTOM,
        duration=2000,
        parent=parent,
    )


def show_warning(title: str, content: str, parent=None):
    InfoBar.warning(
        title=title,
        content=content,
        orient=Qt.Orientation.Horizontal,
        isClosable=True,
        position=InfoBarPosition.TOP,
        duration=2000,
        parent=parent,
    )


def show_error(title: str, content: str, parent=None):
    InfoBar.error(
        title=title,
        content=content,
        orient=Qt.Orientation.Horizontal,
        isClosable=True,
        position=InfoBarPosition.TOP,
        duration=3000,
        parent=parent,
    )


@InfoBarManager.register("BottomCenter")
class BottomCenterInfoBarManager(InfoBarManager):
    """自定义底部中央 InfoBar 管理器"""

    def _pos(self, infoBar: InfoBar, parentSize=None):
        p = infoBar.parent()
        if not isinstance(p, QWidget):
            return QPoint(0, 0)
        parentSize = parentSize or p.size()

        # 水平居中
        x = (parentSize.width() - infoBar.width()) // 2
        # 垂直方向：底部留出 20 像素边距
        y = parentSize.height() - infoBar.height() - 20

        # 计算当前 infoBar 的索引位置，实现多条信息堆叠
        index = self.infoBars[p].index(infoBar)
        for bar in self.infoBars[p][0:index]:
            y -= bar.height() + self.spacing

        return QPoint(x, y)

    def _slideStartPos(self, infoBar: InfoBar):
        # 动画起始位置：从上方 16 像素处滑入
        pos = self._pos(infoBar)
        return QPoint(pos.x(), pos.y() - 16)
