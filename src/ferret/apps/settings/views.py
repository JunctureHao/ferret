from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
    ComboBoxSettingCard,
    CustomColorSettingCard,
    ExpandLayout,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    OptionsSettingCard,
    ScrollArea,
    SettingCardGroup,
    SmoothMode,
    SwitchSettingCard,
    TitleLabel,
    setTheme,
    setThemeColor,
)

from ferret.core.settings import CONFIG

if TYPE_CHECKING:
    from ferret.apps.window import MainWindow
    from ferret.core.mitm import MitmFacade


class SettingsInterface(ScrollArea):
    def __init__(
        self,
        parent: "MainWindow | None" = None,
        *,
        mitm: "MitmFacade | None" = None,
    ) -> None:
        super().__init__(parent)
        self._mitm = mitm
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self.setting_label = TitleLabel(self)
        self.setting_label.setText(self.tr("设置"))

        # 分组
        self.personalization_group = SettingCardGroup(
            title=self.tr("个性化"), parent=self.scroll_widget
        )

        self.theme_card = OptionsSettingCard(
            configItem=CONFIG.themeMode,
            icon=FluentIcon.BRUSH,
            title=self.tr("应用主题"),
            content=self.tr("自定义应用外观"),
            texts=[self.tr("浅色"), self.tr("深色"), self.tr("使用系统设置")],
            parent=self.personalization_group,
        )
        self.theme_color_card = CustomColorSettingCard(
            configItem=CONFIG.themeColor,
            icon=FluentIcon.PALETTE,
            title=self.tr("主题颜色"),
            content=self.tr("更改应用的主题颜色"),
            parent=self.personalization_group,
        )
        self.zoom_card = OptionsSettingCard(
            configItem=CONFIG.dpi_scale,
            icon=FluentIcon.ZOOM,
            title=self.tr("界面缩放"),
            content=self.tr("调整控件和字体的大小"),
            texts=[
                "100%",
                "125%",
                "150%",
                "175%",
                "200%",
                self.tr("使用系统设置"),
            ],
            parent=self.personalization_group,
        )
        self.language_card = ComboBoxSettingCard(
            configItem=CONFIG.language,
            icon=FluentIcon.LANGUAGE,
            title=self.tr("语言"),
            content=self.tr("选择界面所使用的语言"),
            # 语言名一律用**该语言自己的写法**，所以这两条不进翻译目录 —— 看不懂当前
            # 界面语言的人，也得能在这里认出自己的语言（Windows 设置同样这么做）。
            texts=["简体中文", "English"],
            parent=self.personalization_group,
        )

        # Main Panel
        self.main_panel_group = SettingCardGroup(self.tr("主面板"), self.scroll_widget)
        self.minimize_to_tray_card = SwitchSettingCard(
            FluentIcon.MINIMIZE,
            self.tr("关闭后最小化至托盘"),
            self.tr("应用程序将继续在后台运行"),
            configItem=CONFIG.minimize_to_tray,
            parent=self.main_panel_group,
        )
        self.layout_card = ComboBoxSettingCard(
            configItem=CONFIG.layout,
            icon=FluentIcon.LAYOUT,
            title=self.tr("布局"),
            content=self.tr("切换表格信息中详细面板布局"),
            texts=[self.tr("水平"), self.tr("垂直")],
            parent=self.main_panel_group,
        )
        # 固定会话（plans/sticky-session.md）：全局行为偏好，不是规则 —— 刻意放
        # 设置页主面板而不是重写页。默认关：开着时实时流量表见到的请求头已含
        # 代理补回的 Cookie / Authorization，抓包就不再是「如实转发原件」。
        self.sticky_session_card = SwitchSettingCard(
            FluentIcon.FINGERPRINT,
            self.tr("固定会话"),
            self.tr(
                "固化 Cookie 与认证头：跨连接复用客户端会话不丢；"
                "仅补发给服务器的 Cookie/Auth，不改变服务器行为、不写请求参数"
            ),
            configItem=CONFIG.sticky_session_enabled,
            parent=self.main_panel_group,
        )

        self.__init_widget()

    def __init_widget(self):
        # self.resize(1000, 800)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportMargins(0, 80, 0, 20)
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        self.setSmoothMode(
            SmoothMode.NO_SMOOTH, Qt.Orientation.Vertical
        )  # 关闭平滑滚动，避免晃眼
        self.setObjectName("settingInterface")

        # initialize style sheet
        self.scroll_widget.setObjectName("scrollWidget")
        self.setting_label.setObjectName("settingLabel")

        self.__init_layout()
        self.__connect_signal_to_slot()

    def __init_layout(self):
        self.setting_label.move(36, 30)

        self.personalization_group.addSettingCard(self.theme_card)
        self.personalization_group.addSettingCard(self.theme_color_card)
        self.personalization_group.addSettingCard(self.zoom_card)
        self.personalization_group.addSettingCard(self.language_card)

        self.main_panel_group.addSettingCard(self.minimize_to_tray_card)
        self.main_panel_group.addSettingCard(self.layout_card)
        self.main_panel_group.addSettingCard(self.sticky_session_card)

        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 10, 36, 0)
        self.expand_layout.addWidget(self.personalization_group)
        self.expand_layout.addWidget(self.main_panel_group)

    def __connect_signal_to_slot(self):
        CONFIG.appRestartSig.connect(self.__show_restart_tooltip)
        CONFIG.themeChanged.connect(setTheme)
        CONFIG.themeColorChanged.connect(setThemeColor)
        # 开关翻转时卡片自己会把配置落盘，这里只负责把新值热更进内核。
        # 接 valueChanged 而不是卡片的 checkedChanged：配置项是唯一事实源，
        # 程序化改值（以后若有）也走同一条下发路。
        CONFIG.sticky_session_enabled.valueChanged.connect(
            self.__on_sticky_session_changed
        )

    @Slot(bool)
    def __on_sticky_session_changed(self, enabled: bool) -> None:
        """把固定会话开关热更进内核；失败静默。

        内核没跑时 `set_sticky_session` 只对齐内存副本（下次启动的种子会读到
        它），不会抛错；运行中下发失败（超时等）也不回拨开关 —— 开关已落盘，
        回拨反而让「配置说了什么」和「界面显示什么」分家，重开内核会按落盘值
        重放。
        """
        if self._mitm is None:
            return
        try:
            self._mitm.set_sticky_session(enabled)
        except (ValueError, RuntimeError, TimeoutError):
            pass

    @Slot()
    def __show_restart_tooltip(self):
        """show restart tooltip"""
        InfoBar.warning(
            title="",
            content=self.tr("配置将在重启后生效"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position=InfoBarPosition.BOTTOM,
            duration=3000,
            parent=self.window(),
        )
