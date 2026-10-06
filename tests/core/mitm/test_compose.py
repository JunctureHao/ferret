"""Tests for the compose pipeline: flow construction and the result addon."""

from __future__ import annotations

import asyncio
import http.server
import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.core.mitm import (
    CaptureMaster,
    ComposeResult,
    HTTPFlow,
    MitmFacade,
    MitmRuntime,
    Response,
    parse_filter,
)
from ferret.core.mitm.compose import (
    COMPOSE_METADATA_KEY,
    COMPOSE_RECORD_METADATA_KEY,
    ComposeAddon,
    build_compose_flow,
    compose_result,
)

from ._qt import start_runtime, wait_until


class BuildComposeFlowTests(unittest.TestCase):
    def test_request_fields_come_from_arguments(self) -> None:
        flow = build_compose_flow(
            "post",
            "http://example.com:8080/api?a=1",
            [("X-Test", "yes")],
            b"{}",
        )
        request = flow.request
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.host, "example.com")
        self.assertEqual(request.port, 8080)
        self.assertEqual(request.path, "/api?a=1")
        self.assertEqual(request.headers["X-Test"], "yes")
        self.assertEqual(request.content, b"{}")
        # Content-Length 由 Request.make 按 body 自动算出（AGENTS.md：不许手改）。
        self.assertEqual(request.headers["Content-Length"], "2")
        # Host 必须显式补：Request.make 不代写，回放路径也只在 h2/h3 转 h1 时
        # 才补 —— 缺了它 HTTP/1.1 请求一律 400（RFC 9112 §3.2）。非默认端口带端口号。
        self.assertEqual(request.headers["Host"], "example.com:8080")

    def test_https_url_sets_scheme_and_default_port(self) -> None:
        flow = build_compose_flow("GET", "https://example.com/", None, None)
        self.assertEqual(flow.request.scheme, "https")
        self.assertEqual(flow.request.port, 443)
        # 默认端口省略，与浏览器/curl 的 Host 形态一致。
        self.assertEqual(flow.request.headers["Host"], "example.com")

    def test_explicit_host_header_is_preserved(self) -> None:
        """用户显式给 Host 时以用户为准（curl 语义）；URL 不得覆写。"""
        flow = build_compose_flow(
            "GET", "https://example.com/", [("Host", "api.example.com")], None
        )
        self.assertEqual(flow.request.headers["Host"], "api.example.com")

    def test_duplicate_host_rows_last_wins(self) -> None:
        flow = build_compose_flow(
            "GET",
            "http://example.com/",
            [("Host", "a.example.com"), ("Host", "b.example.com")],
            None,
        )
        self.assertEqual(flow.request.headers["Host"], "b.example.com")

    def test_client_conn_stub_satisfies_playback_check(self) -> None:
        """ReplayHandler 要 copy client_conn 并改 state；桩连接必须撑得住。"""
        flow = build_compose_flow("GET", "http://example.com/", None, None)
        client = flow.client_conn.copy()
        self.assertIsNotNone(client.peername)
        # 与 replay_flows 里复制出来的 flow 同一形态：无响应、无错误。
        self.assertIsNone(flow.response)
        self.assertIsNone(flow.error)


class ComposeAddonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.master = CaptureMaster(event_loop=self.loop)
        self.addon = self.master.compose
        self.results: list[ComposeResult] = []
        self.addon.on_result = self.results.append

    def tearDown(self) -> None:
        self.loop.close()

    def _flow(self):
        flow = build_compose_flow("GET", "http://example.com/", None, None)
        flow.metadata[COMPOSE_METADATA_KEY] = "1"
        return flow

    def test_master_registers_compose_addon(self) -> None:
        self.assertIsInstance(self.addon, ComposeAddon)
        self.assertIn(self.addon, self.master.addons.chain)

    def test_unregistered_flow_is_ignored(self) -> None:
        flow = self._flow()
        self.addon.response(flow)
        self.assertEqual(self.results, [])

    def test_registered_flow_reports_result_once(self) -> None:
        flow = self._flow()
        self.addon.register(flow.id, keep=True)
        self.addon.response(flow)
        self.addon.error(flow)  # 第二次钩子不得再报
        self.assertEqual(len(self.results), 1)
        self.assertEqual(self.results[0].flow_id, flow.id)
        self.assertIn("Method", self.results[0].detail)

    def test_keep_false_removes_flow_from_view_on_response(self) -> None:
        flow = self._flow()
        flow.response = Response.make(200, b"{}")
        self.addon.register(flow.id, keep=False)
        self.master.view.add([flow])
        self.assertIsNotNone(self.master.view.get_by_id(flow.id))

        self.addon.response(flow)
        self.assertIsNone(self.master.view.get_by_id(flow.id))
        self.assertEqual(len(self.results), 1)

    def test_keep_true_leaves_view_alone(self) -> None:
        flow = self._flow()
        flow.response = Response.make(200, b"{}")
        self.addon.register(flow.id, keep=True)
        self.master.view.add([flow])
        self.addon.response(flow)
        self.assertIsNotNone(self.master.view.get_by_id(flow.id))
        self.assertEqual(len(self.results), 1)

    def test_compose_result_captures_error_message(self) -> None:
        from mitmproxy.flow import Error

        flow = self._flow()
        flow.error = Error("connection refused")
        result = compose_result(flow)
        self.assertEqual(result.error, "connection refused")


