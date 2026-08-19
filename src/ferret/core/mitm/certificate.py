"""Ferret 的 CA 证书业务。

分工按 mitmproxy 的实际能力划线（`mitmproxy/certs.py`）：

- **生成 / 重新生成**：`certs.CertStore.from_store` → `create_store` → `create_ca`，
  一次写全六个产物，ferret 只负责删旧文件再调它。
- **查看**：`certs.Cert` 的现成字段（cn / organization / subject / issuer /
  notbefore / notafter / has_expired / serial / fingerprint / keyinfo / is_ca），
  这里只做「x509 对象 → 纯数据快照」的搬运，不自己解析 ASN.1。
- **导出**：`Cert.to_pem()`（PEM），`.cer` / `.p12` 直接取 `create_store` 已经写好的文件。
- **安装 / 卸载**：mitmproxy 完全不管系统信任库，只能走系统命令（Windows `certutil`）。

无 Qt：所有方法都是同步阻塞的，调用方负责挪到后台线程（见 `apps/certificate`）。
"""

from __future__ import annotations

import datetime
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ferret.core.mitm.bindings import KEY_SIZE, certs
from ferret.core.settings import APP_NAME, get_certs_dir

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


EXPORT_FORMATS: tuple[CertExportFormat, ...] = (
    CertExportFormat(
        key="pem",
        filename=CA_CERT_PEM,
        label="PEM 证书 (.pem)",
        hint="桌面浏览器、curl、OpenSSL 通用格式",
        file_filter="PEM 证书 (*.pem)",
        from_pem_api=True,
    ),
    CertExportFormat(
        key="cer",
        filename=CA_CERT_CER,
        label="CER 证书 (.cer)",
        hint="Android 设备导入用，内容与 PEM 相同",
        file_filter="CER 证书 (*.cer)",
    ),
    CertExportFormat(
        key="p12",
        filename=CA_CERT_P12,
        label="PKCS#12 证书 (.p12)",
        hint="Windows / iOS 设备导入用，不含私钥",
        file_filter="PKCS#12 证书 (*.p12)",
    ),
)


def export_format(key: str) -> CertExportFormat:
    for fmt in EXPORT_FORMATS:
        if fmt.key == key:
            return fmt
    raise CertificateError(f"未知的导出格式：{key}")


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
        raise CertutilUnavailable("当前系统上找不到 certutil 命令") from exc
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
            raise CertificateError(f"CA 证书生成失败：{exc}") from exc
        info = self.load()
        if info is None:
            raise CertificateError(f"CA 证书生成后仍读不到 {self.cert_path}")
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
                raise CertificateError(f"无法删除旧证书 {path.name}：{exc}") from exc
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
            raise CertificateError("尚未生成 CA 证书")
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
            raise CertificateError(f"导出失败：{exc}") from exc
        return target

    # --- 系统信任库（certutil） ---

    def _query(self, cert_id: str) -> bool:
        """信任库里是否存在匹配 cert_id 的证书。CertId 可以是序列号或 CN。"""
        return self._runner(["-store", "-user", _ROOT_STORE, cert_id]) == 0

    def _checked(self, args: Sequence[str], action: str) -> None:
        # Windows 退出码是 DWORD，按无符号看：0x800704c7 之类的高位码
        # 在某些环境里会以负数递过来。
        code = self._runner(args) & 0xFFFFFFFF
        if code == _ERROR_CANCELLED:
            raise CertificateCancelled(f"{action}已取消")
        if code != 0:
            raise CertificateError(f"{action}失败（certutil 退出码 0x{code:08x}）")

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
        self._checked(
            ["-addstore", "-user", _ROOT_STORE, str(self.cert_path)],
            "安装证书",
        )
        return info

    def uninstall(self) -> None:
        """从系统信任库里删掉本 CA（含历次重新生成留下的同名旧 CA）。

        `certutil -delstore` 一条都没删到时**也返回 0**（实测），所以不能拿它的
        退出码判断「删没删掉」，只能每轮先用 `-store` 查询复核。
        """
        removed = 0
        while self._query(APP_NAME):
            if removed >= _MAX_DELETE_ROUNDS:
                raise CertificateError(
                    "系统信任库中仍有残留证书，请手动检查 certmgr.msc"
                )
            self._checked(
                ["-delstore", "-user", _ROOT_STORE, APP_NAME],
                "卸载证书",
            )
            removed += 1
        if not removed:
            raise CertificateError(f"系统信任库中没有找到 {APP_NAME} 的 CA 证书")
