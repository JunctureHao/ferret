"""Ferret 的 CA 证书业务。

分工按 mitmproxy 的实际能力划线（`mitmproxy/certs.py`）：

- **生成 / 重新生成**：`certs.CertStore.from_store` → `create_store` → `create_ca`，
  一次写全六个产物，ferret 只负责删旧文件再调它。
- **查看**：`certs.Cert` 的现成字段（cn / organization / subject / issuer /
  notbefore / notafter / has_expired / serial / fingerprint / keyinfo / is_ca），
  这里只做「x509 对象 → 纯数据快照」的搬运，不自己解析 ASN.1。
- **导出**：`Cert.to_pem()`（PEM），`.cer` / `.p12` 直接取 `create_store` 已经写好的文件。
- **安装 / 卸载**：mitmproxy 完全不管系统信任库，只能走系统命令（Windows `certutil`）。

只用 QtCore 的 `QCoreApplication.translate`、不碰控件：本模块抛出的异常消息会被
`apps/certificate` 原样显示给用户，所以文案必须过翻译目录。所有方法都是同步阻塞的，
调用方负责挪到后台线程（见 `apps/certificate`）。
"""

from __future__ import annotations

import datetime
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import certifi
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import KEY_SIZE, certs
from ferret.core.settings import APP_NAME, get_certs_dir
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# certs.CertStore.create_store 一次写出的全部产物，重新生成时必须整组删掉，
# 只删证书不删 -ca.pem 会让 from_store 直接复用旧私钥、序列号不变。
CA_KEY_PEM = f"{APP_NAME}-ca.pem"  # 私钥 + 证书
CA_KEY_P12 = f"{APP_NAME}-ca.p12"  # 私钥 + 证书（PKCS#12）
CA_CERT_PEM = f"{APP_NAME}-ca-cert.pem"  # 仅证书
CA_CERT_CER = f"{APP_NAME}-ca-cert.cer"  # 仅证书，内容同 PEM，供 Android
CA_CERT_P12 = f"{APP_NAME}-ca-cert.p12"  # 仅证书，供 Windows 设备
CA_DHPARAM_PEM = f"{APP_NAME}-dhparam.pem"

CA_ARTIFACTS: tuple[str, ...] = (
    CA_KEY_PEM,
    CA_KEY_P12,
    CA_CERT_PEM,
    CA_CERT_CER,
    CA_CERT_P12,
    CA_DHPARAM_PEM,
)

# Windows 当前用户的「受信任的根证书颁发机构」。写死 -user：装到 LocalMachine
# 需要管理员，ferret 是普通用户进程。
_ROOT_STORE = "Root"

# 同名旧 CA 可能残留多张（每次重新生成都会多一张），逐张删；
# 上限是「最多删几张」，纯粹防死循环——删完还查得到才算残留。
_MAX_DELETE_ROUNDS = 8

# `certutil -addstore/-delstore -user Root` 会弹 Windows 的安全警告
# （「你想安装/删除这个根证书吗？」），点「否」时进程以 ERROR_CANCELLED(1223)
# 包成的 HRESULT 退出。这不是失败，是「没做」。
_ERROR_CANCELLED = 0x800704C7


class CertificateError(RuntimeError):
    """证书操作失败。UI 只需要 catch 这一个类型。"""


class CertutilUnavailable(CertificateError):
    """当前系统上没有 certutil（非 Windows，或 PATH 被裁剪）。"""


class CertificateCancelled(CertificateError):
    """用户在 Windows 的安全警告里点了「否」。

    只有安装 / 卸载会弹这个警告。单独立一个类型，好让上层安静收场——
    按普通 `CertificateError` 处理会弹一个「安装失败」，而用户明明是自己点的取消。
    """


class TrustState(StrEnum):
    """系统信任库里本 CA 的状态。"""

    MISSING = "missing"  # 磁盘上还没有 CA，谈不上信任
    ABSENT = "absent"  # 有 CA 文件，但系统信任里没有
    TRUSTED = "trusted"  # 已信任，且正是磁盘上这一张
    STALE = "stale"  # 已信任的是同名旧 CA —— 解密照样会失败，必须重装
    UNAVAILABLE = "unavailable"  # 无法查询（没有 certutil）


