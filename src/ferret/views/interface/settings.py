from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import QWidget
from qfluentwidgets import (
    ComboBoxSettingCard,
    CustomColorSettingCard,
    ExpandLayout,
    FluentIcon,
    InfoBar,
    OptionsSettingCard,
    ScrollArea,
    SettingCardGroup,
    SmoothMode,
    SwitchSettingCard,
    TitleLabel,
    setTheme,
    setThemeColor,
)

from ferret.config.settings import CONFIG

if TYPE_CHECKING:
    from ferret.views.window import MainWindow


class SettingsInterface(ScrollArea):
    def __init__(self, parent: "MainWindow | None" = None) -> None:
        super().__init__(parent)
        self.scroll_widget = QWidget()
        self.expand_layout = ExpandLayout(self.scroll_widget)

        self.setting_label = TitleLabel(self)
        self.setting_label.setText(self.tr("Settings"))

        # 分组
        self.personalization_group = SettingCardGroup(
            title=self.tr("Personalization"), parent=self.scroll_widget
        )

        self.theme_card = OptionsSettingCard(
            configItem=CONFIG.themeMode,
            icon=FluentIcon.BRUSH,
            title=self.tr("Application theme"),
            content=self.tr("Customize the look of your application"),
            texts=[self.tr("Light"), self.tr("Dark"), self.tr("Use system setting")],
            parent=self.personalization_group,
        )
        self.theme_color_card = CustomColorSettingCard(
            configItem=CONFIG.themeColor,
            icon=FluentIcon.PALETTE,
            title=self.tr("Theme color"),
            content=self.tr("Change the theme color of you application"),
            parent=self.personalization_group,
        )
        self.zoom_card = OptionsSettingCard(
            configItem=CONFIG.dpi_scale,
            icon=FluentIcon.ZOOM,
            title=self.tr("Interface zoom"),
            content=self.tr("Change the size of widgets and fonts"),
            texts=[
                "100%",
                "125%",
                "150%",
                "175%",
                "200%",
                self.tr("Use system setting"),
            ],
            parent=self.personalization_group,
        )
        self.language_card = ComboBoxSettingCard(
            configItem=CONFIG.language,
            icon=FluentIcon.LANGUAGE,
            title=self.tr("Language"),
            content=self.tr("Set your preferred language for UI"),
            texts=["简体中文", "English"],
            parent=self.personalization_group,
        )

        # Main Panel
        self.main_panel_group = SettingCardGroup(
            self.tr("Main Panel"), self.scroll_widget
        )
        self.minimize_to_tray_card = SwitchSettingCard(
            FluentIcon.MINIMIZE,
            self.tr("Minimize to tray after closing"),
            self.tr("application will continue to run in the background"),
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

        self.expand_layout.setSpacing(28)
        self.expand_layout.setContentsMargins(36, 10, 36, 0)
        self.expand_layout.addWidget(self.personalization_group)
        self.expand_layout.addWidget(self.main_panel_group)

    def __connect_signal_to_slot(self):
        CONFIG.appRestartSig.connect(self.__show_restart_tooltip)
        CONFIG.themeChanged.connect(setTheme)
        CONFIG.themeColorChanged.connect(setThemeColor)

    @Slot()
    def __show_restart_tooltip(self):
        """show restart tooltip"""
        InfoBar.warning(
            title="",
            content=self.tr("Configuration takes effect after restart"),
            orient=Qt.Orientation.Horizontal,
            isClosable=True,
            position="BottomCenter",
            duration=3000,
            parent=self.window(),
        )
