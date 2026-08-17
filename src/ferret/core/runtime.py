"""Composition root for application-scoped infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QObject

from ferret.core.log import get_logger
from ferret.core.mitm import MitmFacade, MitmRuntime
from ferret.core.system_proxy import SystemProxyService

log = get_logger("application")


class ApplicationRuntime(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.mitm_runtime = MitmRuntime(self)
        self.mitm = MitmFacade(self.mitm_runtime)
        self.system_proxy = SystemProxyService()
        self._shutdown = False

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
