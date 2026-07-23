"""抓包 app 的控制器层 — 在 QThread 中运行轻量 CaptureMaster 并广播流量。

- 用 apps/capture/services.CaptureMaster（轻量版，去 CLI/Web 噪音）
- 自带 UITrafficAddon（流量预处理 + Signal 广播）
- CaptureWorker 负责线程与代理生命周期，CaptureController 负责对外生命周期管理
- UI 只连信号，不直接碰 worker / master
"""

import asyncio
from datetime import datetime
from typing import Any, Dict

from mitmproxy.http import HTTPFlow
from mitmproxy.options import Options
from PySide6.QtCore import QObject, QThread, Signal, SignalInstance

from ferret.apps.capture.services import CaptureMaster
from ferret.utils.exporter import FlowExporter
from ferret.utils.http_parser import (
    build_body,
    parse_cookies_from_headers,
    parse_params,
)
from ferret.utils.process_resolver import resolve_process
from ferret.utils.proxy_manager import SystemProxyManager


class UITrafficAddon:
    """在 mitmproxy 线程中预处理 flow 对象为字典"""

    def __init__(self, signal: SignalInstance):
        self.signal = signal
        self._flow_cache: Dict[str, Any] = {}  # 缓存 flow 对象用于导出
        self._max_cache_size = 1000  # 最大缓存大小

    def requestheaders(self, flow: HTTPFlow):
        data = self._preprocess_flow(flow, "request_headers")
        self.signal.emit(data)

    def request(self, flow: HTTPFlow):
        data = self._preprocess_flow(flow, "request")
        self.signal.emit(data)

    def responseheaders(self, flow: HTTPFlow):
        data = self._preprocess_flow(flow, "response_headers")
        self.signal.emit(data)

    def response(self, flow: HTTPFlow):
        data = self._preprocess_flow(flow, "complete")
        self.signal.emit(data)

    def error(self, flow: HTTPFlow):
        data = self._preprocess_flow(flow, "error")
        self.signal.emit(data)

    def _preprocess_flow(self, flow: HTTPFlow, state: str) -> Dict[str, Any]:
        flow_id = flow.id
        self._cache_flow(flow_id, flow)

        data: Dict[str, Any] = {"id": flow_id, "state": state}

        if state in (
            "request_headers",
            "request",
            "response_headers",
            "complete",
            "error",
        ):
            keep_alive = flow.request.headers.get("keep-alive", None)
            if keep_alive is None and flow.request.http_version == "HTTP/1.1":
                keep_alive = "true"
            elif keep_alive is None:
                keep_alive = "false"

            client_addr = flow.client_conn.peername if flow.client_conn else None
            proc_info = resolve_process(client_addr) if client_addr else None
            app = proc_info.to_dict() if proc_info else {}

            client_pn = flow.client_conn.peername if flow.client_conn else None
            client_sn = (
                getattr(flow.client_conn, "sockname", None)
                if flow.client_conn
                else None
            )
            conn_time = ""
            if flow.request.timestamp_start:
                conn_time = datetime.fromtimestamp(
                    flow.request.timestamp_start
                ).strftime("%Y-%m-%d %H:%M:%S.%f")

            data.update(
                {
                    "Method": flow.request.method,
                    "URL": flow.request.pretty_url,
                    "Host": flow.request.host,
                    "Path": flow.request.path,
                    "Scheme": flow.request.scheme,
                    "HTTP Version": flow.request.http_version,
                    "Request Headers": dict(flow.request.headers),
                    "req_time": flow.request.timestamp_start,
                    "req_timestamp_end": flow.request.timestamp_end,
                    "req_headers_size": len(str(flow.request.headers)),
                    "Status Code": "等待中...",
                    "Keep Alive": keep_alive,
                    **app,
                    "Connection ID": flow.id,
                    "Connection Time": conn_time,
                    "Front Client Address": client_pn[0] if client_pn else "N/A",
                    "Front Client Port": client_pn[1] if client_pn else "N/A",
                    "Front Server Address": client_sn[0] if client_sn else "N/A",
                    "Front Server Port": client_sn[1] if client_sn else "N/A",
                }
            )

            data["Request Params"] = parse_params(flow.request.url)
            data["Request Cookies"] = parse_cookies_from_headers(
                dict(flow.request.headers), "Cookie"
            )

        if state in ("request", "response_headers", "complete", "error"):
            body = flow.request.raw_content or b""
            req_duration = None
            if flow.request.timestamp_end and flow.request.timestamp_start:
                req_duration = (
                    flow.request.timestamp_end - flow.request.timestamp_start
                ) * 1000
            req_ct = flow.request.headers.get("Content-Type", "-")
            req_body_info = build_body(body, req_ct)
            data.update(
                {
                    "req_size": len(body),
                    "req_duration": req_duration,
                    "Request Body": body,
                    "Request Content-Type": req_ct,
                    "Request Body Text": req_body_info["text"],
                    "Request Body Pretty": req_body_info["pretty"],
                    "Request Fold Regions": req_body_info["fold_regions"],
                    "Request Is Binary": req_body_info["is_binary"],
                    "Request Body MIME": req_body_info["mime"],
                }
            )

        if state in ("response_headers", "complete", "error"):
            if flow.response:
                data["Response Cookies"] = parse_cookies_from_headers(
                    dict(flow.response.headers), "Set-Cookie"
                )

                server_addr = "N/A"
                if flow.server_conn and flow.server_conn.peername:
                    server_addr = (
                        f"{flow.server_conn.peername[0]}:{flow.server_conn.peername[1]}"
                    )

                protocol = flow.request.http_version
                if flow.server_conn and flow.server_conn.alpn:
                    protocol = flow.server_conn.alpn.decode()

                proxy_protocol = "http"
                if (
                    flow.server_conn
                    and hasattr(flow.server_conn, "tls_established")
                    and flow.server_conn.tls_established
                ):
                    proxy_protocol = "https"

                server_pn = flow.server_conn.peername if flow.server_conn else None
                server_sn = (
                    getattr(flow.server_conn, "source_address", None)
                    if flow.server_conn
                    else None
                )

                data.update(
                    {
                        "Status Code": flow.response.status_code,
                        "Reason": flow.response.reason,
                        "Response Headers": dict(flow.response.headers),
                        "Response HTTP Version": flow.response.http_version,
                        "Server Address": server_addr,
                        "Protocol": protocol,
                        "res_headers_size": len(str(flow.response.headers)),
                        "res_timestamp_start": flow.response.timestamp_start,
                        "Proxy Protocol": proxy_protocol,
                        "Back Client Address": server_sn[0] if server_sn else "N/A",
                        "Back Client Port": server_sn[1] if server_sn else "N/A",
                        "Back Server Address": server_pn[0] if server_pn else "N/A",
                        "Back Server Port": server_pn[1] if server_pn else "N/A",
                    }
                )

                conn = flow.server_conn
                if conn and getattr(conn, "tls_established", False):
                    tls_info = {
                        "TLS Version": getattr(conn, "tls_version", "N/A"),
                        "TLS SNI": getattr(conn, "sni", "N/A"),
                        "TLS ALPN Offers": [
                            a.decode() if isinstance(a, bytes) else str(a)
                            for a in getattr(conn, "alpn_offers", []) or []
                        ],
                        "TLS ALPN Selected": (
                            conn.alpn.decode() if conn.alpn else "N/A"
                        ),
                        "TLS Cipher": getattr(conn, "cipher", "N/A"),
                        "TLS Cipher List": list(getattr(conn, "cipher_list", []) or []),
                    }
                    if hasattr(conn, "certificate_list") and conn.certificate_list:
                        server_cert = conn.certificate_list[0]
                        if server_cert:
                            tls_info["Not Before"] = server_cert.notbefore.strftime(
                                "%Y-%m-%d %H:%M:%S.000"
                            )
                            tls_info["Not After"] = server_cert.notafter.strftime(
                                "%Y-%m-%d %H:%M:%S.000"
                            )
                    data.update(tls_info)

        if state in ("complete", "error"):
            if flow.response:
                duration = (flow.response.timestamp_end or 0) - (
                    flow.request.timestamp_start or 0
                )
                res_duration = None
                if flow.response.timestamp_end and flow.response.timestamp_start:
                    res_duration = (
                        flow.response.timestamp_end - flow.response.timestamp_start
                    ) * 1000
                body = flow.response.raw_content or b""
                req_total_size = data.get("req_headers_size", 0) + data.get(
                    "req_size", 0
                )
                res_total_size = data.get("res_headers_size", 0) + len(body)
                total_size = req_total_size + res_total_size
                res_ct = flow.response.headers.get("Content-Type", "-")
                res_body_info = build_body(body, res_ct)
                data.update(
                    {
                        "Response Body": body,
                        "Response Content-Type": res_ct,
                        "Response Body Text": res_body_info["text"],
                        "Response Body Pretty": res_body_info["pretty"],
                        "Response Fold Regions": res_body_info["fold_regions"],
                        "Response Is Binary": res_body_info["is_binary"],
                        "Response Body MIME": res_body_info["mime"],
                        "res_size": len(body),
                        "res_time": flow.response.timestamp_end,
                        "res_duration": res_duration,
                        "Duration": f"{duration * 1000:.0f} ms",
                        "total_size": total_size,
                        "TLS Version": getattr(flow.server_conn, "tls_version", "N/A")
                        if flow.server_conn
                        else "N/A",
                    }
                )

        if state == "error":
            data.update(
                {
                    "Status Code": "Error",
                    "Error Message": flow.error.msg if flow.error else "Unknown",
                }
            )

        if state == "complete":
            try:
                data["curl_command"] = FlowExporter.to_curl(flow)
            except Exception as e:
                print(f"生成 cURL 命令失败: {e}")
                data["curl_command"] = f"Error generating curl command: {e}"

        return data

    def _cache_flow(self, flow_id: str, flow: HTTPFlow):
        if len(self._flow_cache) >= self._max_cache_size:
            oldest_key = next(iter(self._flow_cache))
            del self._flow_cache[oldest_key]
        self._flow_cache[flow_id] = flow

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        return self._flow_cache.get(flow_id)


