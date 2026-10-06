"""懒构造子页的占位容器。

FluentWindow 的每个子页都是一整套 qfw 控件树（几十 MB 量级），而多数页在首次切
到之前完全不可见。LazyPage 用空壳占住 stackedWidget 与导航路由键（objectName 不
变，`addSubInterface` / `switchTo` 语义不变），首次 `ensure()` 才经工厂构造真实页
并嵌进布局——`stackedWidget.widget()` 恒返回占位容器，窗口级路由只见到它。

属性访问整体委托真实页、缺失照常 AttributeError：`is_searchable` 的鸭子判定依赖
这一点，不实现搜索协议的页（证书 / Compose）不会被误判成可搜索。ensure 前委托
属性同样 AttributeError——彼时页必然不可见，调用方都先经 ensure（切页钩子）。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QVBoxLayout, QWidget


class LazyPage(QWidget):
    def __init__(
        self,
        factory: Callable[[], QWidget],
        object_name: str,
        on_ensure: Callable[[QWidget], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 路由键由占位容器接管：qfw 导航项与 qrouter 都认这个 objectName。
        self.setObjectName(object_name)
        self._factory = factory
        self._on_ensure = on_ensure
        self._page: QWidget | None = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def ensure(self) -> QWidget:
        """返回真实页；首次调用才构造，构造后触发一次 on_ensure 供调用方补牵线。"""
        if self._page is None:
            page = self._factory()
            self._page = page
            self._layout.addWidget(page)
            if self._on_ensure is not None:
                self._on_ensure(page)
        return self._page

    def __getattr__(self, name: str):
        # 只在常规查找失败时进入；_page 从 __dict__ 取，避免 __getattr__ 自递归。
        page = self.__dict__.get("_page")
        if page is None:
            raise AttributeError(
                f"{type(self).__name__}({self.objectName()!r}) real page not "
                f"constructed yet; missing attribute {name!r}"
            )
        try:
            return getattr(page, name)
        except AttributeError as exc:
            raise AttributeError(
                f"delegated page {type(page).__name__!r} has no attribute {name!r}"
            ) from exc
