"""Tests for the CA-certificate service.

刻意不碰真实系统：证书一律写到临时目录，certutil 用替身 `FakeCertutil`，
绝不执行真的 `certutil`，也不读写用户的 Windows 信任库与 config.json。
"""

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from mitmproxy import certs

from ferret.core.mitm import (
    CA_ARTIFACTS,
    EXPORT_FORMATS,
    CertificateCancelled,
    CertificateError,
    CertutilUnavailable,
    SystemCertificateService,
    TrustState,
    export_format,
)
from ferret.core.mitm.certificate import (
    _MAX_DELETE_ROUNDS,
    CA_CERT_PEM,
    run_certutil,
)
from ferret.core.settings import APP_NAME

# 实测 certutil -store 查不到证书时的退出码（0x80090011 NTE_NOT_FOUND）。
NOT_FOUND = 2148073489
# 实测：安全警告里点「否」时 certutil 以 0x800704c7（ERROR_CANCELLED）退出。
CANCELLED = 0x800704C7


class FakeCertutil:
    """替身 certutil。

    信任库建模成「一串序列号」：按序列号查询只命中对应那张，按 CN（`APP_NAME`）
    查询命中任意一张 —— 这正是 `trust_state()` 能区分 TRUSTED 与 STALE 的依据。
    """

    def __init__(
        self,
        serials: Sequence[str] = (),
        *,
        delstore_deletes: bool = True,
        available: bool = True,
        addstore_code: int = 0,
        delstore_code: int = 0,
    ) -> None:
        self.serials = list(serials)
        self.calls: list[list[str]] = []
        self.delstore_deletes = delstore_deletes
        self.available = available
        self.addstore_code = addstore_code
        self.delstore_code = delstore_code

    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def count(self, verb: str) -> int:
        return self.verbs().count(verb)

    def _matches(self, cert_id: str) -> list[int]:
        if cert_id == APP_NAME:
            return list(range(len(self.serials)))
        return [i for i, serial in enumerate(self.serials) if serial == cert_id]

    def __call__(self, args: Sequence[str]) -> int:
        self.calls.append(list(args))
        if not self.available:
            raise CertutilUnavailable("测试环境没有 certutil")
        verb, cert_id = args[0], args[-1]
        if verb == "-store":
            return 0 if self._matches(cert_id) else NOT_FOUND
        if verb == "-addstore":
            if self.addstore_code:
                return self.addstore_code
            cert = certs.Cert.from_pem(Path(cert_id).read_bytes())
            self.serials.append(f"{cert.serial:x}")
            return 0
        if verb == "-delstore":
            if self.delstore_code:
                return self.delstore_code
            # 实测：一条都没删到也返回 0，所以这里无条件返回 0。
            hits = self._matches(cert_id)
            if hits and self.delstore_deletes:
                del self.serials[hits[0]]  # 真 certutil 一次删一张
            return 0
        raise AssertionError(f"unexpected certutil verb: {verb}")


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.certs_dir = Path(tmp.name)
        self.certutil = FakeCertutil()
        self.service = SystemCertificateService(self.certs_dir, runner=self.certutil)


class GenerationTests(ServiceTestCase):
    def test_ensure_writes_every_artifact(self) -> None:
        self.service.ensure()
        for name in CA_ARTIFACTS:
            with self.subTest(name=name):
                self.assertTrue((self.certs_dir / name).exists())

    def test_ensure_reuses_existing_key(self) -> None:
        first = self.service.ensure()
        self.assertEqual(self.service.ensure().serial_hex, first.serial_hex)

    def test_regenerate_replaces_key_and_serial(self) -> None:
        first = self.service.ensure()
        second = self.service.regenerate()
        self.assertNotEqual(second.serial_hex, first.serial_hex)
        self.assertNotEqual(second.fingerprint_sha256, first.fingerprint_sha256)
        for name in CA_ARTIFACTS:
            self.assertTrue((self.certs_dir / name).exists())

    def test_regenerate_works_on_empty_dir(self) -> None:
        self.assertFalse(self.service.exists())
        self.assertTrue(self.service.regenerate().is_ca)

    def test_load_returns_none_when_absent(self) -> None:
        self.assertFalse(self.service.exists())
        self.assertIsNone(self.service.load())

    def test_load_returns_none_for_corrupt_pem(self) -> None:
        (self.certs_dir / CA_CERT_PEM).write_bytes(b"not a certificate")
        self.assertIsNone(self.service.load())

    def test_require_raises_when_absent(self) -> None:
        with self.assertRaises(CertificateError):
            self.service.require()

    def test_certs_dir_defaults_to_settings(self) -> None:
        # 不传目录时才回落到 get_certs_dir()，本套测试其余用例一律显式传临时目录。
        self.assertIsNotNone(SystemCertificateService().certs_dir)