@dataclass(frozen=True, slots=True)
class CertExportFormat:
    """一种可导出的格式。私钥产物一律不在此列，避免误导出。"""

    key: str
    filename: str
    label: str
    hint: str
    file_filter: str
    # True 表示用原生 Cert.to_pem() 重新序列化，而不是复制磁盘文件。
    from_pem_api: bool = False


# 三段文案只存标记：这张表是模块级常量，求值赶在翻译器安装之前（`core/application.py`
# 顶层就 import 了主窗口），译文会永久冻结成英文。求值在用的地方做 —— 见
# `apps/certificate/views.py` 的 `_export_texts`。
EXPORT_FORMATS: tuple[CertExportFormat, ...] = (
    CertExportFormat(
        key="pem",
        filename=CA_CERT_PEM,
        label=QT_TRANSLATE_NOOP("CertExportFormat", "PEM 证书 (.pem)"),
        hint=QT_TRANSLATE_NOOP(
            "CertExportFormat", "桌面浏览器、curl、OpenSSL 通用格式"
        ),
        file_filter=QT_TRANSLATE_NOOP("CertExportFormat", "PEM 证书 (*.pem)"),
        from_pem_api=True,
    ),
    CertExportFormat(
        key="cer",
        filename=CA_CERT_CER,
        label=QT_TRANSLATE_NOOP("CertExportFormat", "CER 证书 (.cer)"),
        hint=QT_TRANSLATE_NOOP(
            "CertExportFormat", "Android 设备导入用，内容与 PEM 相同"
        ),
        file_filter=QT_TRANSLATE_NOOP("CertExportFormat", "CER 证书 (*.cer)"),
    ),
    CertExportFormat(
        key="p12",
        filename=CA_CERT_P12,
        label=QT_TRANSLATE_NOOP("CertExportFormat", "PKCS#12 证书 (.p12)"),
        hint=QT_TRANSLATE_NOOP(
            "CertExportFormat",
            "Windows / iOS 设备导入用，不含私钥",
        ),
        file_filter=QT_TRANSLATE_NOOP("CertExportFormat", "PKCS#12 证书 (*.p12)"),
    ),
)


def export_format(key: str) -> CertExportFormat:
    for fmt in EXPORT_FORMATS:
        if fmt.key == key:
            return fmt
    # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
    raise CertificateError(
        QCoreApplication.translate("CertificateService", "未知的导出格式：{}").format(
            key
        )
    )


def _join_name(pairs: Sequence[tuple[str, str]]) -> str:
    return ", ".join(f"{k}={v}" for k, v in pairs)


@dataclass(frozen=True, slots=True)
class CaInfo:
    """CA 证书的纯数据快照，字段全部来自 mitmproxy 的 `certs.Cert`。

    刻意不往 `apps/` 传 x509 对象：UI 层只拿字符串和数字，也便于测试断言。
    """

    common_name: str
    organization: str
    subject: str
    issuer: str
    serial_hex: str
    fingerprint_sha256: str
    not_before: datetime.datetime
    not_after: datetime.datetime
    expired: bool
    key_type: str
    key_bits: int
    is_ca: bool
    path: Path

    @classmethod
    def from_cert(cls, cert: certs.Cert, path: Path) -> CaInfo:
        key_type, key_bits = cert.keyinfo
        return cls(
            common_name=cert.cn or "",
            organization=cert.organization or "",
            subject=_join_name(cert.subject),
            issuer=_join_name(cert.issuer),
            # certutil 的「序列号」列显示的就是小写十六进制，保持一致便于人工核对。
            serial_hex=f"{cert.serial:x}",
            fingerprint_sha256=cert.fingerprint().hex(),
            not_before=cert.notbefore,
            not_after=cert.notafter,
            expired=cert.has_expired(),
            key_type=key_type,
            key_bits=key_bits,
            is_ca=cert.is_ca,
            path=path,
        )

    @property
    def days_remaining(self) -> int:
        """距离到期还剩几天，已过期为负。"""
        now = datetime.datetime.now(datetime.UTC)
        return (self.not_after - now).days

    @property
    def self_signed(self) -> bool:
        return self.subject == self.issuer


