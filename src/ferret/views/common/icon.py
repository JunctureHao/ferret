from enum import Enum

from qfluentwidgets import Action, FluentIconBase, Theme, getIconColor, qconfig


class BaseIcon(FluentIconBase, Enum):
    LOCATION = "Location"
    LOCATION_TARGET = "Location_target"
    BOOKMARK_ADD = "Bookmark_Add"

    DOCUMENT_SEARCH = "Document_Search"

    def path(self, theme: Theme = Theme.AUTO) -> str:
        return f":/icons/{self.value}_{getIconColor(theme)}.svg"


class BaseAction(Action):
    """继承action 监听了主题当变化时候重绘制action的icon"""

    def __init__(self, icon, text, parent=None, **kwargs):
        super().__init__(icon=icon, text=text, parent=parent, **kwargs)
        self.fluentIcon = icon  # 记录原始的枚举对象
        # 监听主题变化
        qconfig.themeChanged.connect(self._updateIcon)

    def _updateIcon(self):
        if isinstance(self.fluentIcon, FluentIconBase):
            # 重新设置图标，触发重新染色
            self.setIcon(self.fluentIcon)