class FacadeSendTests(unittest.TestCase):
    """`send_custom_request` 的参数校验（不起内核的部分）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _facade(self):
        from ferret.core.mitm import MitmFacade, MitmRuntime

        return MitmFacade(MitmRuntime())

    def test_blank_method_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._facade().send_custom_request(" ", "http://example.com/")

    def test_blank_url_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._facade().send_custom_request("GET", " ")

    def test_stopped_kernel_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            self._facade().send_custom_request("GET", "http://example.com/")


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """最小回显服务器：任何请求都回 200 + JSON。

    与真实服务器一致，缺 Host 头一律 400（RFC 9112 §3.2）—— 曾有回归：
    手工 flow 不带 Host，本地 http.server 宽松放行而线上必 400。
    """

    def _reply(self) -> None:
        server = self.server
        assert isinstance(server, _EchoServer)
        # 必须消费请求体再关闭 HTTP/1.0 连接；Windows 上带着未读数据关 socket
        # 会发 RST，使同一条 POST 偶发复位或等不到回放结果。
        server.request_body = self.rfile.read(
            int(self.headers.get("Content-Length", 0))
        )
        server.request_received.set()
        # 只在服务器线程上等测试放行；Qt/内核状态仍由 _qt.wait_until 轮询。
        if not server.response_released.wait(timeout=30):
            return
        if not self.headers.get("Host"):
            self.send_response(400)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, format: str, *args) -> None:
        pass  # 安静点


class _EchoServer(http.server.HTTPServer):
    def __init__(self) -> None:
        self.request_received = threading.Event()
        self.request_body: bytes | None = None
        self.response_released = threading.Event()
        self.response_released.set()
        super().__init__(("127.0.0.1", 0), _EchoHandler)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ComposeLiveTests(unittest.TestCase):
    """端到端：编辑页发送 → 内核 → 本地服务器 → compose_result 信号回来。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.server = _EchoServer()
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        # addCleanup 是 LIFO：注册顺序必须与期望执行顺序相反 ——
        # 先 shutdown（让 serve_forever 退出）再 join，写反了 join 会永远等下去。
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

        self.runtime = MitmRuntime(listen_port=_free_port())
        self.addCleanup(self.runtime.stop)
        self.addCleanup(self.server.response_released.set)
        self.facade = MitmFacade(self.runtime)
        self.results: list[ComposeResult] = []
        self.recorded: list[HTTPFlow] = []
        self.captured: list[HTTPFlow] = []
        self.runtime.compose_result.connect(self.results.append)
        self.runtime.compose_flow_added.connect(self.recorded.append)
        self.runtime.flow_added.connect(self.captured.append)
        start_runtime(self.runtime)

    def test_send_reaches_local_server_and_reports_back(self) -> None:
        self.assertFalse(self.runtime.channels_engaged)
        url = f"http://127.0.0.1:{self.port}/api?a=1"
        flow_id = self.facade.send_custom_request(
            "POST", url, [("X-Test", "yes")], b'{"a": 1}', record=True
        )
        self.assertTrue(
            wait_until(lambda: self.results and self.recorded),
            f"runtime={self.runtime.state}, "
            f"server_received={self.server.request_received.is_set()}, "
            f"results={self.results!r}, recorded={len(self.recorded)}",
        )
        (result,) = self.results
        self.assertIsInstance(result, ComposeResult)
        self.assertEqual(result.flow_id, flow_id)
        self.assertEqual(result.error, "")
        self.assertEqual(result.detail["Status Code"], 200)
        self.assertEqual(self.server.request_body, b'{"a": 1}')
        # record=True 走独立新增信号，未「开始抓包」时也能被表格接收。
        self.assertEqual([flow.id for flow in self.recorded], [flow_id])
        self.assertEqual(self.captured, [])
        self.assertFalse(self.runtime.channels_engaged)
        self.assertEqual(self.facade.total_count(), 1)
        (flow,) = self.facade.all_http_flows()
        self.assertEqual(flow.id, flow_id)
        self.assertIs(flow.metadata[COMPOSE_RECORD_METADATA_KEY], True)

    def test_record_false_stays_hidden_until_response_then_leaves_view(self) -> None:
        self.server.response_released.clear()
        url = f"http://127.0.0.1:{self.port}/quiet"
        flow_id = self.facade.send_custom_request("GET", url, [], b"", record=False)
        self.assertTrue(wait_until(self.server.request_received.is_set))
        self.app.processEvents()
        # 原生 View 仍须持有正在发送的流量；提前 remove 会 kill 回放。
        (pending,) = self.facade.all_http_flows()
        self.assertEqual(pending.id, flow_id)
        self.assertIsNone(pending.response)
        self.assertIs(pending.metadata[COMPOSE_RECORD_METADATA_KEY], False)
        self.assertEqual(self.results, [])
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.captured, [])
        self.assertEqual(self.facade.visible_flow_rows(), [])
        self.assertEqual(self.facade.total_count(), 0)
        matcher = parse_filter("~http")
        assert matcher is not None
        self.assertEqual(self.facade.match_ids(matcher), set())
        # 改过滤器触发刷新，也不能把等待响应的隐藏条目带进表格。
        self.facade.set_filter(matcher)
        self.assertEqual(self.facade.visible_flow_rows(), [])

        self.server.response_released.set()
        self.assertTrue(wait_until(lambda: self.results))
        (result,) = self.results
        self.assertEqual(result.flow_id, flow_id)
        self.assertEqual(result.error, "")
        self.assertEqual(result.detail["Status Code"], 200)
        self.assertEqual(self.facade.all_http_flows(), [])
        self.assertEqual(self.facade.visible_flow_rows(), [])
        self.assertEqual(self.facade.total_count(), 0)
        self.assertEqual(self.facade.match_ids(matcher), set())
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.captured, [])

    def test_bypass_rule_still_reports_compose_result(self) -> None:
        """网关「绕行」命中 Compose 流量时，结果回报不得被 AddonHalt 截断。

        回归钉：绕行原本在每个钩子派发都抛 AddonHalt，链尾的 ComposeAddon
        收不到 response/error，编辑页永久停在「发送中」。
        """
        from ferret.core.mitm import (
            GatewayField,
            GatewayLayer,
            GatewayLogic,
            GatewayPolicy,
            GatewayRule,
        )

        self.facade.set_gateway_rules(
            [
                GatewayRule(
                    layer=GatewayLayer.L7,
                    policy=GatewayPolicy.BYPASS,
                    field=GatewayField.HOST,
                    logic=GatewayLogic.EQUALS,
                    value="127.0.0.1",
                )
            ]
        )
        url = f"http://127.0.0.1:{self.port}/bypassed"
        flow_id = self.facade.send_custom_request("GET", url, [], b"", record=False)
        self.assertTrue(wait_until(lambda: self.results), f"results={self.results!r}")
        (result,) = self.results
        self.assertEqual(result.flow_id, flow_id)
        self.assertEqual(result.error, "")
        self.assertEqual(result.detail["Status Code"], 200)
        # 绕行：不产生任何抓包记录（record=False 的 Compose 也一样不落地）。
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.captured, [])
        self.assertEqual(self.facade.total_count(), 0)

    def test_response_filter_delays_the_recorded_add_until_it_matches(self) -> None:
        self.server.response_released.clear()
        self.facade.set_filter(parse_filter("~s"))
        flow_id = self.facade.send_custom_request(
            "GET", f"http://127.0.0.1:{self.port}/filtered", record=True
        )
        self.assertTrue(wait_until(self.server.request_received.is_set))
        self.app.processEvents()
        self.assertEqual(self.recorded, [])
        self.assertEqual(self.facade.visible_flow_rows(), [])
        self.assertEqual(self.facade.total_count(), 1)

        self.server.response_released.set()
        self.assertTrue(wait_until(lambda: self.results and self.recorded))
        # 响应更新使原生 View 首次纳入可见集，此时仍识别为 Compose 记录。
        self.runtime.call(lambda: self.runtime.view.update(list(self.runtime.view)))
        self.app.processEvents()
        self.assertEqual([flow.id for flow in self.recorded], [flow_id])
        self.assertEqual(self.captured, [])
        self.assertEqual(len(self.results), 1)
        self.assertEqual(
            [row.id for row in self.facade.visible_flow_rows()], [flow_id]
        )


if __name__ == "__main__":
    unittest.main()
