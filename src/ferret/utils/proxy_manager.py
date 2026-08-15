"""Compatibility wrapper for the former static proxy manager API."""

from ferret.core.system_proxy import SystemProxyService


class SystemProxyManager:
    _service = SystemProxyService()

    @classmethod
    def set_proxy(cls, host: str = "127.0.0.1", port: int = 8080) -> bool:
        try:
            cls._service.attach(host, port)
            return True
        except Exception:  # noqa: BLE001
            return False

    @classmethod
    def unset_proxy(cls) -> bool:
        return cls._service.detach()
