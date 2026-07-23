from enum import Enum
from typing import Any

from PySide6.QtCore import QLocale
from qfluentwidgets import (
    BoolValidator,
    ConfigItem,
    ConfigSerializer,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
)

APP_NAME = "Ferret"

CONFIG_NAME = "config.json"


class Language(Enum):
    CHINESE_SIMPLIFIED = QLocale(QLocale.Language.Chinese, QLocale.Country.China)
    ENGLISH = QLocale(QLocale.Language.English, QLocale.Country.UnitedKingdom)


class LanguageSerializer(ConfigSerializer):
    """Language serializer"""

    def serialize(self, value: Language) -> Any:
        return value.value.name()

    def deserialize(self, value: str) -> Language:
        return Language(QLocale(value))


class Layout(Enum):
    HORIZONTAL = "Horizontal"
    VERTICAL = "Vertical"


class LayoutSerializer(ConfigSerializer):
    def serialize(self, value: Layout) -> str:
        return value.value

    def deserialize(self, value: str) -> Layout:
        return Layout(value)


class Config(QConfig):
    dpi_scale = OptionsConfigItem(
        group="MainWindow",
        name="DpiScale",
        default="Auto",
        validator=OptionsValidator([1, 1.25, 1.5, 1.75, 2, "Auto"]),
        restart=True,
    )

    language = OptionsConfigItem(
        group="MainWindow",
        name="Language",
        default=Language.CHINESE_SIMPLIFIED,
        validator=OptionsValidator(Language),
        serializer=LanguageSerializer(),
        restart=True,
    )

    minimize_to_tray = ConfigItem(
        group="MainWindow",
        name="MinimizeToTray",
        default=True,
        validator=BoolValidator(),
    )

    layout = OptionsConfigItem(
        group="MainWindow",
        name="Layout",
        default=Layout.HORIZONTAL,
        validator=OptionsValidator(Layout),
        serializer=LayoutSerializer(),
    )


CONFIG = Config()
