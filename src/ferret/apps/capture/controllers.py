import asyncio
import time

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from ferret.apps.capture.services import (
    CaptureMaster,
    Cert,
    FlowExporter,
    HTTPFlow,
    Options,
    ReplayHandler,
    SessionStore,
    UiBridgeAddon,
    View,
    compile_filter,
    parse_filter,
)
from ferret.core.log import get_logger
from ferret.utils.proxy_manager import SystemProxyManager

log = get_logger("mitmproxy")


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
        except Exception as e:  # noqa: BLE001
            log.error("Mitmproxy 内核运行异常: %s", e)

    async def _start_proxy(self):
        """真正的异步启动逻辑"""
        from ferret.core.settings import get_certs_dir

        opts = Options(
            listen_host="127.0.0.1",
            listen_port=self.port,
            confdir=str(get_certs_dir()),
        )
        # persistent_view 在 controller 中始终被创建，此处收窄为非空以通过类型检查
        if self.persistent_view is not None:
            view: View = self.persistent_view
            self.master = CaptureMaster(opts, view=view)
            # 注册 UI 桥接 addon：事件钩子里把原生 HTTPFlow 跨线程发给 Qt。
            # 放在 View 之后注册，保证 request 事件先落入 View 再触发桥接。
            if self.controller is not None:
                self.master.addons.add(UiBridgeAddon(view, self.controller))
                log.info("mitmproxy 线程已开启 (端口 %d)", self.port)
                try:
                    await self.master.run()
                except asyncio.CancelledError:
                    log.info("Mitmproxy 任务已取消")
                finally:
                    log.info("Mitmproxy 线程已关闭")

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
        # 基底过滤：只展示 HTTP 流量（排除 TCP/UDP/DNS）。表格模型只渲染 HTTPFlow
        # 属性，非 HTTP 流无 .request 会触发 AttributeError（见 models.PacketTableModel）。
        # 即使 _HTTPOnlyFilter 类已移除，这里仍用等价的 flowfilter "~http" 字符串保持
        # 同一语义；GUI 搜索表达式也以 ~http 为基底（见 build_filter_expression）。
        self._persistent_view.set_filter(parse_filter("~http"))

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

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        """按 id 从 View 中获取原始 flow"""
        view = self.view
        if view:
            flow = view.get_by_id(flow_id)
            if isinstance(flow, HTTPFlow):
                return flow
        return None

    def total_count(self) -> int:
        """已抓取的全部 HTTP 流量数（不受 GUI 搜索过滤影响）。

        用 ``View._store``（全量，含 TCP/UDP/DNS）里 **HTTPFlow 的数量** 作为
        「总数」分母。``_store`` 不受 ``View.set_filter``（GUI 搜索）影响，所以
        打开搜索时分母保持不变；而分子（表格可见行 = ``view._view`` 长度）会随
        搜索缩小，从而正确显示「匹配数 / 总HTTP数」（如 3/16）。

        非 HTTP 流量不计入分母——ferret 表格只展示 HTTP，它们不是用户关心的「总数」。
        """
        view = self.view
        if view is None:
            return 0
        return sum(1 for f in view._store.values() if isinstance(f, HTTPFlow))

    def apply_filter(self, conditions: list[dict] | None = None) -> None:
        """把 GUI 搜索条件编译为 flowfilter 表达式并应用到 View。

        过滤只影响显示（View._view 可见列表），不清除 _store 中的任何流量——
        清空搜索 / 切换条件后，被隐藏的 flow 仍完整保留，符合「抓全部、显示过滤」语义。

        :param conditions: MultiFilterManager.get_conditions() 返回的条件列表，
                           为空 / None 时仅保留基底 ~http（显示全部 HTTP 流量）。
        """
        view = self.view
        if view is None:
            return

        view.set_filter(compile_filter(conditions))

    def save_flows(self, flows: list[HTTPFlow], path: str) -> int:
        """保存选中的流量到文件"""
        return SessionStore.save_flows(flows, path)

    def get_httpie_command(self, flow_id: str) -> str:
        """获取 HTTPie 命令（字符串）"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.httpie_command(flow_obj)
        return ""

    def get_raw_request(self, flow_id: str) -> bytes:
        """获取原始HTTP请求"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.raw_request(flow_obj)
        return b""

    def get_raw_response(self, flow_id: str) -> bytes:
        """获取原始HTTP响应"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.raw_response(flow_obj)
        return b""

    def get_raw_flow(self, flow_id: str) -> bytes:
        """获取原始HTTP请求和响应"""
        flow_obj = self.get_flow(flow_id)
        if flow_obj:
            return FlowExporter.raw(flow_obj)
        return b""

    def replay_flow(self, flow_id: str) -> None:
        """客户端重发给真实服务器

        1. 原始flow数据copy出新的flow数据且原始数据保留
        2. 设置新flow数据 reuqst、response对象以及相关属性
        3. 获取当前时间设置 ceate、start、end 三个属性，来确保排序问题
        4. 获取master内核让 replayhandler发送给内核 进行重发

        :param flow_id: 重发流量的id
        """
        old_flow = self.get_flow(flow_id)
        if old_flow is None:
            return
        if not self.is_capturing or self._sniffer is None:
            return
        master = self._sniffer.master
        if master is None:
            return
        new_flow: HTTPFlow = old_flow.copy()
        new_flow.response = None
        new_flow.error = None
        new_flow.is_replay = "request"

        # 让新 flow 排到末尾：把时间戳设为当前时间（一定比旧 flow 晚）
        now = time.time()
        new_flow.timestamp_created = now
        new_flow.request.timestamp_start = now
        new_flow.request.timestamp_end = now

        view = self.view
        if view is None:
            return
        view.add([new_flow])
        loop = master.event_loop
        handler = ReplayHandler(new_flow, master.options)

        def _schedule():
            asyncio.ensure_future(handler.replay())

        loop.call_soon_threadsafe(_schedule)

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

    def import_flows(self, path: str):
        view = self.view
        if view is None:
            return
        flows = SessionStore.load_flows(path)
        http_flows = [f for f in flows if isinstance(f, HTTPFlow)]
        view.add(http_flows)


class CertBadgeController(QObject):
    status_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cert = Cert()

    def refresh(self) -> bool:
        installed = self._cert.check()
        self.status_changed.emit(installed)
        return installed
