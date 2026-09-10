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

    # 旧版屏蔽规则。网关页取代屏蔽页之后这一项**只剩迁移用途**：
    # apps/gateway 首次启动时把它转成网关规则再清空（见 GatewayController）。
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

    # 抓包通道的启用开关（见 core/mitm/modes.py）。持久化决定的是「点击开始抓包
    # 时开启哪些通道」—— 应用启动本身零抓包动作（内核 regular 空转），所以这里
    # 落盘的是偏好而不是运行态。默认全开：三条通道都要经过一次显式点击才生效。
    system_proxy_enabled = ConfigItem(
        group="Proxy",
        name="SystemProxyEnabled",
        default=True,
        validator=BoolValidator(),
    )

    local_enabled = ConfigItem(
        group="Proxy",
        name="LocalEnabled",
        default=True,
        validator=BoolValidator(),
    )

    # 本地重定向的进程过滤串，语法同上游 `local:` spec（进程名 / PID，逗号分隔，
    # `!` 取反）。留空 = 截全部本机进程；ferret 自身 PID 由上游自动排除。
    local_spec = ConfigItem(
        group="Proxy",
        name="LocalSpec",
        default="",
    )

    wireguard_enabled = ConfigItem(
        group="Proxy",
        name="WireGuardEnabled",
        default=True,
        validator=BoolValidator(),
    )

    # 反向代理通道（.plans/reverse-mode.md）：把 ferret 架在目标服务前面，客户端
    # 直连本监听口即被捕获。意图值落盘、接通位不落盘，与三条既有通道同一语义。
    # 默认关且目标为空——与 local/wireguard 不同，它需要一个显式目标才有意义。
    reverse_enabled = ConfigItem(
        group="Proxy",
        name="ReverseEnabled",
        default=False,
        validator=BoolValidator(),
    )

    # 伪装的目标服务，只接受 http(s)://host[:port]（提交前有前置校验，坏值过
    # 不了对话框；历史落盘坏值由 validate_mode_specs 在开始抓包时兜底拦截）。
    reverse_target = ConfigItem(
        group="Proxy",
        name="ReverseTarget",
        default="",
    )

    # reverse 通道的独立监听端口。必须与 regular 监听端口不同（内核查重键是
    # (host, port, proto)，两者 host 同源，撞端口必被拒），默认 8081 = 8080 + 1。
    # 故意不挂 RangeValidator：手改成字符串的配置会让启动崩（listen_port 同款
    # 决策），收敛交给 core/network.py 的 normalize_listen_port。
    reverse_port = ConfigItem(
        group="Proxy",
        name="ReversePort",
        default=8081,
    )

    # 网关规则，存 list[dict]（见 core/mitm/gateway.py 的 GatewayRule.to_dict）。
    # 和 block_list 同一个坑：QConfig.set 开头 `if item.value == value: return`，
    # 原地 mutate 再 set 会静默不落盘 —— 写回时必须传一个新 list。
    gateway_rules = ConfigItem(
        group="Gateway",
        name="Rules",
        default=[],
    )

    # 网关总开关。关掉之后所有规则一律不判，挂起中的流量立刻放行。
    gateway_enabled = ConfigItem(
        group="Gateway",
        name="Enabled",
        default=True,
        validator=BoolValidator(),
    )

    # 重写规则，存 list[dict]（见 core/mitm/rewrite.py 的 RewriteRule.to_dict）。
    # 和 block_list 同一个坑：QConfig.set 开头 `if item.value == value: return`，
    # 原地 mutate 再 set 会静默不落盘 —— 写回时必须传一个新 list。
    rewrite_rules = ConfigItem(
        group="Rewrite",
        name="Rules",
        default=[],
    )

    # 固定会话总开关（原生 StickyCookie / StickyAuth 两个 addon，见
    # core/mitm/runtime.py）。默认**关**：开启会改写实时抓取所见的请求头（代理侧
    # 补 Cookie / Authorization），与「抓包应如实转发原件」冲突 —— 验证「客户端
    # 到底传不传」时开着它会得到被代理污染的假象。
    sticky_session_enabled = ConfigItem(
        group="Rewrite",
        name="StickySessionEnabled",
        default=False,
        validator=BoolValidator(),
    )

    # 无缓存·明文（原生 anticache + anticomp 两个 addon，见 core/mitm/runtime.py）。
    # 默认关：开着会改写请求头（删条件缓存头 + 改 Accept-Encoding=identity），
    # 与「抓包应如实转发原件」冲突。合成一个开关，两个 option 同开同关。
    anticache_plaintext = ConfigItem(
        group="Rewrite",
        name="AnticachePlaintext",
        default=False,
        validator=BoolValidator(),
    )

    # 断点规则，存 list[dict]（见 core/mitm/intercept.py 的 InterceptRule.to_dict）。
    # 和 block_list 同一个坑：QConfig.set 开头 `if item.value == value: return`，
    # 原地 mutate 再 set 会静默不落盘 —— 写回时必须传一个新 list。
    intercept_rules = ConfigItem(
        group="Intercept",
        name="Rules",
        default=[],
    )

    # 断点总开关。默认**关**：断点会把客户端连接一直钉住等人处理，一启动就生效
    # 等于用户还没看见界面、流量就先卡住了（重写、网关都是无人值守的，断点不是）。
    intercept_enabled = ConfigItem(
        group="Intercept",
        name="Enabled",
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
