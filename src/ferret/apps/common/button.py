from qfluentwidgets import ToolTipFilter, TransparentToolButton


class TransparentTooltipButton(TransparentToolButton):
    def setToolTip(self, arg__1: str, /) -> None:
        super().setToolTip(arg__1)
        self.installEventFilter(ToolTipFilter(self, 300))
