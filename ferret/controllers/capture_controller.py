# ferret/ferret/controllers/capture_controller.py
from PySide6.QtCore import QObject, Signal

from ferret.core.capture import SnifferWorker, UITrafficAddon
from ferret.utils.exporter import FlowExporter
from ferret.utils.proxy_manager import SystemProxyManager


class CaptureController(QObject):
    """抓包控制器，管理抓包生命周期，不持有 UI 引用"""

    # 数据信号
    packet_received = Signal(object)
    capture_started = Signal(object)
    # 状态信号
    captureStateChanged = Signal(bool)  # 抓包状态变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sniffer: SnifferWorker | None = None
        self._current_port = 8080

    @property
    def is_capturing(self) -> bool:
        """是否正在抓包"""
        return self._sniffer is not None

    @property
    def current_port(self) -> int:
        """当前端口"""
        return self._current_port

    def start_capture(self, port: int | None = None):
        """
        启动抓包

        Args:
            port: 监听端口，None则使用当前端口

        Returns:
            是否启动成功
        """
        if self._sniffer is not None:
            return

        if port is not None:
            self._current_port = port

        # 1. 启用系统代理
        SystemProxyManager.set_proxy("127.0.0.1", self._current_port)

        # 2. 启动抓包线程
        self._sniffer = SnifferWorker(self._current_port)
        if self._sniffer is not None:
            self._sniffer.packet_captured.connect(self.packet_received)
            self._sniffer.start()
            # 发出抓包开始信号，传递 UITrafficAddon 实例
            traffic_addon = self._sniffer.get_traffic_addon()
            if traffic_addon:
                self.capture_started.emit(traffic_addon)

    def stop_capture(self):
        """
        停止抓包

        Returns:
            是否停止成功
        """
        if self._sniffer is None:
            return

        # 1. 禁用系统代理
        SystemProxyManager.unset_proxy()

        # 2. 停止抓包线程
        self._sniffer.stop()
        self._sniffer = None

    def update_port(self, new_port: int):
        """
        更新端口

        Args:
            new_port: 新端口

        Returns:
            是否更新成功
        """
        if new_port == self._current_port:
            return
        self._current_port = new_port

        # 如果正在抓包，需要重启
        if self.is_capturing:
            self.stop_capture()
            self.start_capture()

    def get_traffic_addon(self) -> UITrafficAddon | None:
        """获取 UITrafficAddon 实例（用于 flow 对象缓存）"""
        if self._sniffer:
            return self._sniffer.get_traffic_addon()
        return None
    
    def get_raw_request(self, flow_id: str) -> bytes:
        """获取原始HTTP请求"""
        traffic_addon = self.get_traffic_addon()
        if traffic_addon:
            flow_obj = traffic_addon.get_flow(flow_id)
            if flow_obj:
                return FlowExporter.to_raw_request(flow_obj)
        return b""
    
    def get_raw_response(self, flow_id: str) -> bytes:
        """获取原始HTTP响应"""
        traffic_addon = self.get_traffic_addon()
        if traffic_addon:
            flow_obj = traffic_addon.get_flow(flow_id)
            if flow_obj:
                return FlowExporter.to_raw_response(flow_obj)
        return b""
    
    def get_raw_flow(self, flow_id: str) -> bytes:
        """获取原始HTTP请求和响应"""
        traffic_addon = self.get_traffic_addon()
        if traffic_addon:
            flow_obj = traffic_addon.get_flow(flow_id)
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
