"""flowfilter 搜索动作与错误条（.plans/0-titlebar-search.md §5 v3）。

全局搜索框只有一个（`SearchHost.edit`，见 apps/common/search.py）；捕获页**不换
控件**，而是把两个独特动作（帮助 Flyout / 高亮 toggle）作为 QAction 注入框内
trailing 位，键入文本经 200ms 复位式 debounce 交给本页的 flowfilter 引擎
（"用自己的搜索引擎"，规格 §5.1 v3，2026-09-27 用户修正）。「仅高亮」切换即时
重算不走 debounce。语法错误由 controller 经 `filterExpressionRejected` 回传：
`FlowFilterErrorPanel` 整体替换 flow table（2026-09-27 用户决议，§5.3 v4），
控制器契约不变（非法不上屏、沿用上次有效，见 capture/controllers.py）。

曾经的「添加筛选」token 插入下拉与可折叠高级面板已退役（规格 §5.5/§5.6），
token 知识的唯一来源是帮助 Flyout。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    Flyout,
    FlyoutAnimationType,
    FlyoutView,
    IconWidget,
    PushButton,
    qconfig,
)


class CaptureFilterActions(QObject):
    """捕获页注入全局搜索框的独特动作 + flowfilter 应用管线（规格 §5.1 v3）。

    - 帮助：弹出 flowfilter 语法 Flyout（token 知识唯一来源）；锚点经 `bind_anchor`
      绑到全局框（MainWindow 牵线）。
    - 高亮：筛选/高亮二态 toggle，切换即时重算不走 debounce。
    - 文本：`feed()` 进 200ms 复位式 debounce，到点发 `conditionsChanged`
      （`CapturesInterface` 据此走 apply_filter / apply_highlight 两条路径）。
    """

    conditionsChanged = Signal()  # 表达式变了 / 高亮态翻了，请重算过滤或高亮

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._anchor: QWidget | None = None
        # 200ms 复位式 debounce：每个键入都重启，避免每按一键就编译一次表达式。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._on_condition_changed)

        self.help_action = QAction(
            FluentIcon.HELP.icon(), self.tr("flowfilter 语法帮助"), self
        )
        self.help_action.setToolTip(self.tr("flowfilter 语法帮助"))
        self.help_action.triggered.connect(self._show_syntax_help)

        self.highlight_action = QAction(
            FluentIcon.FILTER.icon(), self.tr("过滤模式"), self
        )
        self.highlight_action.setCheckable(True)
        self.highlight_action.setToolTip(
            self.tr("过滤模式：隐藏不匹配的流量，点击切换为仅高亮")
        )
        self.highlight_action.toggled.connect(self._on_highlight_toggled)
        # 主题切换后重上色（同 SearchHost 的 leading 图标）。
        qconfig.themeChangedFinished.connect(self._refresh_theme_icons)

    @property
    def actions(self) -> list[QAction]:
        """注入全局框 trailing 位的动作（顺序即排布序，内嵌 clear 恒最右）。"""
        return [self.help_action, self.highlight_action]

    def bind_anchor(self, anchor: QWidget) -> None:
        """绑定帮助 Flyout 的锚点（= 全局搜索框；MainWindow 牵线）。"""
        self._anchor = anchor

    def feed(self, text: str) -> None:
        """路由层键入转发入口：记下文本并重启 debounce。"""
        self._text = text
        self._debounce.start()

    def raw_text(self) -> str:
        """当前键入的原始文本（未 strip；校验在 controller）。"""
        return self._text

    def is_highlight_mode(self) -> bool:
        """开了「仅高亮」→ 表达式当高亮用（不隐藏行）；否则当过滤用。"""
        return self.highlight_action.isChecked()

    def has_active_filter(self) -> bool:
        return bool(self.raw_text().strip())

    def _sync_highlight_icon(self) -> None:
        """按高亮态同步 toggle 的图标与 tooltip（toggle 与主题刷新共用）。"""
        checked = self.highlight_action.isChecked()
        icon = FluentIcon.BRUSH if checked else FluentIcon.FILTER
        self.highlight_action.setIcon(icon.icon())
        self.highlight_action.setToolTip(
            self.tr("仅高亮模式：命中行整行染色，不隐藏；点击切回过滤")
            if checked
            else self.tr("过滤模式：隐藏不匹配的流量，点击切换为仅高亮")
        )

    @Slot(bool)
    def _on_highlight_toggled(self, checked: bool) -> None:
        self._sync_highlight_icon()
        # 过滤↔高亮是两条不同下发路径，切换立即重算（不走 200ms debounce）。
        self._on_condition_changed()

    def _refresh_theme_icons(self) -> None:
        """主题切换完成后重上色（FluentIconEngine 在 icon() 时烘焙颜色）。"""
        self.help_action.setIcon(FluentIcon.HELP.icon())
        self._sync_highlight_icon()

    @Slot()
    def _on_condition_changed(self) -> None:
        self.conditionsChanged.emit()

    def _show_syntax_help(self) -> None:
        """弹出 flowfilter 操作符速查表（「添加筛选」退役后 token 知识唯一来源）。"""
        if self._anchor is None:  # 未牵线时静默不弹（不该发生，防御）
            return
        view = FlyoutView(
            title=self.tr("flowfilter 语法"),
            content=self.tr(
                "~u <正则>     URL（含 scheme/端口/查询串）\n"
                "~d <正则>     域名（不含端口）\n"
                "~m <正则>     请求方法，如 ~m GET\n"
                "~c <整数>     状态码，只认精确码，如 ~c 200 / ~c 404\n"
                "~h <正则>     请求或响应头\n"
                "~b <正则>     正文\n"
                "~t <正则>     内容类型\n"
                "~q / ~s        请求期 / 响应期\n"
                "~websocket    WebSocket 流量\n"
                "~marked        已标记流量\n"
                "\n"
                "组合：a & b（与） a | b（或） !a（非） ( )（分组）\n"
                '带空格或括号的值要加引号：~u "api/.*"'
            ),
            isClosable=True,
        )
        flyout = Flyout.make(
            view, self._anchor, self._anchor.window(), aniType=FlyoutAnimationType.PULL_UP
        )
        # qfw 的 × 只 emit `closed` 信号（`Flyout.create` 才会代连 close）；
        # `Flyout.make` 不接线，必须自己连，否则点 × 关不掉（实测 bug）。
        view.closed.connect(flyout.close)


class FlowFilterErrorPanel(QWidget):
    """flowfilter 语法错误全量替换面板（规格 §5.3 v4，2026-09-27 用户决议）。

    表达式非法时整体替换 flow table 面板（用户实机决议：表格上方小字太不显眼）；
    控制器契约不变（非法不上屏、沿用上次有效，见 capture/controllers.py）——
    面板是纯显示态，修正或清除表达式后表格自动恢复。错误消息是 parse 原文
    （任意英文），直显不译。
    """

    clearRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.icon = IconWidget(FluentIcon.INFO, self)
        self.icon.setFixedSize(32, 32)
        self.title = BodyLabel(self.tr("过滤表达式语法错误"), self)
        self.message = BodyLabel(self)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.hint = CaptionLabel(
            self.tr("捕获表格已让位；修正或清除表达式后自动恢复"), self
        )
        self.clear_btn = PushButton(FluentIcon.DELETE, self.tr("清除表达式"), self)
        self.clear_btn.clicked.connect(self._emit_clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.addStretch(1)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(8)
        layout.addWidget(self.title, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(6)
        layout.addWidget(self.message, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(4)
        layout.addWidget(self.hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(12)
        layout.addWidget(self.clear_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

    def set_raw_error(self, message: str) -> None:
        """填充错误信息（空串 = 清空待恢复；显隐由宿主的内容堆栈切换）。"""
        self.message.setText(message)

    @Slot()
    def _emit_clear(self) -> None:
        self.clearRequested.emit()
