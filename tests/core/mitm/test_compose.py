"""Tests for the compose pipeline: flow construction and the result addon."""

import asyncio
import http.server
import os
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from ferret.core.mitm import (
    CaptureMaster,
    ComposeResult,
    MitmFacade,
    MitmRuntime,
    Response,
)
from ferret.core.mitm.compose import (
    COMPOSE_METADATA_KEY,
    ComposeAddon,
    build_compose_flow,
    compose_result,
)


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
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        # addCleanup 是 LIFO：注册顺序必须与期望执行顺序相反 ——
        # 先 shutdown（让 serve_forever 退出）再 join，写反了 join 会永远等下去。
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

        self.runtime = MitmRuntime(listen_port=_free_port())
        self.addCleanup(self.runtime.stop)
        self.facade = MitmFacade(self.runtime)
        self.runtime.start()
        self._wait(self.runtime.ready)

    def _wait(self, signal, timeout_ms: int = 10000) -> list:
        loop = QEventLoop()
        values = []

        def receive(*args):
            values.append(args)
            loop.quit()

        signal.connect(receive)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        signal.disconnect(receive)
        return values

    def test_send_reaches_local_server_and_reports_back(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api?a=1"
        flow_id = self.facade.send_custom_request(
            "POST", url, [("X-Test", "yes")], b'{"a": 1}', record=True
        )
        results = self._wait(self.runtime.compose_result)
        self.assertTrue(results, "compose_result 信号没有在超时内到达")
        (result,) = results[0]
        self.assertIsInstance(result, ComposeResult)
        self.assertEqual(result.flow_id, flow_id)
        self.assertEqual(result.error, "")
        self.assertEqual(result.detail["Status Code"], 200)
        # record=True：流量留在列表里。
        self.assertIsNotNone(self.runtime.view.get_by_id(flow_id))

    def test_record_false_removes_flow_from_view(self) -> None:
        url = f"http://127.0.0.1:{self.port}/quiet"
        flow_id = self.facade.send_custom_request("GET", url, [], b"", record=False)
        results = self._wait(self.runtime.compose_result)
        self.assertTrue(results, "compose_result 信号没有在超时内到达")
        self.assertIsNone(self.runtime.view.get_by_id(flow_id))


if __name__ == "__main__":
    unittest.main()
