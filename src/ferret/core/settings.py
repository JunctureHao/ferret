from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from copy import deepcopy
from enum import Enum
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication, QLocale, QStandardPaths, QTimer
from qfluentwidgets import (
    ConfigItem,
    ConfigSerializer,
    ConfigValidator,
    EnumSerializer,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    Theme,
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


class _BoolValidator(ConfigValidator):
    """非 bool 一律修回构造时声明的默认值。

    qfw 的 ``BoolValidator`` 是 ``OptionsValidator([True, False])``，`correct`
    把不在 options 里的值修成 ``options[0]`` —— 恰好是 True：JSON 里的 null、
    ``"false"``、0 加载时都会把默认关闭的开关顶开（ssl_insecure / scripts_enabled
    首当其冲，issues #88）。这里收紧：合法值原样通过，坏值回各项默认。
    """

    def __init__(self, default: bool) -> None:
        self.default = default

    def validate(self, value) -> bool:
        return isinstance(value, bool)

    def correct(self, value) -> bool:
        return value if isinstance(value, bool) else self.default


class BoolConfigItem(ConfigItem):
    """布尔配置项：加载坏值回落**本项默认**，而不是 qfw 的「非法值恒变 True」。

    用自定义 validator 而不是改 qfw 产物：qfw 包不动，校验挂钩在
    `ConfigItem.value` setter 上，``qconfig.load`` → ``deserializeFrom`` →
    setter 一路都会过它，加载与手改两条路一次收口。
    """

    def __init__(self, group: str, name: str, default: bool, restart: bool = False):
        super().__init__(
            group, name, default, validator=_BoolValidator(default), restart=restart
        )


class _StringListSerializer(ConfigSerializer):
    """加载边界拒绝错误结构，让 Config.load 恢复本项默认并记录警告。"""

    def deserialize(self, value: Any) -> list[str]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise TypeError("Expected a list of strings")
        return list(value)


class Config(QConfig):
    def __init__(self) -> None:
        super().__init__()
        self.load_warnings: list[tuple[str, str]] = []
        self._save_timer: QTimer | None = None
        self._save_pending = False

    def defer_save(self) -> None:
        """连续拖列宽等操作只在稳定 300 ms 后落盘，退出时可同步冲刷。"""
        self._save_pending = True
        app = QCoreApplication.instance()
        if app is None:
            return
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(300)
            self._save_timer.timeout.connect(self.save)
            app.aboutToQuit.connect(self.flush_pending_save)
        self._save_timer.start()

    def flush_pending_save(self) -> None:
        # A single-shot timer is already inactive while its callback runs. Keep
        # the obligation separately so failed writes can be retried on exit.
        if self._save_pending:
            self.save()

    def recovery_messages(self) -> list[str]:
        """翻译器装好后再格式化启动诊断。"""
        messages = []
        for kind, detail in self.load_warnings:
            if kind == "recovered":
                message = QCoreApplication.translate(
                    "Config", "配置文件损坏，已从备份恢复：{}"
                )
            elif kind == "backup":
                message = QCoreApplication.translate(
                    "Config",
                    "无法修复配置文件，已使用备份内容继续运行；原文件将保留：{}",
                )
            elif kind == "field":
                message = QCoreApplication.translate(
                    "Config", "配置项 {} 无效，已使用默认值。"
                )
            else:
                message = QCoreApplication.translate(
                    "Config", "无法读取配置，已使用默认值；原文件将保留：{}"
                )
            messages.append(message.format(detail))
        return messages

    @staticmethod
    def _read(path: Path) -> dict:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be an object")  # noqa: TRY004
        return data

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        """同目录暂存并替换；写入或替换失败时保留原文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=4)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _preserve_corrupt(self, path: Path) -> None:
        if path.exists():
            shutil.copy2(
                path, path.with_name(f"{path.name}.corrupt-{uuid.uuid4().hex}")
            )

    def save(self) -> None:
        self._save_pending = True
        if self._save_timer is not None:
            self._save_timer.stop()
        path = self.file
        if path.exists():
            try:
                previous = self._read(path)
            except (ValueError, UnicodeError):
                # 没有有效备份时允许使用默认配置，但下次保存仍须留下坏文件供恢复。
                self._preserve_corrupt(path)
            else:
                self._write(path.with_suffix(path.suffix + ".bak"), previous)
        self._write(path, self.toDict())
        self._save_pending = False

    def load(self, file=None, config=None) -> None:
        if config is not None and config is not self:
            raise ValueError("Load configuration on its owning Config instance")
        if file is not None:
            self.file = Path(file)
        if self._save_timer is not None:
            self._save_timer.stop()
        self._save_pending = False
        self.load_warnings.clear()
        try:
            data = self._read(self.file)
        except (OSError, ValueError, UnicodeError) as exc:
            try:
                data = self._read(self.file.with_suffix(self.file.suffix + ".bak"))
            except (OSError, ValueError, UnicodeError) as backup_error:
                data = {}
                # A first launch with neither file present is not corruption.
                kind = (
                    ""
                    if isinstance(exc, FileNotFoundError)
                    and isinstance(backup_error, FileNotFoundError)
                    else "defaults"
                )
            else:
                try:
                    self._preserve_corrupt(self.file)
                    self._write(self.file, data)
                except OSError:
                    # Reading a usable backup and repairing the primary are
                    # separate steps. Read-only storage must not discard the
                    # recovered preferences for this run.
                    kind = "backup"
                else:
                    kind = "recovered"
            if kind:
                self.load_warnings.append((kind, str(self.file)))
                logging.getLogger("ferret.settings").warning(
                    "Configuration %s: %s (%s)", kind, self.file, exc
                )
        for name in dir(type(self)):
            item = getattr(type(self), name)
            if not isinstance(item, ConfigItem):
                continue
            group = data.get(item.group, {})
            if not isinstance(group, dict) or item.name not in group:
                item.value = deepcopy(item.defaultValue)
                continue
            value = group[item.name]
            try:
                item.deserializeFrom(value)
            except (ValueError, TypeError, KeyError):
                item.value = deepcopy(item.defaultValue)
                self.load_warnings.append(("field", item.key))
                logging.getLogger("ferret.settings").warning(
                    "Invalid configuration field: %s", item.key
                )
        self.theme = self.get(self.themeMode)

    def reset_to_defaults(self) -> None:
        """全部配置项恢复出厂默认并立即落盘（设置页「重置所有设置」入口）。

        逐项走 ``set`` 而不是整盘覆写：值真正变化的项会发 ``valueChanged``，
        已挂接的热更链路（固定会话 / 无缓存 / 协议层 / DNS hosts 等）随之自动重推，
        绑卡片的控件也自行刷新；``language`` / ``dpi_scale`` 带 restart 标记，
        ``appRestartSig`` 会提示重启。列表 / dict 默认值必须 deepcopy —— 共享同一
        可变默认值会让「原地 mutate 再 set」静默不落盘的坑复发。
        """
        for name in dir(type(self)):
            item = getattr(type(self), name)
            if not isinstance(item, ConfigItem):
                continue
            self.set(item, deepcopy(item.defaultValue), save=False)
        self.save()

    # 应用主题：覆盖 qfluentwidgets 基类的出厂默认（Theme.LIGHT），改为跟随系统。
    # 键名 group/name 必须与基类一致（QFluentWidgets/ThemeMode），否则落盘与
    # 框架读取对不上。
    themeMode = OptionsConfigItem(
        group="QFluentWidgets",
        name="ThemeMode",
        default=Theme.AUTO,
        validator=OptionsValidator(Theme),
        serializer=EnumSerializer(Theme),
    )

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

    minimize_to_tray = BoolConfigItem(
        group="MainWindow",
        name="MinimizeToTray",
        default=False,
    )

    layout = OptionsConfigItem(
        group="MainWindow",
        name="Layout",
        default=Layout.VERTICAL,
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
    # 拒连。默认**不拒公网**：出厂即拦公网会让用户第一次抓外网就莫名连不上，先放行、
    # 让用户按需打开更符合直觉（环回恒放行且不可配）。
    block_global = BoolConfigItem(
        group="Proxy",
        name="BlockGlobal",
        default=False,
    )

    block_private = BoolConfigItem(
        group="Proxy",
        name="BlockPrivate",
        default=False,
    )

    # 代理认证（原生 ProxyAuth addon，见 docs/design.md#auth）：和上面两个开关同属
    # 「谁能用这个代理」。block_* 按来源 IP 类别一刀切，挡不住「要放行手机、但不想
    # 放行同网段陌生人」这种需求 —— 那正是这里补的洞。
    # 只对 regular / upstream 通道有效；local / wireguard / reverse 接通期间自动让路
    # （MitmRuntime._effective_proxyauth），意图值照常保留。
    proxyauth_enabled = BoolConfigItem(
        group="Proxy",
        name="ProxyAuthEnabled",
        default=False,
    )

    # 与 upstream_username/password 拆两项存的理由相同，但**约束更严**：原生
    # SingleUser 用 `split(":")` 要求恰好两段（addons/proxyauth.py:192-197），客户端
    # 侧 parse_http_basic_auth 同样只切两段 —— 所以用户名和密码**都**不能含冒号。
    # 对话框提交前前置拒绝，_effective_proxyauth 里另有一道兜底闸门（防手改配置）。
    # 密码可为空（原生允许 "alice:"）。**明文落盘**，与 upstream_password 及 confdir
    # 里的 CA 私钥同一安全姿态；不愿落盘的不要启用。
    proxyauth_username = ConfigItem(
        group="Proxy",
        name="ProxyAuthUsername",
        default="",
    )

    proxyauth_password = ConfigItem(
        group="Proxy",
        name="ProxyAuthPassword",
        default="",
    )

    # 抓包通道的启用开关（见 core/mitm/modes.py）。持久化决定的是「点击开始抓包
    # 时开启哪些通道」—— 应用启动本身零抓包动作（内核 regular 空转），所以这里
    # 落盘的是偏好而不是运行态。默认只开系统代理：本地重定向要提权装驱动、
    # WireGuard 要开 UDP 端口，都不该在用户没点之前替他决定。
    system_proxy_enabled = BoolConfigItem(
        group="Proxy",
        name="SystemProxyEnabled",
        default=True,
    )

    local_enabled = BoolConfigItem(
        group="Proxy",
        name="LocalEnabled",
        default=False,
    )

    # 本地重定向的进程过滤串，语法同上游 `local:` spec（进程名 / PID，逗号分隔，
    # `!` 取反）。留空 = 截全部本机进程；ferret 自身 PID 由上游自动排除。
    local_spec = ConfigItem(
        group="Proxy",
        name="LocalSpec",
        default="",
    )

    wireguard_enabled = BoolConfigItem(
        group="Proxy",
        name="WireGuardEnabled",
        default=False,
    )

    # 反向代理通道（docs/design.md#capture）：把 ferret 架在目标服务前面，客户端
    # 直连本监听口即被捕获。意图值落盘、接通位不落盘，与三条既有通道同一语义。
    # 默认关且目标为空——与 local/wireguard 不同，它需要一个显式目标才有意义。
    reverse_enabled = BoolConfigItem(
        group="Proxy",
        name="ReverseEnabled",
        default=False,
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

    # SOCKS5 入站通道（docs/design.md#capture）：独立端口的 SOCKS5 代理，给只认
    # SOCKS5 的客户端（移动端 App、部分 CLI）接入。意图值落盘、接通位不落盘，与
    # 四通道同一语义。默认关。监听地址跟随全局 listen_host（D2）。
    socks5_enabled = BoolConfigItem(
        group="Proxy",
        name="Socks5Enabled",
        default=False,
    )

    # SOCKS5 通道的独立监听端口，默认 1080（SOCKS5 惯例，D1）。与 reverse_port
    # 同款决策：不挂 RangeValidator，收敛交给 normalize_listen_port。
    socks5_port = ConfigItem(
        group="Proxy",
        name="Socks5Port",
        default=1080,
    )

    # 上游代理出口：**不是第五条通道**，而是把 mode 列表第一个槽位从 regular 换成
    # upstream（见 core/mitm/modes.py::upstream_mode_spec）—— 监听地址端口一字不动，
    # 只把系统代理这条通道的出口从直连改成「先交给上游代理」。企业强制代理、链式
    # 抓包（ferret → Burp/Charles）、出口 IP 池都靠它。意图值落盘、接通位不落盘，
    # 与四条通道同一语义。默认关且目标为空——它需要一个显式目标才有意义。
    upstream_enabled = BoolConfigItem(
        group="Proxy",
        name="UpstreamEnabled",
        default=False,
    )

    # 上游代理地址，只接受 host[:port] 或 http(s)://host[:port]，**不能带
    # user:pass@**（原生 server_spec 的 host 段是 `[^:/]+`，解析不了；见
    # core/mitm/modes.py::upstream_mode_spec）。省略 scheme 按 http、省略端口按
    # scheme 兜底（80/443）。提交前有前置校验（坏值 + 自环），历史落盘坏值由
    # validate_mode_specs 在开始抓包时兜底拦截。
    upstream_target = ConfigItem(
        group="Proxy",
        name="UpstreamTarget",
        default="",
    )

    # 上游代理的 Basic 凭证，拆两项存：原生格式是 "user:pass"，服务端按首个冒号
    # 切，所以密码含冒号没问题、用户名含冒号无法表达 —— 由 UI 分开收，避免让用户
    # 自己拼出一个歧义串。留空 = 不发认证头（见 MitmRuntime._upstream_auth）。
    # **明文落盘**，与 confdir 里的 CA 私钥同一安全姿态；不愿落盘的留空即可。
    upstream_username = ConfigItem(
        group="Proxy",
        name="UpstreamUsername",
        default="",
    )

    upstream_password = ConfigItem(
        group="Proxy",
        name="UpstreamPassword",
        default="",
    )

    # DNS 解析两选项（docs/design.md#capture）：原生 DnsResolver addon。仅对隧道内
    # DNS 生效（WireGuard 通道的 10.0.0.53）；regular 模式下客户端自解 DNS、选项
    # 管不到 —— 不产生 DNSFlow 就没有任何代码路径碰到它，故不设让路、常驻种子。
    # 自定义服务器列表，留空 = 跟随系统 DNS（原生默认 []）。和 gateway_rules 同
    # 一个坑：QConfig.set 开头 `if item.value == value: return`，写回必须传新 list。
    dns_name_servers = ConfigItem(
        group="Proxy",
        name="DnsNameServers",
        default=[],
        serializer=_StringListSerializer(),
    )

    # 解析时查操作系统 hosts 文件（原生默认 True，开关方向不反转 —— 呈现语义
    # 就是「解析时查 hosts」）。
    dns_use_hosts_file = BoolConfigItem(
        group="Proxy",
        name="DnsUseHostsFile",
        default=True,
    )

    # 上游 TLS 信任三选项（docs/design.md#tls）：原生 tlsconfig 的 ssl_* 与
    # add_upstream_certs_to_client_chain，四条通道共用一条 tls_start_server，
    # 语义天然一致 —— 全局偏好，不设让路、不随通道回滚。
    # 不校验上游服务器证书（原生默认 False）。顺带打开不安全重协商
    # （tlsconfig.py 把它同时喂给 legacy_server_connect），文案已写明。
    ssl_insecure = BoolConfigItem(
        group="Proxy",
        name="SslInsecure",
        default=False,
    )

    # 额外信任的 CA 证书文件路径（.pem / .crt / .cer），留空 = 只认公共根。
    # 只存**用户给的路径**：合并产物（公共根 + 用户根）是生成物，每次现算、
    # 永不落盘（换机换目录后落盘路径就是死路径，见 core/mitm/certificate.py
    # 的 build_trusted_ca_bundle）。和 gateway_rules 同一个坑：QConfig.set 开头
    # `if item.value == value: return`，写回必须传新 list。
    ssl_trusted_ca_files = ConfigItem(
        group="Proxy",
        name="SslTrustedCaFiles",
        default=[],
        serializer=_StringListSerializer(),
    )

    # 向客户端拼接上游真实证书链（原生默认 False）。给做了证书锁定的 App 用，
    # 与前两项正交 —— 下发时必须一并带上 upstream_cert=True，否则原生
    # Core.configure 抛 OptionsError（见 core/mitm/runtime.py::ssl_option_updates）。
    add_upstream_certs_to_client_chain = BoolConfigItem(
        group="Proxy",
        name="UpstreamCertsToClientChain",
        default=False,
    )

    # mTLS 客户端证书路径（docs/design.md#tls）：空串 = 未启用。
    # 存**一个** str 而不是列表 —— 原生 client_certs 就是一个路径，指到目录时按
    # SNI 找 `<主机名>.pem`（精确匹配、无通配、无兜底），多主机证书全在那个目录里，
    # Ferret 不替它记账。与上面三项同属「四条通道共用一条 tls_start_server」的全局
    # 偏好。下发前必须把空串归一成 None（见 runtime.py::client_certs_option_updates）。
    # 存用户给的原样路径（可以带 `~`）：原生 addons/core.py 与 tlsconfig.py 两处都
    # 自己 expanduser，我们展开了反而让 CONFIG 与 options 对不上。
    client_certs_path = ConfigItem(
        group="Proxy",
        name="ClientCertsPath",
        default="",
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
    # 默认**关**：各功能一律默认不启用，由用户在界面显式打开。
    gateway_enabled = BoolConfigItem(
        group="Gateway",
        name="Enabled",
        default=False,
    )

    # 重写规则，存 list[dict]（见 core/mitm/rewrite.py 的 RewriteRule.to_dict）。
    # 和 block_list 同一个坑：QConfig.set 开头 `if item.value == value: return`，
    # 原地 mutate 再 set 会静默不落盘 —— 写回时必须传一个新 list。
    rewrite_rules = ConfigItem(
        group="Rewrite",
        name="Rules",
        default=[],
    )

    # 重写总开关。关掉之后所有重写规则一律不生效，流量原样转发。默认**开**：
    # 规则表出厂为空、零副作用，开与不开等价；落盘是为了与网关/断点/脚本/Mock
    # 四个总开关同一口径 —— 重启后保持用户上次的开关状态。
    rewrite_enabled = BoolConfigItem(
        group="Rewrite",
        name="Enabled",
        default=True,
    )

    # 用户脚本清单，存 list[dict]（见 core/mitm/scripts.py 的 ScriptEntry.to_dict）。
    # 和 rewrite_rules 同一个坑：QConfig.set 开头 `if item.value == value: return`，
    # 原地 mutate 再 set 会静默不落盘 —— 写回时必须传一个新 list。
    scripts = ConfigItem(
        group="Scripts",
        name="Scripts",
        default=[],
    )

    # 脚本总开关。关掉之后所有脚本一律不装载、不参与流量处理。默认**关**：脚本以
    # 应用同等权限执行，一启动就全跑起来风险太大，由用户显式打开（各功能一律默认
    # 不启用，与网关/断点同一姿态）。
    scripts_enabled = BoolConfigItem(
        group="Scripts",
        name="Enabled",
        default=False,
    )

    # 固定会话总开关（原生 StickyCookie / StickyAuth 两个 addon，见
    # core/mitm/runtime.py）。默认**关**：开启会改写实时抓取所见的请求头（代理侧
    # 补 Cookie / Authorization），与「抓包应如实转发原件」冲突 —— 验证「客户端
    # 到底传不传」时开着它会得到被代理污染的假象。
    sticky_session_enabled = BoolConfigItem(
        group="Rewrite",
        name="StickySessionEnabled",
        default=False,
    )

    # 无缓存·明文（原生 anticache + anticomp 两个 addon，见 core/mitm/runtime.py）。
    # 默认关：开着会改写请求头（删条件缓存头 + 改 Accept-Encoding=identity），
    # 与「抓包应如实转发原件」冲突。合成一个开关，两个 option 同开同关。
    anticache_plaintext = BoolConfigItem(
        group="Rewrite",
        name="AnticachePlaintext",
        default=False,
    )

    # 协议层两开关（docs/design.md#capture）：原生 http2 / http3 布尔选项。
    # 默认**开**（对齐原生出厂）：全支持是正常姿态；关掉是调试降级手段（h2 →
    # HTTP/1.1 行式可读、h3 → 客户端回落 TCP 解决 QUIC/UDP 抓不到），不是「如实
    # 转发」问题，故默认值方向与 sticky / anticache 相反。开关方向不反转（呈现
    # 语义 = 落盘语义 = 「启用」），与 dns_use_hosts_file 同款理由。
    http2_enabled = BoolConfigItem(
        group="Proxy",
        name="Http2Enabled",
        default=True,
    )

    http3_enabled = BoolConfigItem(
        group="Proxy",
        name="Http3Enabled",
        default=True,
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
    intercept_enabled = BoolConfigItem(
        group="Intercept",
        name="Enabled",
        default=False,
    )

    # mock 响应池（docs/design.md#mock）：原生 ServerPlayback 的旋钮。
    # 池内容不落 config.json（见 core/mitm/facade.py 的 mock 池托管文件），这里只
    # 存开关与匹配行为。默认全按「GUI mock 语义」取值，与原生出厂值两处刻意不同：
    # reuse=True（原生默认 False 是消耗式，池耗尽后未命中策略跟着失效、流量静默
    # 直连，GUI 用户不可感知）；extra="forward" 保持原生默认。
    mock_enabled = BoolConfigItem(
        group="Mock",
        name="Enabled",
        default=False,
    )

    # 未命中策略（原生 server_replay_extra 的 choices，多一个不许少一个不行 ——
    # OptionsConfigItem 手改坏值会 correct 回默认）。
    mock_extra = OptionsConfigItem(
        group="Mock",
        name="Extra",
        default="forward",
        validator=OptionsValidator(["forward", "kill", "204", "400", "404", "500"]),
    )

    mock_reuse = BoolConfigItem(
        group="Mock",
        name="Reuse",
        default=True,
    )

    # 命中后刷新日期/Expires/Last-Modified 头与 Cookie 过期（原生默认 True）。
    mock_refresh = BoolConfigItem(
        group="Mock",
        name="Refresh",
        default=True,
    )

    # —— 高级匹配（原生哈希粒度选项；变更时原生 configure 自动重算哈希）——
    mock_ignore_host = BoolConfigItem(
        group="Mock",
        name="IgnoreHost",
        default=False,
    )

    # 两个列表项与 block_list 同一个坑：QConfig.set 开头 `if item.value == value:
    # return`，原地 mutate 再 set 会静默不落盘 —— 写回必须传新 list。
    mock_ignore_params = ConfigItem(
        group="Mock",
        name="IgnoreParams",
        default=[],
        serializer=_StringListSerializer(),
    )

    mock_use_headers = ConfigItem(
        group="Mock",
        name="UseHeaders",
        default=[],
        serializer=_StringListSerializer(),
    )

    # 「导出字段为 CSV」上次勾选的列（存 flow_detail 的 dict key，跨语言稳定；
    # 见 apps/common/flow/csv_export.py）。和 block_list 同一个坑：QConfig.set 开头
    # `if item.value == value: return`，写回必须传一个新 list。默认列在 csv_export.py
    # 侧收敛（这里给 [] 触发回落，避免默认值在两处各写一份）。
    csv_export_fields = ConfigItem(
        group="Export",
        name="CsvFields",
        default=[],
    )

    # 流列表列布局（存 {version, order, visible, widths} dict，稳定 key 跨语言不变；
    # 见 apps/common/flow/columns.py）。默认 {} 触发消费侧回落到出厂布局（默认布局
    # 只在 columns.py 收敛，不在两处各写一份）。和 block_list 同一个坑：QConfig.set
    # 开头 `if item.value == value: return`，写回必须传一个新 dict。
    flow_columns = ConfigItem(
        group="FlowList",
        name="Columns",
        default={},
    )

    # 启动后自动检查更新（docs/design.md#update）：只控制「启动那一次静默
    # 检查」，设置页的手动入口恒可用，不受此开关影响。BoolConfigItem 而非裸
    # validator=BoolValidator()：坏值回落本项默认（#88 的收口语义）。
    auto_check_update = BoolConfigItem(
        group="Update",
        name="AutoCheck",
        default=True,
    )


# 历史版本用 AppConfigLocation（%LocalAppData%\Ferret），与 Velopack 安装根
# （%LocalAppData%\<packId>，scripts/package.py 的 PACK_ID 同为 "Ferret"）撞车：
# 覆盖重装 / 卸载会整个替换该根，CA、会话、脚本连锅端，之后 mitmproxy 静默生成新
# CA、系统信任的还是旧的那张 → STALE「证书失效」。Roaming 在 Velopack 领地之外，
# 官方也建议要活过卸载的数据放那里。旧根同时是 Velopack 工作目录，只搬走自己的
# 数据项，绝不整目录删除。
_LEGACY_CONFIG_ITEMS = (
    CONFIG_NAME,
    f"{CONFIG_NAME}.bak",
    "certs",
    "sessions",
    "scripts",
    "mock_pool.flow",
    "system-proxy-state.json",
)


def _migrate_script_paths(path: Path, old_dir: Path, new_dir: Path) -> None:
    """修正已搬走的托管脚本引用；直接保留原始 JSON 中的其他配置。"""
    try:
        data = Config._read(path)
    except (OSError, ValueError, UnicodeError):
        # 主配置损坏时仍要继续处理备份，恢复与损坏提示留给 Config.load。
        return
    group = data.get("Scripts")
    entries = group.get("Scripts") if isinstance(group, dict) else None
    if not isinstance(entries, list):
        return
    old_scripts = (old_dir / "scripts").resolve()
    new_scripts = new_dir / "scripts"
    changed = False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("origin") != "new":
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            continue
        try:
            source = Path(raw_path)
            if not source.is_absolute():
                continue
            source = source.resolve()
            target = new_scripts / source.relative_to(old_scripts)
            # 旧文件仍在表示未迁移成功或目标冲突，不能改绑到另一份同名脚本。
            if source.exists() or not target.is_file():
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        entry["path"] = str(target)
        changed = True
    if changed:
        try:
            Config._write(path, data)
        except OSError as exc:
            logging.getLogger("ferret.settings").warning(
                "托管脚本路径迁移失败 %s: %s", path, exc
            )


def _migrate_legacy_config_dir(new_dir: Path) -> None:
    """把 AppConfigLocation 时代的自有数据一次性搬到 Roaming（幂等，不覆盖新数据）。"""
    old_dir = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    )
    if old_dir == new_dir:
        return
    candidates = [old_dir / name for name in _LEGACY_CONFIG_ITEMS]
    candidates.extend(old_dir.glob("ferret.log*"))
    for src in candidates:
        dst = new_dir / src.name
        if not src.exists() or dst.exists():
            continue
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
        except OSError as exc:
            logging.getLogger("ferret.settings").warning(
                "旧配置目录迁移失败 %s: %s", src, exc
            )
    # 文件移动成功而原子写配置失败时，下次仍须重试；旧目录已消失也不例外。
    for name in (CONFIG_NAME, f"{CONFIG_NAME}.bak"):
        _migrate_script_paths(new_dir / name, old_dir, new_dir)


def get_config_dir() -> Path:
    d = Path(
        QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    )
    _migrate_legacy_config_dir(d)
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


def get_scripts_dir() -> Path:
    """应用内新建脚本的托管目录（docs/design.md#scripts；打包后路径稳定可写）。"""
    directory = get_config_dir() / "scripts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def get_mock_pool_file() -> Path:
    """mock 响应池的托管 .flow 文件（docs/design.md#mock）。

    池不落 config.json：单条流是完整报文，塞进 JSON 既大又得自己序列化；直接用
    原生 FlowFile（`core/mitm/io.py`）存 .flow，导入/导出与内核读同一条格式。
    """
    return get_config_dir() / "mock_pool.flow"


CONFIG = Config()