# --- 上游信任库（.plans/upstream-tls.md）---
#
# 原生 `ssl_verify_upstream_trusted_ca` 是**替换**语义而不是追加：
# `net/tls.py::create_proxy_server_context` 只在 ca_path 与 ca_pemfile 双双为空时
# 才回落 `certifi.where()`，填了用户那把根，公共根就整个不加载 —— 用户加一把测试
# 根会把百度都搞挂。所以 ferret 自己把「公共根 + 用户根」合并成一份产物再下发，
# 让「加测试根」与「公共站点照常校验」同时成立（方案 D1）。
#
# 产物名必须带内容指纹：`create_proxy_server_context` 是 `@lru_cache(256)`，缓存键
# 里的 ca_pemfile 是**路径字符串**。文件名固定的话，用户换一把根（内容变、路径没变）
# 会永久命中旧 context，热更静默失效且无从排查。改名即改键，旧 context 自然作废。
TRUSTED_CA_PREFIX = "upstream-trusted-"
TRUSTED_CA_SUFFIX = ".pem"

# 一张 PEM 证书的边界。用户手上的文件常是多张拼起来的 bundle，而 `Cert.from_pem`
# 只解第一张 —— 自己按块切开逐张解，既是校验也是计数（卡片要显示「共 N 张」）。
_PEM_CERT_BLOCK = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class TrustedCaSummary:
    """用户信任库的一次静态盘点：只读文件、**不写任何产物**。

    界面刷新卡片文案走这条（每次刷新都写盘既浪费又意外），真要下发时才调
    `build_trusted_ca_bundle`。两者共用同一个解析函数，判定必然一致。
    """

    good: tuple[str, ...]  # 至少解出一张证书的文件
    bad: tuple[str, ...]  # 读不到 / 解不出证书的文件（顺序同入参）
    cert_count: int  # good 里的证书总张数，不含公共根

    @property
    def configured(self) -> int:
        """去掉空行之后，用户实际配了几个文件。"""
        return len(self.good) + len(self.bad)


def _read_cert_pems(path: str) -> list[bytes]:
    """把一个文件里的证书逐张解出来，重新序列化成规范 PEM。

    解不动就返回空列表（调用方据此归入坏文件）——「这文件根本不是证书」要在
    保存那一刻就告诉用户，而不是等某次 TLS 握手失败。经 `Cert.to_pem()` 过一手
    是顺带的好处：进 bundle 的一定是解析得动的规范编码。
    """
    try:
        raw = Path(path).expanduser().read_bytes()
    except OSError:
        return []
    pems: list[bytes] = []
    for block in _PEM_CERT_BLOCK.findall(raw):
        try:
            pems.append(certs.Cert.from_pem(block).to_pem())
        except (ValueError, TypeError):
            # 同一个文件里只要有一块坏的就整份判坏：半份信任库比没有更难排查。
            return []
    return pems


def inspect_trusted_ca_files(files: Sequence[str]) -> TrustedCaSummary:
    """盘点用户给的信任文件，不写盘。空行 / 纯空白路径忽略。"""
    good: list[str] = []
    bad: list[str] = []
    count = 0
    for item in files:
        path = item.strip()
        if not path:
            continue
        pems = _read_cert_pems(path)
        if pems:
            good.append(path)
            count += len(pems)
        else:
            bad.append(path)
    return TrustedCaSummary(good=tuple(good), bad=tuple(bad), cert_count=count)


def _prune_trusted_ca_bundles(directory: Path, keep: Path | None) -> None:
    """清掉本函数族生成的旧指纹产物，`keep` 那份留着。

    只认自己的前缀：`CA_ARTIFACTS` 那族是 `{APP_NAME}-*`，两边永不相交。
    删不掉只是攒下垃圾文件，不值得让下发失败，所以 OSError 一律咽掉。
    """
    try:
        stale_files = list(directory.glob(f"{TRUSTED_CA_PREFIX}*{TRUSTED_CA_SUFFIX}"))
    except OSError:
        return
    for stale in stale_files:
        if keep is not None and stale == keep:
            continue
        try:
            stale.unlink()
        except OSError:
            pass


