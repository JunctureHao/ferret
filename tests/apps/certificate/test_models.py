import os
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.apps.certificate.models import (
    STATE_DETAILS,
    STATE_TITLES,
    CertificateState,
    format_fingerprint,
    format_time,
    info_rows,
)
from ferret.core.mitm import CaInfo, TrustState


def make_info(*, days: int = 3648, expired: bool = False, **overrides) -> CaInfo:
    now = datetime.now(UTC)
    fields = {
        "common_name": "Ferret",
        "organization": "Ferret",
        "subject": "CN=Ferret, O=Ferret",
        "issuer": "CN=Ferret, O=Ferret",
        "serial_hex": "0a1b2c",
        "fingerprint_sha256": "ab" * 32,
        "not_before": now - timedelta(days=2),
        # 多给 30 秒，免得 days_remaining 被执行耗时抹掉一天。
        "not_after": now + timedelta(days=days, seconds=30),
        "expired": expired,
        "key_type": "RSA",
        "key_bits": 2048,
        "is_ca": True,
        "path": Path("C:/certs/Ferret-ca-cert.pem"),
    }
    fields.update(overrides)
    return CaInfo(**fields)


class CertificateStateTests(unittest.TestCase):
    def test_default_is_missing_without_info(self) -> None:
        state = CertificateState()
        self.assertIs(state.trust, TrustState.MISSING)
        self.assertIsNone(state.info)

    def test_every_trust_state_has_title_and_detail(self) -> None:
        for trust in TrustState:
            with self.subTest(trust=trust):
                state = CertificateState(trust=trust)
                self.assertEqual(state.title, STATE_TITLES[trust])
                self.assertEqual(state.detail, STATE_DETAILS[trust])

    def test_trusted_and_healthy_uses_default_detail(self) -> None:
        state = CertificateState(TrustState.TRUSTED, make_info())
        self.assertEqual(state.detail, STATE_DETAILS[TrustState.TRUSTED])

    def test_trusted_but_expired_warns(self) -> None:
        state = CertificateState(TrustState.TRUSTED, make_info(days=-5, expired=True))
        self.assertIn("已过期", state.detail)

    def test_trusted_but_expiring_soon_shows_days(self) -> None:
        state = CertificateState(TrustState.TRUSTED, make_info(days=10))
        self.assertIn("10 天", state.detail)

    def test_expiry_warning_only_applies_when_trusted(self) -> None:
        # 没装进系统的时候，先说「要安装」比说「快过期」有用。
        state = CertificateState(TrustState.ABSENT, make_info(days=-5, expired=True))
        self.assertEqual(state.detail, STATE_DETAILS[TrustState.ABSENT])

    def test_action_availability_per_state(self) -> None:
        expected = {
            TrustState.MISSING: (True, False),
            TrustState.ABSENT: (True, False),
            TrustState.TRUSTED: (False, True),
            TrustState.STALE: (True, True),
            TrustState.UNAVAILABLE: (False, False),
        }
        for trust, (install, uninstall) in expected.items():
            with self.subTest(trust=trust):
                state = CertificateState(trust=trust)
                self.assertEqual(state.can_install, install)
                self.assertEqual(state.can_uninstall, uninstall)

    def test_only_stale_needs_reinstall(self) -> None:
        for trust in TrustState:
            with self.subTest(trust=trust):
                state = CertificateState(trust=trust)
                self.assertEqual(state.needs_reinstall, trust is TrustState.STALE)


class FormattingTests(unittest.TestCase):
    def test_fingerprint_is_grouped_and_uppercased(self) -> None:
        self.assertEqual(format_fingerprint("aabbcc"), "AA BB CC")

    def test_fingerprint_keeps_full_digest(self) -> None:
        formatted = format_fingerprint("ab" * 32)
        self.assertEqual(len(formatted.split(" ")), 32)

    def test_fingerprint_groups_are_separated_by_whitespace(self) -> None:
        # 空格是 QLabel 唯一认的断行点，指纹能不能换行全靠它。
        self.assertNotIn(":", format_fingerprint("ab" * 32))

    def test_fingerprint_tolerates_empty(self) -> None:
        self.assertEqual(format_fingerprint(""), "")

    def test_time_is_rendered_in_local_zone(self) -> None:
        moment = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        self.assertEqual(
            format_time(moment), moment.astimezone().strftime("%Y-%m-%d %H:%M")
        )


class InfoRowsTests(unittest.TestCase):
    def rows(self, **kwargs) -> dict[str, str]:
        return dict(info_rows(make_info(**kwargs)))

    def test_covers_every_native_field(self) -> None:
        rows = self.rows()
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows["通用名称"], "Ferret")
        self.assertEqual(rows["序列号"], "0a1b2c")
        self.assertEqual(rows["密钥"], "RSA 2048 位")
        self.assertEqual(rows["证书类型"], "根 CA")
        self.assertEqual(rows["文件位置"], str(Path("C:/certs/Ferret-ca-cert.pem")))

    def test_self_signed_is_marked_on_issuer(self) -> None:
        self.assertIn("（自签名）", self.rows()["颁发者"])

    def test_cross_signed_issuer_has_no_marker(self) -> None:
        rows = self.rows(issuer="CN=Other CA")
        self.assertEqual(rows["颁发者"], "CN=Other CA")

    def test_blank_fields_fall_back_to_dash(self) -> None:
        rows = self.rows(common_name="", organization="")
        self.assertEqual(rows["通用名称"], "-")
        self.assertEqual(rows["组织"], "-")

    def test_non_ca_cert_is_labelled(self) -> None:
        self.assertEqual(self.rows(is_ca=False)["证书类型"], "非 CA 证书")

    def test_validity_shows_remaining_days(self) -> None:
        self.assertIn("剩余 3648 天", self.rows()["有效期"])

    def test_validity_shows_expired(self) -> None:
        self.assertIn("已过期", self.rows(days=-5, expired=True)["有效期"])

    def test_fingerprint_row_is_grouped(self) -> None:
        self.assertEqual(self.rows()["SHA-256 指纹"], " ".join(["AB"] * 32))


if __name__ == "__main__":
    unittest.main()
