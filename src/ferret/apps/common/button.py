"""带 ToolTipFilter 的透明工具按钮。

filter 只装一次（issues #56）：qfw 的惯例是每次 ``setToolTip`` 都 new 一个
``ToolTipFilter`` 并 ``installEventFilter``，而流量表的排序按钮每刷一次就重设
tooltip —— 同一按钮上 filter 会线性累积（N 次设置 → N 个 filter，一次 Enter
弹 N 个 ToolTip）。filter 工作时按控件读**当前** tooltip 文本，装一个就够。
"""

from __future__ import annotations

from qfluentwidgets import ToolTipFilter, TransparentToolButton


class TransparentTooltipButton(TransparentToolButton):
    _tooltip_filter: ToolTipFilter | None = None

    def setToolTip(self, arg__1: str, /) -> None:
        super().setToolTip(arg__1)
        if self._tooltip_filter is None:
            self._tooltip_filter = ToolTipFilter(self, 300)
            self.installEventFilter(self._tooltip_filter)
