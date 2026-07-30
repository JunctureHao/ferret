import asyncio

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ferret.apps.capture.services import (
    CaptureMaster,
    Cert,
    Flow,
    FlowExporter,
    Options,
    UiBridgeAddon,
    View,
    _HTTPOnlyFilter,
)
from ferret.utils.proxy_manager import SystemProxyManager


class CaptureWorker(QThread):
    """mitmproxy 运行容器（轻量版）

    负责启动/停止 CaptureMaster，并把 UI 桥接 addon（UiBridgeAddon）注册进
    mitmproxy 事件循环——这样流量事件由真正的 addon 钩子捕获并跨线程发给 Qt。
    """

    def __init__(
        self,
        port: int = 8080,
        persistent_view: View | None = None,
        controller: "CaptureController | None" = None,
    ):
        super().__init__()
        self.port = port
        self.persistent_view = persistent_view
        self.controller = controller
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
        # persistent_view 在 controller 中始终被创建，此处收窄为非空以通过类型检查
        if self.persistent_view is not None:
            view: View = self.persistent_view
            self.master = CaptureMaster(opts, view=view)
            # 注册 UI 桥接 addon：事件钩子里把原生 HTTPFlow 跨线程发给 Qt。
            # 放在 View 之后注册，保证 request 事件先落入 View 再触发桥接。
            if self.controller is not None:
                self.master.addons.add(UiBridgeAddon(view, self.controller))
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
        # 持久化 View：跨 toggle 保留数据（同时作为表格模型的存储/排序/过滤后端）
        self._persistent_view = View()
        self._persistent_view.set_filter(_HTTPOnlyFilter())

        # 流量事件改由 UiBridgeAddon（真正的 mitmproxy addon）在钩子里
        # 直接 emit 本 controller 的 4 个信号，不再直接连 View 的 SyncSignal。
        # addon 在 CaptureWorker._start_proxy 中注册，桥接对象即本 controller。

        # 延迟发射 master_ready，确保 UI 信号连接已建立
        QTimer.singleShot(0, lambda: self.master_ready.emit(self._persistent_view))

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

        # 2. 启动抓包线程（UiBridgeAddon 在 worker 内注册，事件经 addon 钩子
        #    转发为本 controller 的 Qt 信号；worker 只管跑代理）
        self._sniffer = CaptureWorker(self._current_port, self._persistent_view, self)
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
