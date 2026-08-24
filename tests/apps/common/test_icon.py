import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QFile, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentIcon, FluentIconBase, Theme

from ferret.apps.common.icon import BaseIcon
from ferret.core import resources_rc  # noqa: F401  注册 :/icons/*.svg


def ink_extent(icon: FluentIconBase, size: int = 96) -> tuple[float, float]:
    """把图标画进一个正方形里，量出实际有墨的那块占多大（横、纵各一个比例）。

    量的是「画面占框」而不是文件里写的 `width`/`height` —— 导航栏对所有图标都用
    同一个 `QRectF(…, 16, 16)`，`QSvgRenderer` 把 viewBox 缩放到这个框，所以视觉
    大小完全由「图形在 viewBox 里占多少」决定，跟标称尺寸无关。
    """
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    icon.render(painter, QRectF(0, 0, size, size))
    painter.end()

    # ARGB32 在小端机上每像素 4 字节、alpha 在第 4 个；逐字节扫比 pixelColor 快得多。
    raw = bytes(image.constBits())
    stride = image.bytesPerLine()
    opaque = {
        (x, y)
        for y in range(size)
        for x in range(size)
        if raw[y * stride + x * 4 + 3] > 20
    }
    xs = [x for x, _ in opaque]
    ys = [y for _, y in opaque]
    return (max(xs) - min(xs) + 1) / size, (max(ys) - min(ys) + 1) / size


class BaseIconTests(unittest.TestCase):
    """每个自定义图标都得在编译好的资源里躺着两份。

    加一个图标要动四处：两个 svg、`BaseIcon` 的枚举、`resources.qrc`，最后
    `pyside6-rcc` 重编 `core/resources_rc.py`。漏掉最后一步不会报错 ——
    `QIcon(":/icons/缺的.svg")` 是个空图标，界面上只是那一格空着。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_every_icon_ships_both_themes(self) -> None:
        for icon in BaseIcon:
            for theme in (Theme.LIGHT, Theme.DARK):
                with self.subTest(icon=icon.value, theme=theme.value):
                    self.assertTrue(QFile.exists(icon.path(theme)), icon.path(theme))

    def test_every_icon_actually_renders(self) -> None:
        """路径在、内容坏（比如 svg 少了 fill）同样是空图标，得画一遍才看得出来。"""
        for icon in BaseIcon:
            with self.subTest(icon=icon.value):
                pixmap = icon.icon(Theme.LIGHT).pixmap(24, 24)
                self.assertFalse(pixmap.isNull())

    def test_the_nav_icon_is_as_big_as_its_neighbours(self) -> None:
        """微软 Fluent 那套 24 网格自带 2px 安全边距，照抄进来就会比邻居小一号。

        导航栏对每个图标都画进同一个 `QRectF(…, 16, 16)`（`NavigationTreeWidget`
        的 `drawIcon`），所以视觉大小只看「图形在 viewBox 里占多少」。qfluentwidgets
        自带的图标是顶到边的，我们的 svg 留着 `viewBox="0 0 24 24"` 就只画到 13px
        上下，并排一眼看得出小 —— 惯例是把 viewBox 收到画面上（`2 2 20 20`）。

        只管导航栏这几个：chevron 之类的内联箭头本来就该在框里偏小，不适用。
        """
        neighbours = (FluentIcon.WIFI, FluentIcon.HISTORY, FluentIcon.SETTING)
        floor = min(max(ink_extent(icon)) for icon in neighbours)
        width, height = ink_extent(BaseIcon.BUG)
        self.assertGreaterEqual(
            max(width, height),
            floor - 0.02,
            f"{BaseIcon.BUG.path(Theme.LIGHT)} 画面只占 {width:.0%}×{height:.0%}，"
            f"邻居是 {floor:.0%}，viewBox 该往画面上收",
        )


if __name__ == "__main__":
    unittest.main()
