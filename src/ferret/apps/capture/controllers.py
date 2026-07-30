"""抓包 app 的控制器层 — 在 QThread 中运行轻量 CaptureMaster 并广播流量。

- 用 apps/capture/services.CaptureMaster（轻量版，去 CLI/Web 噪音）
- 复用 mitmproxy.addons.view.View 做 flow 的存储、过滤与排序
- CaptureWorker 负责线程与代理生命周期，CaptureController 负责对外生命周期管理
- UI 只连信号，不直接碰 worker / master
"""

import asyncio

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ferret.apps.capture.services import (
    CaptureMaster,
    Cert,
    Flow,
    FlowExporter,
    Options,
    View,
    _HTTPOnlyFilter,
)
from ferret.utils.proxy_manager import SystemProxyManager


class CaptureWorker(QThread):
    """mitmproxy 运行容器（轻量版）

    只负责启动/停止 CaptureMaster，不再桥接 View 信号。
    View 信号由 CaptureController 直接连接（持久化，避免重复连接）。
    """

    def __init__(self, port: int = 8080, persistent_view: View | None = None):
        super().__init__()
        self.port = port
        self.persistent_view = persistent_view
        self.master: CaptureMaster | None = None

    def run(self):
        """线程入口点"""
        try:
            asyncio.run(self._start_proxy())
        except Exception as e:  # noqa: BLE001 - 线程入口兜底，代理内核任何异常仅记录不扩散
            print(f"Mitmproxy 内核运行异常: {e}")

    async def _start_proxy(self):
        """真正的异步启动逻辑"""
        opts = Options(listen_host="127.0.0.1", listen_port=self.port)
        self.master = CaptureMaster(opts, view=self.persistent_view)

        try:
            await self.master.run()
        except asyncio.CancelledError:
            print("Mitmproxy 任务已取消")
        finally:
            print("Mitmproxy 异步循环已结束")

    def stop(self):
        if self.master:
            self.master.shutdown()
        self.quit()
        self.wait()


class CaptureController(QObject):
    """抓包控制器，管理抓包生命周期，不持有 UI 引用。"""

    # 数据信号：直接转发自 mitmproxy View
    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    master_ready = Signal(object)  # mitmproxy View 就绪

    # 状态信号
    captureStateChanged = Signal(bool)  # 抓包状态变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sniffer: CaptureWorker | None = None
        self._current_port = 8080
        # 持久化 View：跨 toggle 保留数据
        self._persistent_view = View()
        self._persistent_view.set_filter(_HTTPOnlyFilter())

        # 直接连接 View 的 mitmproxy 信号 → Qt 信号（只连接一次，永不会重复）
        # 信号在 worker 线程同步触发，Qt 信号通过 QueuedConnection 跨线程到主线程
        self._persistent_view.sig_view_add.connect(self._on_view_add)
        self._persistent_view.sig_view_update.connect(self._on_view_update)
        self._persistent_view.sig_view_remove.connect(self._on_view_remove)
        self._persistent_view.sig_view_refresh.connect(self._on_view_refresh)

        # 延迟发射 master_ready，确保 UI 信号连接已建立
        QTimer.singleShot(0, lambda: self.master_ready.emit(self._persistent_view))

    # ------------------------------------------------------------------
    # View 信号桥接（mitmproxy SyncSignal → Qt Signal）
    # ------------------------------------------------------------------
    def _on_view_add(self, flow) -> None:
        self.flow_added.emit(flow)

    def _on_view_update(self, flow) -> None:
        self.flow_updated.emit(flow)

    def _on_view_remove(self, flow, index: int) -> None:
        self.flow_removed.emit(flow, index)

    def _on_view_refresh(self) -> None:
        self.view_refreshed.emit()

    @property
    def is_capturing(self) -> bool:
        """是否正在抓包"""
        return self._sniffer is not None

    @property
    def current_port(self) -> int:
        """当前端口"""
        return self._current_port

    @property
    def view(self) -> View | None:
        """当前 mitmproxy View 实例（持久化，跨 toggle 保留）"""
        return self._persistent_view

    def start_capture(self, port: int | None = None):
        """启动抓包
        1. 判断抓包worker是否存在 有则用旧
        2. 判断是否传入端口 有则用新
        3. 设置系统代理
        4. 设置抓包线程worker（CaptureWorker）

        :param int port:监听端口，None则使用当前端口
        """
        if self._sniffer is not None:
            return

        if port is not None:
            self._current_port = port

        # 1. 启用系统代理
        SystemProxyManager.set_proxy("127.0.0.1", self._current_port)

        # 2. 启动抓包线程（View 信号已由 controller 直接连接，worker 只管跑代理）
        self._sniffer = CaptureWorker(self._current_port, self._persistent_view)
        self._sniffer.start()

    def stop_capture(self):
        """停止抓包"""
        if self._sniffer is None:
            return

        # 1. 禁用系统代理
        SystemProxyManager.unset_proxy()

        # 2. 停止抓包线程
        self._sniffer.stop()
        self._sniffer = None

    def update_port(self, new_port: int):
        """更新端口

        Args:
            new_port: 新端口
        """
        if new_port == self._current_port:
            return
        self._current_port = new_port

        # 如果正在抓包，需要重启
        if self.is_capturing:
            self.stop_capture()
            self.start_capture()

    def get_flow(self, flow_id: str) -> Flow | None:
        """按 id 从 View 中获取原始 flow"""
        view = self.view
        if view:
            return view.get_by_id(flow_id)
        return None

    def get_raw_request(self, flow_id: str) -> bytes:
        """获取原始HTTP请求"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.to_raw_request(flow_obj)
        return b""

    def get_raw_response(self, flow_id: str) -> bytes:
        """获取原始HTTP响应"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.to_raw_response(flow_obj)
        return b""

    def get_raw_flow(self, flow_id: str) -> bytes:
        """获取原始HTTP请求和响应"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.to_raw(flow_obj)
        return b""

    def toggle_capture(self) -> bool:
        """切换抓包状态，发射状态变化信号

        Returns:
            切换后是否正在抓包
        """
        if self.is_capturing:
            self.stop_capture()
            self.captureStateChanged.emit(False)
            return False
        else:
            self.start_capture()
            self.captureStateChanged.emit(True)
            return True

    def cleanup(self):
        """清理资源（应用退出时调用）"""
        if self.is_capturing:
            self.stop_capture()


class CertBadgeController(QObject):
    status_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cert = Cert()

    def refresh(self) -> bool:
        installed = self._cert.check()
        self.status_changed.emit(installed)
        return installed