class CaInfoTests(ServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.info = self.service.ensure()

    def test_names_come_from_native_fields(self) -> None:
        self.assertEqual(self.info.common_name, APP_NAME)
        self.assertEqual(self.info.organization, APP_NAME)
        self.assertEqual(self.info.subject, f"CN={APP_NAME}, O={APP_NAME}")
        self.assertEqual(self.info.issuer, self.info.subject)
        self.assertTrue(self.info.self_signed)

    def test_key_and_ca_flags(self) -> None:
        self.assertEqual(self.info.key_type, "RSA")
        self.assertEqual(self.info.key_bits, 2048)
        self.assertTrue(self.info.is_ca)

    def test_serial_and_fingerprint_are_hex(self) -> None:
        int(self.info.serial_hex, 16)
        self.assertEqual(len(self.info.fingerprint_sha256), 64)
        int(self.info.fingerprint_sha256, 16)

    def test_validity_window(self) -> None:
        # CA_EXPIRY 10 年、CERT_VALIDITY_OFFSET -2 天 → 新证书剩 3648 天。
        self.assertFalse(self.info.expired)
        self.assertEqual(self.info.days_remaining, 3648)
        self.assertLess(self.info.not_before, self.info.not_after)
        self.assertIsNotNone(self.info.not_after.tzinfo)

    def test_path_points_at_public_cert(self) -> None:
        self.assertEqual(self.info.path, self.certs_dir / CA_CERT_PEM)


class ExportTests(ServiceTestCase):
    def test_every_format_matches_its_artifact(self) -> None:
        self.service.ensure()
        for fmt in EXPORT_FORMATS:
            with self.subTest(key=fmt.key):
                target = self.certs_dir / f"out.{fmt.key}"
                self.assertEqual(self.service.export(fmt, target), target)
                self.assertEqual(
                    target.read_bytes(), (self.certs_dir / fmt.filename).read_bytes()
                )

    def test_pem_goes_through_native_to_pem(self) -> None:
        info = self.service.ensure()
        target = self.certs_dir / "sub" / "dir" / "ca.pem"  # 顺带验证自动建目录
        self.service.export("pem", target)
        data = target.read_bytes()
        self.assertTrue(data.startswith(b"-----BEGIN CERTIFICATE-----"))
        self.assertEqual(certs.Cert.from_pem(data).serial, int(info.serial_hex, 16))

    def test_p12_is_binary_not_pem(self) -> None:
        self.service.ensure()
        target = self.certs_dir / "out.p12"
        self.service.export("p12", target)
        self.assertFalse(target.read_bytes().startswith(b"-----BEGIN"))

    def test_export_generates_when_missing(self) -> None:
        self.assertFalse(self.service.exists())
        target = self.certs_dir / "fresh.pem"
        self.service.export("pem", target)
        self.assertTrue(target.exists())
        self.assertTrue(self.service.exists())

    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(CertificateError):
            export_format("docx")
        with self.assertRaises(CertificateError):
            self.service.export("docx", self.certs_dir / "x")

    def test_no_private_key_artifact_is_exportable(self) -> None:
        # 只证书、不带私钥：导出列表里不能出现 -ca.pem / -ca.p12。
        names = {fmt.filename for fmt in EXPORT_FORMATS}
        self.assertNotIn(f"{APP_NAME}-ca.pem", names)
        self.assertNotIn(f"{APP_NAME}-ca.p12", names)


class TrustStateTests(ServiceTestCase):
    def test_missing_without_ca_file(self) -> None:
        self.assertIs(self.service.trust_state(), TrustState.MISSING)
        self.assertEqual(self.certutil.calls, [])  # 没证书就不必问系统

    def test_absent_when_store_empty(self) -> None:
        self.service.ensure()
        self.assertIs(self.service.trust_state(), TrustState.ABSENT)
        self.assertFalse(self.service.is_installed())

    def test_trusted_after_install(self) -> None:
        self.service.install()
        self.assertIs(self.service.trust_state(), TrustState.TRUSTED)
        self.assertTrue(self.service.is_installed())

    def test_stale_after_regenerate(self) -> None:
        self.service.install()
        self.service.regenerate()
        # 系统里那张旧 CA 名字还对得上，序列号已经不是磁盘上这一张了。
        self.assertIs(self.service.trust_state(), TrustState.STALE)
        self.assertFalse(self.service.is_installed())

    def test_serial_is_queried_before_common_name(self) -> None:
        info = self.service.ensure()
        self.certutil.calls.clear()
        self.service.trust_state()
        queried = [call[-1] for call in self.certutil.calls]
        self.assertEqual(queried[0], info.serial_hex)
        self.assertEqual(queried[1], APP_NAME)

    def test_unavailable_without_certutil(self) -> None:
        self.service.ensure()
        self.certutil.available = False
        self.assertIs(self.service.trust_state(), TrustState.UNAVAILABLE)
        self.assertFalse(self.service.is_installed())


class InstallTests(ServiceTestCase):
    def test_install_generates_then_adds(self) -> None:
        info = self.service.install()
        self.assertTrue(self.service.exists())
        self.assertEqual(self.certutil.serials, [info.serial_hex])
        addstore = self.certutil.calls[self.certutil.verbs().index("-addstore")]
        self.assertEqual(addstore[-1], str(self.service.cert_path))

    def test_install_targets_current_user_root_store(self) -> None:
        self.service.install()
        for call in self.certutil.calls:
            with self.subTest(call=call):
                self.assertIn("-user", call)  # 装 LocalMachine 需要管理员
                self.assertIn("Root", call)

    def test_install_purges_stale_entry_first(self) -> None:
        self.service.install()
        info = self.service.regenerate()
        self.certutil.calls.clear()
        self.service.install()
        verbs = self.certutil.verbs()
        self.assertLess(verbs.index("-delstore"), verbs.index("-addstore"))
        # 旧的清掉、新的装上，系统里只剩当前这一张。
        self.assertEqual(self.certutil.serials, [info.serial_hex])
        self.assertIs(self.service.trust_state(), TrustState.TRUSTED)

    def test_install_failure_reports_exit_code(self) -> None:
        self.certutil.addstore_code = NOT_FOUND
        with self.assertRaises(CertificateError) as ctx:
            self.service.install()
        self.assertIn(f"0x{NOT_FOUND:08x}", str(ctx.exception))

    def test_install_cancelled_by_user_is_not_a_failure(self) -> None:
        """安全警告里点「否」要能和真失败区分开，否则界面会弹一个「安装失败」。"""
        self.certutil.addstore_code = CANCELLED
        with self.assertRaises(CertificateCancelled) as ctx:
            self.service.install()
        self.assertIn("取消", str(ctx.exception))
        self.assertNotIn("失败", str(ctx.exception))
        # 仍然是 CertificateError 的子类：老的 except 分支不会漏接。
        self.assertIsInstance(ctx.exception, CertificateError)

    def test_install_cancelled_while_purging_stale_entry(self) -> None:
        """STALE 时 install 先卸旧的，卸载那一步点「否」也要整体安静中止。"""
        self.service.install()
        self.service.regenerate()
        self.certutil.delstore_code = CANCELLED
        self.certutil.calls.clear()
        with self.assertRaises(CertificateCancelled):
            self.service.install()
        self.assertEqual(self.certutil.count("-delstore"), 1)
        # 旧的没清掉就绝不能装新的：否则信任库里躺着两张同名 CA。
        self.assertEqual(self.certutil.count("-addstore"), 0)

    def test_cancelled_exit_code_is_read_as_unsigned(self) -> None:
        """Windows 的 DWORD 退出码在某些环境里是负数递过来的。"""
        signed = CANCELLED - 0x100000000
        self.certutil.addstore_code = signed
        with self.assertRaises(CertificateCancelled):
            self.service.install()

    def test_install_without_certutil_raises(self) -> None:
        self.certutil.available = False
        with self.assertRaises(CertutilUnavailable):
            self.service.install()


class UninstallTests(ServiceTestCase):
    def test_uninstall_removes_installed_ca(self) -> None:
        self.service.install()
        self.service.uninstall()
        self.assertEqual(self.certutil.serials, [])
        self.assertIs(self.service.trust_state(), TrustState.ABSENT)

    def test_uninstall_clears_every_same_name_leftover(self) -> None:
        # 信任库里可能躺着好几张同名旧 CA（历史版本安装过、或用户手动导入过），
        # install() 的清理只覆盖它自己那次，卸载必须把同名的全扫掉。
        self.service.install()
        self.certutil.serials[:0] = ["dead01", "dead02"]
        self.certutil.calls.clear()
        self.service.uninstall()
        self.assertEqual(self.certutil.serials, [])
        self.assertEqual(self.certutil.count("-delstore"), 3)

    def test_uninstall_cancelled_by_user_stops_asking(self) -> None:
        """点「否」必须跳出删除循环，否则同一个警告会被连弹 _MAX_DELETE_ROUNDS 次。"""
        self.service.install()
        self.certutil.delstore_code = CANCELLED
        self.certutil.calls.clear()
        with self.assertRaises(CertificateCancelled):
            self.service.uninstall()
        self.assertEqual(self.certutil.count("-delstore"), 1)

    def test_uninstall_raises_when_nothing_installed(self) -> None:
        self.service.ensure()
        with self.assertRaises(CertificateError) as ctx:
            self.service.uninstall()
        self.assertIn(APP_NAME, str(ctx.exception))
        self.assertEqual(self.certutil.count("-delstore"), 0)

    def test_uninstall_detects_delstore_that_deletes_nothing(self) -> None:
        # -delstore 一条没删到也返回 0，只能靠每轮的 -store 复核发现删不掉。
        self.service.install()
        self.certutil.delstore_deletes = False
        with self.assertRaises(CertificateError) as ctx:
            self.service.uninstall()
        self.assertIn("残留", str(ctx.exception))
        self.assertEqual(self.certutil.count("-delstore"), _MAX_DELETE_ROUNDS)

    def test_uninstall_succeeds_at_round_limit(self) -> None:
        # 恰好 _MAX_DELETE_ROUNDS 张时不能误报残留。
        self.service.install()
        self.certutil.serials *= _MAX_DELETE_ROUNDS
        self.service.uninstall()
        self.assertEqual(self.certutil.serials, [])


class RunCertutilTests(unittest.TestCase):
    def test_returns_exit_code_without_parsing_output(self) -> None:
        completed = mock.Mock(returncode=NOT_FOUND, stdout="证书不存在")
        with mock.patch("subprocess.run", return_value=completed) as run:
            self.assertEqual(run_certutil(["-store", "-user", "Root", "x"]), NOT_FOUND)
        self.assertEqual(
            run.call_args.args[0], ["certutil", "-store", "-user", "Root", "x"]
        )
        self.assertIs(run.call_args.kwargs["check"], False)

    def test_missing_binary_becomes_certutil_unavailable(self) -> None:
        with (
            mock.patch("subprocess.run", side_effect=FileNotFoundError),
            self.assertRaises(CertutilUnavailable),
        ):
            run_certutil(["-store"])
