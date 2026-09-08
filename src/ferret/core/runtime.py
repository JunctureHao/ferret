"""Composition root for application-scoped infrastructure."""

from __future__ import annotations

from PySide6.QtCore import QObject
from sysproxy import SystemProxyService

from ferret.core.log import get_logger
from ferret.core.mitm import MitmFacade, MitmRuntime, validate_local_spec
from ferret.core.network import normalize_listen_host, normalize_listen_port
from ferret.core.settings import CONFIG, get_config_dir

log = get_logger("application")


class ApplicationRuntime(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.mitm_runtime = self._build_mitm_runtime()
        self.mitm = MitmFacade(self.mitm_runtime)
        # journal 落在应用配置目录：崩溃后下次启动 `recover()` 还能找到它。
        # sysproxy 包刻意不带默认目录，路径一律由宿主注入。
        self.system_proxy = SystemProxyService(
            journal_path=get_config_dir() / "system-proxy-state.json"
        )
        self._shutdown = False

    def _build_mitm_runtime(self) -> MitmRuntime:
        """Seed the kernel from persisted settings.

        `Application._init_config` 在建窗口之前就 `qconfig.load` 过了，所以这里读到的
        已经是落盘值。配置是纯文本、用户可能手改，一律经 normalize_* 收敛后再用。

        通道开关在这里只是**落盘偏好的搬运**，不触发任何抓包动作 —— 应用启动恒为
        regular 空转，点「开始抓包」才按这些开关开启通道（见 CaptureController）。
        """
        # local 的过滤串经原生解析器收敛：坏串退回「截全部」比让启动失败友好得多，
        # 与 normalize_listen_host 同一个思路。
        local_spec = str(CONFIG.get(CONFIG.local_spec) or "")
        try:
            validate_local_spec(local_spec)
        except ValueError as exc:
            # 全量测试里可能有残留的 mitmproxy 日志 handler 指着已关闭的事件循环
            # （同 _MitmThread.run 的 try/except RuntimeError）—— 提示性日志失败
            # 不值得连坐启动。
            try:
                log.warning("本地重定向过滤串无法解析，已忽略: %s", exc)
            except RuntimeError:
                pass
            local_spec = ""
        return MitmRuntime(
            self,
            listen_host=normalize_listen_host(CONFIG.get(CONFIG.listen_host)),
            listen_port=normalize_listen_port(CONFIG.get(CONFIG.listen_port)),
            block_global=bool(CONFIG.get(CONFIG.block_global)),
            block_private=bool(CONFIG.get(CONFIG.block_private)),
            use_local=bool(CONFIG.get(CONFIG.local_enabled)),
            local_spec=local_spec,
            use_wireguard=bool(CONFIG.get(CONFIG.wireguard_enabled)),
            sticky_session_enabled=bool(CONFIG.get(CONFIG.sticky_session_enabled)),
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
