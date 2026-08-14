"""System trust-store integration for Ferret's mitmproxy CA."""

import subprocess

from ferret.core.mitm.bindings import KEY_SIZE, certs
from ferret.core.settings import APP_NAME, get_certs_dir


class SystemCertificateService:
    def is_installed(self) -> bool:
        try:
            result = subprocess.run(
                ["certutil", "-store", "-user", "Root"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
        return APP_NAME in result.stdout

    def check(self) -> bool:
        """Compatibility wrapper for the previous certificate API."""
        return self.is_installed()

    def install(self) -> None:
        cert_dir = get_certs_dir()
        cert_path = cert_dir / f"{APP_NAME}-ca-cert.pem"
        if not cert_path.exists():
            certs.CertStore.from_store(str(cert_dir), APP_NAME, KEY_SIZE)
        subprocess.run(
            ["certutil", "-addstore", "-user", "Root", str(cert_path)],
            capture_output=True,
            text=True,
            check=True,
        )


# Compatibility alias for existing callers. Prefer SystemCertificateService.
Cert = SystemCertificateService
