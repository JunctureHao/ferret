import asyncio
from datetime import datetime
from typing import Any, Dict

from mitmproxy.http import HTTPFlow
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster
from PySide6.QtCore import QThread, Signal, SignalInstance

from ferret.utils.exporter import FlowExporter
from ferret.utils.process_resolver import resolve_process


class SnifferAddon:
    def __init__(self, signal: SignalInstance):
        self.signal = signal

    # ── 阶段 ①：请求头到达 ──
    def requestheaders(self, flow: HTTPFlow):
        self.signal.emit((flow, "request_headers"))

    # ── 阶段 ②：请求体到达 ──
    def request(self, flow: HTTPFlow):
        self.signal.emit((flow, "request"))

    # ── 阶段 ③：响应头到达 ← 关键改进 ──
    def responseheaders(self, flow: HTTPFlow):
        self.signal.emit((flow, "response_headers"))

    # ── 阶段 ④：响应体到达 ──
    def response(self, flow: HTTPFlow):
        self.signal.emit((flow, "complete"))

    # ── 阶段 ⑤：错误 ──
    def error(self, flow: HTTPFlow):
        self.signal.emit((flow, "error"))


class UITrafficAddon:
    """在 mitmproxy 线程中预处理 flow 对象为字典"""

    def __init__(self, signal: SignalInstance):
        self.signal = signal
        self._flow_cache: Dict[str, Any] = {}  # 缓存 flow 对象用于导出
        self._max_cache_size = 1000  # 最大缓存大小

    def requestheaders(self, flow: HTTPFlow):
        """预处理请求头，生成显示字典和 cURL 命令"""
        data = self._preprocess_flow(flow, "request_headers")
        self.signal.emit(data)

    def request(self, flow: HTTPFlow):
        """预处理请求体"""
        data = self._preprocess_flow(flow, "request")
        self.signal.emit(data)

    def responseheaders(self, flow: HTTPFlow):
        """预处理响应头"""
        data = self._preprocess_flow(flow, "response_headers")
        self.signal.emit(data)

    def response(self, flow: HTTPFlow):
        """预处理响应体，生成完整的显示数据"""
        data = self._preprocess_flow(flow, "complete")
        self.signal.emit(data)

    def error(self, flow: HTTPFlow):
        """预处理错误状态"""
        data = self._preprocess_flow(flow, "error")
        self.signal.emit(data)

    def _preprocess_flow(self, flow: HTTPFlow, state: str) -> Dict[str, Any]:
        """预处理 flow 对象为字典，包含所有显示字段和 cURL 命令"""
        flow_id = flow.id

        # 缓存 flow 对象（用于可能的导出）
        self._cache_flow(flow_id, flow)

        # 基础数据
        data = {
            "id": flow_id,
            "state": state,
        }

        # 请求阶段
        if state in (
            "request_headers",
            "request",
            "response_headers",
            "complete",
            "error",
        ):
            # Keep Alive
            keep_alive = flow.request.headers.get("keep-alive", None)
            if keep_alive is None and flow.request.http_version == "HTTP/1.1":
                keep_alive = "true"
            elif keep_alive is None:
                keep_alive = "false"

            # 进程信息
            client_addr = flow.client_conn.peername if flow.client_conn else None
            proc_info = resolve_process(client_addr)
            app = proc_info.to_dict() if proc_info else {}

            # 连接信息
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

        # 请求体阶段
        if state in ("request", "response_headers", "complete", "error"):
            body = flow.request.raw_content or b""
            req_duration = None
            if flow.request.timestamp_end and flow.request.timestamp_start:
                req_duration = (
                    flow.request.timestamp_end - flow.request.timestamp_start
                ) * 1000  # 毫秒
            data.update(
                {
                    "req_size": len(body),
                    "req_duration": req_duration,
                    "Request Body": body,
                    "Request Content-Type": flow.request.headers.get(
                        "Content-Type", "-"
                    ),
                }
            )

        # 响应头阶段
        if state in ("response_headers", "complete", "error"):
            if flow.response:
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

                # TLS 信息
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

                    # 证书信息（简化版，完整版参考原代码）
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

        # 响应体阶段
        if state in ("complete", "error"):
            if flow.response:
                duration = (flow.response.timestamp_end or 0) - (
                    flow.request.timestamp_start or 0
                )
                res_duration = None
                if flow.response.timestamp_end and flow.response.timestamp_start:
                    res_duration = (
                        flow.response.timestamp_end - flow.response.timestamp_start
                    ) * 1000  # 毫秒
                body = flow.response.raw_content or b""
                # 计算总大小
                req_total_size = data.get("req_headers_size", 0) + data.get(
                    "req_size", 0
                )
                res_total_size = data.get("res_headers_size", 0) + len(body)
                total_size = req_total_size + res_total_size
                data.update(
                    {
                        "Response Body": body,
                        "Response Content-Type": flow.response.headers.get(
                            "Content-Type", "-"
                        ),
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

        # 错误状态
        if state == "error":
            data.update(
                {
                    "Status Code": "Error",
                    "Error Message": flow.error.msg if flow.error else "Unknown",
                }
            )

        # 预生成 cURL 命令（仅在完整状态时生成）
        if state == "complete":
            try:
                # 使用 FlowExporter 生成 cURL 命令
                curl_cmd = FlowExporter.to_curl(flow)
                data["curl_command"] = curl_cmd
            except Exception as e:
                print(f"生成 cURL 命令失败: {e}")
                data["curl_command"] = f"Error generating curl command: {e}"

        return data

    def _cache_flow(self, flow_id: str, flow: HTTPFlow):
        """缓存 flow 对象，带大小限制"""
        # 如果缓存已满，移除最旧的条目（简单实现）
        if len(self._flow_cache) >= self._max_cache_size:
            # 移除第一个插入的条目（近似 LRU）
            oldest_key = next(iter(self._flow_cache))
            del self._flow_cache[oldest_key]

        self._flow_cache[flow_id] = flow

    def get_flow(self, flow_id: str) -> HTTPFlow:
        """获取缓存的 flow 对象（用于导出）"""
        return self._flow_cache.get(flow_id)


class SnifferWorker(QThread):
    """mitmproxy 运行容器"""

    packet_captured = Signal(object)

    def __init__(self, port=8080):
        super().__init__()
        self.port = port
        self.master = None
        self._traffic_addon = None  # 保存 UITrafficAddon 实例

    def run(self):
        """线程入口点"""
        # 使用 asyncio.run 开启一个真正的异步运行环境
        try:
            asyncio.run(self._start_proxy())
        except Exception as e:
            # 捕获可能的异常，防止线程崩溃导致主程序闪退
            print(f"Mitmproxy 内核运行异常: {e}")

    async def _start_proxy(self):
        """真正的异步启动逻辑"""
        opts = Options(listen_host="127.0.0.1", listen_port=self.port)

        # 在异步环境下初始化 Master
        self.master = DumpMaster(opts)

        # 注入插件
        # 注意：这里的 self.packet_captured 在运行时是 SignalInstance
        self._traffic_addon = UITrafficAddon(self.packet_captured)
        self.master.addons.add(self._traffic_addon)

        try:
            # 开始异步运行
            await self.master.run()
        except asyncio.CancelledError:
            print("Mitmproxy 任务已取消")
        finally:
            print("Mitmproxy 异步循环已结束")

    def get_traffic_addon(self):
        """获取 UITrafficAddon 实例"""
        return self._traffic_addon

    def stop(self):
        if self.master:
            self.master.shutdown()
        self.quit()
        self.wait()
