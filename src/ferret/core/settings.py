from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLocale, QStandardPaths
from qfluentwidgets import (
    BoolValidator,
    ConfigItem,
    ConfigSerializer,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
)

from ferret.core.network import DEFAULT_PORT, LISTEN_HOSTS, LOOPBACK_HOST

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

    # 屏蔽规则，存 list[dict]（见 core/mitm/blocklist.py 的 BlockRule.to_dict）。
    # 注意 QConfig.set 开头有 `if item.value == value: return`，原地 mutate 再 set
    # 会静默不落盘 —— 写回时必须传一个新 list。
    block_list = ConfigItem(
        group="Proxy",
        name="BlockList",
        default=[],
    )

    # 绑定地址：只有环回和 0.0.0.0 两个合法值（见 core/network.py）。
    # LISTEN_HOSTS 把环回排在首位，所以配置被手改成别的值时，
    # OptionsValidator.correct 会退回环回 —— 出错方向永远偏安全。
    listen_host = OptionsConfigItem(
        group="Proxy",
        name="ListenHost",
        default=LOOPBACK_HOST,
        validator=OptionsValidator(list(LISTEN_HOSTS)),
    )

    # 故意不挂 RangeValidator：它的 correct 是 `min(max(lo, v), hi)`，遇到手改成
    # 字符串的配置会抛 TypeError，而 QConfig.load 不 catch —— 启动就崩。
    # 端口的收敛统一交给 core/network.py 的 normalize_listen_port。
    listen_port = ConfigItem(
        group="Proxy",
        name="ListenPort",
        default=DEFAULT_PORT,
    )

    # 原生 Block addon 的来源过滤（mitmproxy/addons/block.py），按**来源 IP 类别**
    # 拒连；默认沿用 mitmproxy 出厂姿态：拒公网、放局域网。环回恒放行且不可配。
    block_global = ConfigItem(
        group="Proxy",
        name="BlockGlobal",
        default=True,
        validator=BoolValidator(),
    )

    block_private = ConfigItem(
        group="Proxy",
        name="BlockPrivate",
        default=False,
        validator=BoolValidator(),
    )


def get_config_dir() -> Path:
    d = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_file() -> Path:
    return get_config_dir() / CONFIG_NAME


def get_certs_dir() -> Path:
    return get_config_dir() / "certs"


def get_sessions_dir() -> Path:
    directory = get_config_dir() / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


CONFIG = Config()