def build_trusted_ca_bundle(
    files: Sequence[str],
    *,
    certs_dir: Path | None = None,
) -> tuple[str | None, list[str]]:
    """把公共根 + 用户根合并成一份带指纹的 PEM，返回 (产物路径|None, 坏文件列表)。

    返回 None 表示「不下发 ca_pemfile」= 原生 certifi 行为，两种情形：入参为空，
    或者给的文件一张证书都解不出来（全坏 → 回退公共根，而不是把上游信任库搞成
    空库）。坏文件只进返回值、**不抛异常** —— 一把坏证书不该拖死内核启动，
    调用方负责记日志、界面显示「N 个文件已失效」。

    写盘失败才抛 `CertificateError`：那是「用户以为加上了、其实没加」的场景，
    必须说出来。写入走临时文件 + `os.replace`，半份 PEM 不会出现在目标路径上。
    """
    directory = certs_dir if certs_dir is not None else get_certs_dir()
    summary = inspect_trusted_ca_files(files)
    bad = list(summary.bad)
    if not summary.good:
        # 零产物：顺手把上一次的指纹文件清掉，别在证书目录里留孤儿。
        _prune_trusted_ca_bundles(directory, keep=None)
        return None, bad

    chunks = [Path(certifi.where()).read_bytes()]
    for path in summary.good:
        chunks.extend(_read_cert_pems(path))
    blob = b"\n".join(chunk.rstrip(b"\n") for chunk in chunks) + b"\n"
    digest = hashlib.sha256(blob).hexdigest()[:8]
    target = directory / f"{TRUSTED_CA_PREFIX}{digest}{TRUSTED_CA_SUFFIX}"

    # 内容寻址：同名即同内容，已经在盘上就不重写（也避免动它的 mtime）。
    if not target.exists():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            # 临时文件落在同一个目录里：os.replace 只在同一卷上才保证原子。
            handle, temp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as fp:
                    fp.write(blob)
                os.replace(temp_name, target)
            except OSError:
                Path(temp_name).unlink(missing_ok=True)
                raise
        except OSError as exc:
            raise CertificateError(
                QCoreApplication.translate(
                    "CertificateService", "上游信任库写入失败：{}"
                ).format(exc)
            ) from exc
    _prune_trusted_ca_bundles(directory, keep=target)
    return str(target), bad


# --- mTLS 客户端证书（.plans/mtls-client-certs.md）---
#
# 原生 `client_certs` 是**一个路径**：指到文件 = 对每个要客户端证书的上游都出示同一张；
# 指到目录 = 按 SNI 找 `<主机名>.pem`，精确匹配、无通配、无兜底。这里只做只读盘点与
# 保存前的闸门，一张证书都不拷贝、不改写 —— 用户的私钥留在他自己的目录里。

