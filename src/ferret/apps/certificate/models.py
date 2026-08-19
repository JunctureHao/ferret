"""证书页的展示模型：把 core 的 `CaInfo` / `TrustState` 翻成界面文案。

不含 Qt。这里只负责「数据 → 字符串」，图标与按钮由 `views.py` 决定。
"""

from dataclasses import dataclass
from datetime import datetime

from ferret.core.mitm import CaInfo, TrustState

STATE_TITLES: dict[TrustState, str] = {
    TrustState.MISSING: "尚未生成 CA 证书",
    TrustState.ABSENT: "证书未安装",
    TrustState.TRUSTED: "证书已安装",
    TrustState.STALE: "系统信任的是旧证书",
    TrustState.UNAVAILABLE: "无法检测安装状态",
}

# STALE 是最容易踩的坑：界面若只按名字判定就会显示「已安装」，
# 但系统里那张旧 CA 和现在的私钥对不上，HTTPS 照样解密失败。
STATE_DETAILS: dict[TrustState, str] = {
    TrustState.MISSING: "点击安装会自动生成一套 CA 证书并写入系统信任库。",
    TrustState.ABSENT: "解密 HTTPS 流量前，需要把本机 CA 证书装进系统受信任的根证书。",
    TrustState.TRUSTED: "系统受信任的根证书里就是当前这张 CA，可以正常解密 HTTPS。",
    TrustState.STALE: (
        "系统里存的是同名的旧 CA，与当前证书不匹配，HTTPS 仍会报证书错误。"
        "重新安装即可覆盖。"
    ),
    TrustState.UNAVAILABLE: "当前系统上找不到 certutil 命令，请手动导入证书文件。",
}

_INSTALLABLE = (TrustState.MISSING, TrustState.ABSENT, TrustState.STALE)
_REMOVABLE = (TrustState.TRUSTED, TrustState.STALE)


@dataclass(frozen=True, slots=True)
class CertificateState:
    """一次检测的完整结果：信任库状态 + 磁盘上证书的快照。"""

    trust: TrustState = TrustState.MISSING
    info: CaInfo | None = None

    @property
    def title(self) -> str:
        return STATE_TITLES[self.trust]

    @property
    def detail(self) -> str:
        if self.trust is TrustState.TRUSTED and self.info is not None:
            if self.info.expired:
                return "证书已过期，请重新生成后再安装。"
            if self.info.days_remaining < 30:
                return f"证书已安装，但只剩 {self.info.days_remaining} 天有效期。"
        return STATE_DETAILS[self.trust]

    @property
    def can_install(self) -> bool:
        return self.trust in _INSTALLABLE

    @property
    def can_uninstall(self) -> bool:
        return self.trust in _REMOVABLE

    @property
    def needs_reinstall(self) -> bool:
        return self.trust is TrustState.STALE


def format_time(value: datetime) -> str:
    """UTC 转本机时区显示：证书里存的是 UTC，用户看的是本地时间。"""
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def format_fingerprint(hex_digest: str) -> str:
    """按字节分组，方便和 certmgr.msc 的指纹逐段核对。

    分隔符用空格而不是冒号，一是 certutil / certmgr.msc 本来就这么显示，
    二是 QLabel 只在空白处断行——冒号连起来的 95 个字符是一个不可拆的整体，
    窄窗口下既换不了行，还会把整页顶宽。
    """
    return " ".join(hex_digest[i : i + 2].upper() for i in range(0, len(hex_digest), 2))


def _validity(info: CaInfo) -> str:
    days = info.days_remaining
    span = f"{format_time(info.not_before)} ~ {format_time(info.not_after)}"
    if info.expired:
        return f"{span}（已过期）"
    return f"{span}（剩余 {days} 天）"


def info_rows(info: CaInfo) -> list[tuple[str, str]]:
    """详情卡的字段表。全部取自 mitmproxy `certs.Cert` 的现成字段。"""
    return [
        ("通用名称", info.common_name or "-"),
        ("组织", info.organization or "-"),
        ("使用者", info.subject),
        ("颁发者", info.issuer + ("（自签名）" if info.self_signed else "")),
        ("序列号", info.serial_hex),
        ("SHA-256 指纹", format_fingerprint(info.fingerprint_sha256)),
        ("有效期", _validity(info)),
        ("密钥", f"{info.key_type} {info.key_bits} 位"),
        ("证书类型", "根 CA" if info.is_ca else "非 CA 证书"),
        ("文件位置", str(info.path)),
    ]
