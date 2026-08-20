"""Composition root for application-scoped infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QObject

from ferret.core.log import get_logger
from ferret.core.mitm import MitmFacade, MitmRuntime
from ferret.core.network import normalize_listen_host, normalize_listen_port
from ferret.core.settings import CONFIG
from ferret.core.system_proxy import SystemProxyService

log = get_logger("application")


class ApplicationRuntime(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.mitm_runtime = self._build_mitm_runtime()
        self.mitm = MitmFacade(self.mitm_runtime)
        self.system_proxy = SystemProxyService()
        self._shutdown = False

    def _build_mitm_runtime(self) -> MitmRuntime:
        """Seed the kernel from persisted settings.

        `Application._init_config` 在建窗口之前就 `qconfig.load` 过了，所以这里读到的
        已经是落盘值。配置是纯文本、用户可能手改，一律经 normalize_* 收敛后再用。
        """
        return MitmRuntime(
            self,
            listen_host=normalize_listen_host(CONFIG.get(CONFIG.listen_host)),
            listen_port=normalize_listen_port(CONFIG.get(CONFIG.listen_port)),
            block_global=bool(CONFIG.get(CONFIG.block_global)),
            block_private=bool(CONFIG.get(CONFIG.block_private)),
        )

    def start(self) -> None:
        self._shutdown = False
        if not self.system_proxy.recover():
            log.error("failed to recover system proxy from previous run")
        self.mitm_runtime.start()

    def shutdown(self) -> bool:
        if self._shutdown:
            return True
        if not self.system_proxy.detach():
            return False
        try:
            self.mitm.stop_capture_recording()
        except Exception:
            log.exception("failed to stop native flow recording")
        runtime_stopped = self.mitm_runtime.stop()
        self._shutdown = runtime_stopped
        return runtime_stopped