# 一块私钥的 PEM 边界。原生 use_privatekey_file 认得的几种头都在这个式子里：
# PKCS#8 明文（PRIVATE KEY）、PKCS#8 加密（ENCRYPTED PRIVATE KEY）、传统格式
# （RSA/EC/DSA PRIVATE KEY）。**加密的传统 PEM 不改块名**，靠块内的 Proc-Type 头标记
# —— 所以「是不是加密私钥」只能真解一次才知道，不能拿字符串判（见
# `_client_cert_key_error` 与 .plans/mtls-client-certs.md §2.4）。
_PEM_KEY_BLOCK = re.compile(
    rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# 目录模式一次最多解析多少张 .pem。用户完全可能把路径指到盘根或一个 UNC 共享，而盘点
# 跑在界面线程上（对话框实时预览 + 卡片刷新），逐张解 X.509 会肉眼可见地卡。超出就截断
# 并置 truncated，界面照实说「只盘了前 N 个」。
CLIENT_CERTS_SCAN_LIMIT = 200

# 原生只认这一个后缀（os.path.join(dir, f"{sni}.pem")），扫目录时同口径。
CLIENT_CERT_SUFFIX = ".pem"

# 「创建推荐目录」按钮的落点名，挂在证书目录下面。
CLIENT_CERTS_DIR_NAME = "client-certs"


@dataclass(frozen=True, slots=True)
class ClientCertEntry:
    """一个客户端证书文件的解析结果。`error` 为空串 = 可用。

    `error` 是**已翻译的整句**，对话框和卡片直接显示，不再二次拼装。
    """

    path: str
    name: str  # 文件名。目录模式下它就是要匹配的主机名（<主机名>.pem）
    error: str = ""
    cn: str = ""
    notafter: datetime.datetime | None = None
    expired: bool = False
    cert_count: int = 0  # 文件里的证书张数：1 张叶子 + N 张中间证书


@dataclass(frozen=True, slots=True)
class ClientCertsSummary:
    """对配置里那一个路径的静态盘点：只读文件、**不写任何产物**。

    与 `TrustedCaSummary` 同姿态 —— 界面刷新卡片、对话框实时预览都走这条，真要下发时
    由 `client_certs_error` 做闸门，两者共用同一个解析函数，判定必然一致。
    """

    configured: str  # 用户配的原样路径（可能带 ~），空串 = 未启用
    exists: bool
    is_dir: bool
    entries: tuple[ClientCertEntry, ...]
    truncated: bool = False  # 目录里的 .pem 超过 CLIENT_CERTS_SCAN_LIMIT，只盘了前一批

    @property
    def good(self) -> tuple[ClientCertEntry, ...]:
        return tuple(item for item in self.entries if not item.error)

    @property
    def bad(self) -> tuple[ClientCertEntry, ...]:
        return tuple(item for item in self.entries if item.error)

    @property
    def expired(self) -> tuple[ClientCertEntry, ...]:
        """已过期但**仍然可用**的那些：有的服务器不校验有效期，不替它拒。"""
        return tuple(item for item in self.entries if not item.error and item.expired)


def _client_cert_key_error(raw: bytes, path: str) -> str:
    """解一遍私钥，返回空串 = 可用，否则一句已翻译的原因。

    加密私钥判成 TypeError 而不是看字符串：传统格式的加密 PEM 块名仍是
    `RSA PRIVATE KEY`，`ENCRYPTED PRIVATE KEY` 只覆盖 PKCS#8 那一半。
    """
    blocks = _PEM_KEY_BLOCK.findall(raw)
    if not blocks:
        return QCoreApplication.translate(
            "CertificateService",
            "文件里没有私钥。客户端证书要把私钥和证书拼进同一个 .pem。",
        )
    try:
        certs.load_pem_private_key(blocks[0], None)
    except TypeError:
        # 原生 use_privatekey_file 没有口令通道，自存口令等于在配置里再落一份秘密。
        return QCoreApplication.translate(
            "CertificateService",
            "私钥已加密，Ferret 不支持口令。先解密：openssl rsa -in {} -out key.pem，"
            "再把解密后的私钥与证书拼进同一个 .pem。",
        ).format(path)
    except Exception as exc:  # noqa: BLE001  cryptography 的异常谱没承诺，坏文件不该炸盘点
        return QCoreApplication.translate(
            "CertificateService", "私钥解析失败：{}"
        ).format(exc)
    return ""


def inspect_client_cert_file(path: str) -> ClientCertEntry:
    """单份客户端证书的五连判：读得到、有证书、证书解得出、私钥解得出、公钥配对。

    最后一判不能省：私钥与证书不配对时 `use_privatekey_file` 与
    `use_certificate_chain_file` **都不报错**，OpenSSL 只是静默丢掉那把私钥，最终表现
    为一次「没出示证书」的失败握手，用户无从排查（见 .plans/mtls-client-certs.md §2.4）。
    """
    target = Path(path).expanduser()
    name = target.name
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return ClientCertEntry(
            path=path,
            name=name,
            error=QCoreApplication.translate(
                "CertificateService", "读不到文件：{}"
            ).format(exc),
        )

    cert_blocks = _PEM_CERT_BLOCK.findall(raw)
    if not cert_blocks:
        return ClientCertEntry(
            path=path,
            name=name,
            error=QCoreApplication.translate("CertificateService", "文件里没有证书。"),
        )
    try:
        # 原生 use_certificate_chain_file 拿第一张当叶子、其余算中间证书，这里同口径。
        cert = certs.Cert.from_pem(cert_blocks[0])
    except (ValueError, TypeError) as exc:
        return ClientCertEntry(
            path=path,
            name=name,
            error=QCoreApplication.translate(
                "CertificateService", "证书解析失败：{}"
            ).format(exc),
        )

    key_error = _client_cert_key_error(raw, path)
    if key_error:
        return ClientCertEntry(path=path, name=name, error=key_error)

    key = certs.load_pem_private_key(_PEM_KEY_BLOCK.findall(raw)[0], None)
    if key.public_key() != cert.public_key():
        return ClientCertEntry(
            path=path,
            name=name,
            error=QCoreApplication.translate(
                "CertificateService",
                "私钥与证书不匹配，出示时会被静默丢弃。请确认两者来自同一次签发。",
            ),
        )

    return ClientCertEntry(
        path=path,
        name=name,
        cn=cert.cn or "",
        notafter=cert.notafter,
        expired=cert.has_expired(),
        cert_count=len(cert_blocks),
    )


def _scan_client_certs_dir(directory: Path) -> tuple[tuple[ClientCertEntry, ...], bool]:
    """非递归扫目录里的 .pem，返回（盘点, 是否被截断）。

    非递归是照原生来的：它只拼 `<目录>/<主机名>.pem`，子目录里的文件永远匹配不到。
    """
    try:
        found = sorted(
            item
            for item in directory.iterdir()
            if item.suffix.lower() == CLIENT_CERT_SUFFIX and item.is_file()
        )
    except OSError:
        return (), False
    truncated = len(found) > CLIENT_CERTS_SCAN_LIMIT
    entries = tuple(
        inspect_client_cert_file(str(item)) for item in found[:CLIENT_CERTS_SCAN_LIMIT]
    )
    return entries, truncated


def inspect_client_certs(path: str) -> ClientCertsSummary:
    """盘点配置里那一个路径，不写盘。形态（文件 / 目录）由磁盘现状决定。"""
    configured = path.strip()
    if not configured:
        return ClientCertsSummary(configured="", exists=False, is_dir=False, entries=())
    target = Path(configured).expanduser()
    if target.is_dir():
        entries, truncated = _scan_client_certs_dir(target)
        return ClientCertsSummary(
            configured=configured,
            exists=True,
            is_dir=True,
            entries=entries,
            truncated=truncated,
        )
    if target.is_file():
        return ClientCertsSummary(
            configured=configured,
            exists=True,
            is_dir=False,
            entries=(inspect_client_cert_file(configured),),
        )
    return ClientCertsSummary(
        configured=configured, exists=False, is_dir=False, entries=()
    )


def client_certs_error(path: str) -> str:
    """保存前的唯一闸门，返回空串 = 放行，否则一句已翻译的原因。

    提交链与内核种子共用它，判据必然一致。目录模式**只查存在性**：里面的坏文件只进
    盘点、不拦保存 —— 判据同上游信任组的「坏文件回退公共根」，一份坏证书不该让整项
    配不上，何况目录可能是刚建好还没往里放东西。
    """
    configured = path.strip()
    if not configured:
        return ""  # 空 = 清除，永远放行
    target = Path(configured).expanduser()
    if target.is_dir():
        return ""
    if not target.exists():
        return QCoreApplication.translate(
            "CertificateService", "路径不存在：{}"
        ).format(configured)
    return inspect_client_cert_file(configured).error


def client_certs_suggest_dir(certs_dir: Path | None = None) -> Path:
    """「按主机目录」模式的推荐落点。只算路径，**不创建目录**。

    写成函数而不是模块级常量：`get_certs_dir()` 本身就是调用期取值（打包后、换用户
    目录后都得重算），常量会把它钉死在导入那一刻。
    """
    directory = certs_dir if certs_dir is not None else get_certs_dir()
    return directory / CLIENT_CERTS_DIR_NAME


CertutilRunner = Callable[[Sequence[str]], int]


def run_certutil(args: Sequence[str]) -> int:
    """执行 certutil，只返回退出码。

    certutil 的输出是本地化的（本机打印中文），所以**唯一可以分支的信号是退出码**，
    绝不解析 stdout 文本。
    """
    try:
        completed = subprocess.run(
            ["certutil", *args],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:  # FileNotFoundError 也是 OSError
        raise CertutilUnavailable(
            QCoreApplication.translate(
                "CertificateService",
                "当前系统上找不到 certutil 命令",
            )
        ) from exc
    return completed.returncode


class SystemCertificateService:
    """CA 文件的生成/查看/导出，加上系统信任库的安装/卸载。"""

    def __init__(
        self,
        certs_dir: Path | None = None,
        runner: CertutilRunner = run_certutil,
    ) -> None:
        # 构造时不碰文件系统：目录延迟到真正要用时才解析/创建。
        self._certs_dir = certs_dir
        self._runner = runner

    # --- 路径 ---

    @property
    def certs_dir(self) -> Path:
        return self._certs_dir if self._certs_dir is not None else get_certs_dir()

    @property
    def cert_path(self) -> Path:
        """对外分发、也是装进系统信任库的那份（仅证书，无私钥）。"""
        return self.certs_dir / CA_CERT_PEM

    def artifact_paths(self) -> list[Path]:
        return [self.certs_dir / name for name in CA_ARTIFACTS]

    def exists(self) -> bool:
        return self.cert_path.exists()

    # --- 生成（原生） ---

    def ensure(self) -> CaInfo:
        """磁盘上没有 CA 就让 mitmproxy 生成一套，然后返回快照。

        `from_store` 看的是 `{APP_NAME}-ca.pem`；缺失时它自己调 `create_store`，
        六个产物一次写全，ferret 不复制任何生成逻辑。
        """
        directory = self.certs_dir
        directory.mkdir(parents=True, exist_ok=True)
        try:
            certs.CertStore.from_store(directory, APP_NAME, KEY_SIZE)
        except (OSError, ValueError) as exc:
            raise CertificateError(
                QCoreApplication.translate(
                    "CertificateService", "CA 证书生成失败：{}"
                ).format(exc)
            ) from exc
        info = self.load()
        if info is None:
            raise CertificateError(
                QCoreApplication.translate(
                    "CertificateService",
                    "CA 证书生成后仍读不到 {}",
                ).format(self.cert_path)
            )
        return info

    def regenerate(self) -> CaInfo:
        """删掉整组产物再重新生成。

        必须连 `-ca.pem`（私钥）一起删：`from_store` 只要看到它就复用旧私钥，
        序列号和指纹都不会变，「重新生成」就成了空操作。
        旧 CA 在系统信任里随即失效，调用方要提示用户重新安装。
        """
        for path in self.artifact_paths():
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise CertificateError(
                    QCoreApplication.translate(
                        "CertificateService",
                        "无法删除旧证书 {}：{}",
                    ).format(path.name, exc)
                ) from exc
        return self.ensure()

    # --- 查看（原生字段） ---

    def load(self) -> CaInfo | None:
        """读磁盘上的 CA；不存在或内容坏掉都返回 None。"""
        try:
            raw = self.cert_path.read_bytes()
        except OSError:
            return None
        try:
            cert = certs.Cert.from_pem(raw)
        except (ValueError, TypeError):
            return None
        return CaInfo.from_cert(cert, self.cert_path)

    def require(self) -> CaInfo:
        info = self.load()
        if info is None:
            raise CertificateError(
                QCoreApplication.translate("CertificateService", "尚未生成 CA 证书")
            )
        return info

    # --- 导出（原生 to_pem / create_store 的产物） ---

    def pem_bytes(self) -> bytes:
        """原生 `Cert.to_pem()`：顺带证明磁盘上那份是能解析的。"""
        raw = self.cert_path.read_bytes()
        return certs.Cert.from_pem(raw).to_pem()

    def export(self, fmt: CertExportFormat | str, target: Path | str) -> Path:
        """把指定格式写到 target。缺文件时先按需生成。"""
        if isinstance(fmt, str):
            fmt = export_format(fmt)
        source = self.certs_dir / fmt.filename
        if not source.exists():
            self.ensure()
        target = Path(target)
        try:
            data = self.pem_bytes() if fmt.from_pem_api else source.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except (OSError, ValueError) as exc:
            raise CertificateError(
                QCoreApplication.translate("CertificateService", "导出失败：{}").format(
                    exc
                )
            ) from exc
        return target

    # --- 系统信任库（certutil） ---

    def _query(self, cert_id: str) -> bool:
        """信任库里是否存在匹配 cert_id 的证书。CertId 可以是序列号或 CN。"""
        return self._runner(["-store", "-user", _ROOT_STORE, cert_id]) == 0

    def _checked(self, args: Sequence[str], *, cancelled: str, failed: str) -> None:
        """两句文案由调用方整句给出。

        不接「动作名」再去拼「{动作}失败」：别的语言语序不同，拼出来的句子没法翻。
        `failed` 是个带一个 `{}` 的模板，占位处填 certutil 的退出码。
        """
        # Windows 退出码是 DWORD，按无符号看：0x800704c7 之类的高位码
        # 在某些环境里会以负数递过来。
        code = self._runner(args) & 0xFFFFFFFF
        if code == _ERROR_CANCELLED:
            raise CertificateCancelled(cancelled)
        if code != 0:
            raise CertificateError(failed.format(f"0x{code:08x}"))

    def trust_state(self) -> TrustState:
        """按**序列号**判定，而不是 CN 子串。

        CN 子串匹配会把「上次生成、早已和磁盘私钥对不上的旧 CA」误判成已安装，
        表现是界面显示已信任、浏览器却照样报证书错误。
        """
        info = self.load()
        if info is None:
            return TrustState.MISSING
        try:
            if self._query(info.serial_hex):
                return TrustState.TRUSTED
            return TrustState.STALE if self._query(APP_NAME) else TrustState.ABSENT
        except CertutilUnavailable:
            return TrustState.UNAVAILABLE

    def is_installed(self) -> bool:
        """仅当信任库里就是当前这张 CA 时为真。"""
        return self.trust_state() is TrustState.TRUSTED

    def install(self) -> CaInfo:
        """按需生成 → 清掉同名旧 CA → 把当前 CA 装进用户根信任库。"""
        info = self.ensure()
        if self.trust_state() is TrustState.STALE:
            self.uninstall()
        translate = QCoreApplication.translate
        self._checked(
            ["-addstore", "-user", _ROOT_STORE, str(self.cert_path)],
            cancelled=translate("CertificateService", "安装证书已取消"),
            failed=translate(
                "CertificateService",
                "安装证书失败（certutil 退出码 {}）",
            ),
        )
        return info

    def uninstall(self) -> None:
        """从系统信任库里删掉本 CA（含历次重新生成留下的同名旧 CA）。

        `certutil -delstore` 一条都没删到时**也返回 0**（实测），所以不能拿它的
        退出码判断「删没删掉」，只能每轮先用 `-store` 查询复核。
        """
        translate = QCoreApplication.translate
        removed = 0
        while self._query(APP_NAME):
            if removed >= _MAX_DELETE_ROUNDS:
                raise CertificateError(
                    translate(
                        "CertificateService",
                        "系统信任库中仍有残留证书，请手动检查 certmgr.msc",
                    )
                )
            self._checked(
                ["-delstore", "-user", _ROOT_STORE, APP_NAME],
                cancelled=translate("CertificateService", "卸载证书已取消"),
                failed=translate(
                    "CertificateService",
                    "卸载证书失败（certutil 退出码 {}）",
                ),
            )
            removed += 1
        if not removed:
            raise CertificateError(
                translate(
                    "CertificateService",
                    "系统信任库中没有找到 {} 的 CA 证书",
                ).format(APP_NAME)
            )
