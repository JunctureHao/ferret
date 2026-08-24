"""证书页的展示模型：把 core 的 `CaInfo` / `TrustState` 翻成界面文案。

只碰 `QtCore` 的翻译接口，不碰控件。这里只负责「数据 → 字符串」，图标与按钮由
`views.py` 决定。

界面文案的源语言是英文，中文来自 `zh_CN.qm`。模块级的两张表因此只做标记不求值 ——
`core/application.py` 顶层就 import 了 `MainWindow`，模块级求值赶在 `_init_i18n()`
安装翻译器之前，译文会永久冻结成英文。求值放在 `title` / `detail` 里。
"""

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import CaInfo, TrustState
from ferret.utils.i18n import QT_TRANSLATE_NOOP

STATE_TITLES: dict[TrustState, str] = {
    TrustState.MISSING: QT_TRANSLATE_NOOP("CertificateState", "No CA certificate yet"),
    TrustState.ABSENT: QT_TRANSLATE_NOOP(
        "CertificateState", "Certificate not installed"
    ),
    TrustState.TRUSTED: QT_TRANSLATE_NOOP("CertificateState", "Certificate installed"),
    TrustState.STALE: QT_TRANSLATE_NOOP(
        "CertificateState", "The system trusts an older certificate"
    ),
    TrustState.UNAVAILABLE: QT_TRANSLATE_NOOP(
        "CertificateState", "Cannot detect the install state"
    ),
}

# STALE 是最容易踩的坑：界面若只按名字判定就会显示「已安装」，
# 但系统里那张旧 CA 和现在的私钥对不上，HTTPS 照样解密失败。
STATE_DETAILS: dict[TrustState, str] = {
    TrustState.MISSING: QT_TRANSLATE_NOOP(
        "CertificateState",
        "Installing generates a CA certificate and writes it into the system trust store.",
    ),
    TrustState.ABSENT: QT_TRANSLATE_NOOP(
        "CertificateState",
        "Install this machine's CA certificate into the system trusted roots before "
        "decrypting HTTPS traffic.",
    ),
    TrustState.TRUSTED: QT_TRANSLATE_NOOP(
        "CertificateState",
        "The system trusted roots hold this exact CA, so HTTPS decrypts normally.",
    ),
    TrustState.STALE: QT_TRANSLATE_NOOP(
        "CertificateState",
        "The system holds an older CA with the same name that does not match the "
        "current certificate, so HTTPS still reports certificate errors. "
        "Reinstalling overwrites it.",
    ),
    TrustState.UNAVAILABLE: QT_TRANSLATE_NOOP(
        "CertificateState",
        "The certutil command is missing on this system; import the certificate file "
        "manually.",
    ),
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
        return QCoreApplication.translate("CertificateState", STATE_TITLES[self.trust])

    @property
    def detail(self) -> str:
        translate = QCoreApplication.translate
        if self.trust is TrustState.TRUSTED and self.info is not None:
            if self.info.expired:
                return translate(
                    "CertificateState",
                    "The certificate has expired; regenerate it before installing.",
                )
            if self.info.days_remaining < 30:
                # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
                return translate(
                    "CertificateState",
                    "Certificate installed, but only {} day(s) of validity remain.",
                ).format(self.info.days_remaining)
        return translate("CertificateState", STATE_DETAILS[self.trust])

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
    translate = QCoreApplication.translate
    span = f"{format_time(info.not_before)} ~ {format_time(info.not_after)}"
    if info.expired:
        return translate("CertificateInfo", "{} (expired)").format(span)
    return translate("CertificateInfo", "{} ({} day(s) left)").format(
        span, info.days_remaining
    )


def info_rows(info: CaInfo) -> list[tuple[str, str]]:
    """详情卡的字段表。全部取自 mitmproxy `certs.Cert` 的现成字段。"""
    translate = QCoreApplication.translate
    issuer = info.issuer
    if info.self_signed:
        issuer = translate("CertificateInfo", "{} (self-signed)").format(issuer)
    return [
        (translate("CertificateInfo", "Common name"), info.common_name or "-"),
        (translate("CertificateInfo", "Organization"), info.organization or "-"),
        (translate("CertificateInfo", "Subject"), info.subject),
        (translate("CertificateInfo", "Issuer"), issuer),
        (translate("CertificateInfo", "Serial number"), info.serial_hex),
        (
            translate("CertificateInfo", "SHA-256 fingerprint"),
            format_fingerprint(info.fingerprint_sha256),
        ),
        (translate("CertificateInfo", "Validity"), _validity(info)),
        (
            translate("CertificateInfo", "Key"),
            translate("CertificateInfo", "{} {} bits").format(
                info.key_type, info.key_bits
            ),
        ),
        (
            translate("CertificateInfo", "Certificate type"),
            translate("CertificateInfo", "Root CA")
            if info.is_ca
            else translate("CertificateInfo", "Not a CA certificate"),
        ),
        (translate("CertificateInfo", "File location"), str(info.path)),
    ]