class CaptureWorker(QThread):
    """mitmproxy 运行容器（轻量版）"""

    packet_captured = Signal(object)

    def __init__(self, port: int = 8080):
        super().__init__()
        self.port = port
        self.master = None
        self._traffic_addon = None

    def run(self):
        """线程入口点"""
        try:
            asyncio.run(self._start_proxy())
        except Exception as e:
            print(f"Mitmproxy 内核运行异常: {e}")

    async def _start_proxy(self):
        """真正的异步启动逻辑"""
        opts = Options(listen_host="127.0.0.1", listen_port=self.port)
        self.master = CaptureMaster(opts)

        self._traffic_addon = UITrafficAddon(self.packet_captured)
        self.master.addons.add(self._traffic_addon)

        try:
            await self.master.run()
        except asyncio.CancelledError:
            print("Mitmproxy 任务已取消")
        finally:
            print("Mitmproxy 异步循环已结束")

    def get_traffic_addon(self) -> UITrafficAddon | None:
        return self._traffic_addon

    def stop(self):
        if self.master:
            self.master.shutdown()
        self.quit()
        self.wait()


class CaptureController(QObject):
    """抓包控制器，管理抓包生命周期，不持有 UI 引用。

    由旧的 ferret.controllers.capture_controller.CaptureController 迁入，
    并将其内部的 SnifferWorker / UITrafficAddon 替换为 apps/capture 内部的
    CaptureWorker。
    """

    # 数据信号
    packet_received = Signal(object)
    capture_started = Signal(object)
    # 状态信号
    captureStateChanged = Signal(bool)  # 抓包状态变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sniffer: CaptureWorker | None = None
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
        """
        if self._sniffer is not None:
            return

        if port is not None:
            self._current_port = port

        # 1. 启用系统代理
        SystemProxyManager.set_proxy("127.0.0.1", self._current_port)

        # 2. 启动抓包线程
        self._sniffer = CaptureWorker(self._current_port)
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
