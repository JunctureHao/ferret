"""`FlowExporter` 的 body 两件：解压后口径 + 永不抛契约。

`request_body` / `response_body` 与 raw 三件是两个口径：raw 是线上字节（body 仍
压缩态），body 是 `get_content(strict=False)` 的解压内容。这里钉死三件事：
明文往返、gzip 解压（body ≠ 线上字节）、无体 / 挂起回 ``b""`` 而不是抛。

`WindowsCurlQuotingTests` 另钉 curl 导出 Windows 引号改写的契约：POSIX 单引号
参数表必须先还原再重加引号，内嵌单引号才不会把命令切碎（.plans/issues.md #10）。
`WindowsCurlReplayTests` 则按 issues #82 的验收口径，把导出命令放进**真实
cmd.exe + 真实 curl.exe** 回放到环回服务器 —— 不以 shlex 自回读代替。
"""

import gzip
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import unittest

from mitmproxy.test import tflow

from ferret.core.mitm import FlowExporter
from ferret.core.mitm.export import _to_windows_curl


class BodyExportTests(unittest.TestCase):
    def test_request_body_round_trips_plaintext(self) -> None:
        flow = tflow.tflow()
        self.assertEqual(FlowExporter.request_body(flow), b"content")

    def test_response_body_round_trips_plaintext(self) -> None:
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.content = b"hello body"
        self.assertEqual(FlowExporter.response_body(flow), b"hello body")

    def test_response_body_is_the_decompressed_bytes(self) -> None:
        """gzip 响应必须拿到解压内容 —— 这是「body ≠ 线上字节」的立身之本。"""
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"y" * 2048)

        self.assertEqual(FlowExporter.response_body(flow), b"y" * 2048)
        self.assertNotEqual(
            FlowExporter.response_body(flow),
            flow.response.raw_content,
        )

    def test_a_pending_response_yields_empty(self) -> None:
        """响应没到（挂起中）不抛 —— 界面统一走「空数据」警告路径。"""
        flow = tflow.tflow()
        self.assertEqual(FlowExporter.response_body(flow), b"")

    def test_a_bodiless_request_yields_empty(self) -> None:
        flow = tflow.tflow()
        flow.request.content = None
        self.assertEqual(FlowExporter.request_body(flow), b"")


class WindowsCurlQuotingTests(unittest.TestCase):
    """Windows 引号改写（``_to_windows_curl``）：POSIX 参数表 → MSVCRT 引号。

    断言一律用 ``shlex.split`` 吃回，POSIX / Windows 两种风格通吃，平台无关。
    """

    def test_an_embedded_apostrophe_survives_the_rewrite(self) -> None:
        """shlex.quote 对内嵌单引号用 ``'"'"'`` 续接，正则直改会整段切碎。"""
        command = "curl -H " + shlex.quote("User-Agent: O'Reilly/1.0")
        converted = _to_windows_curl(command)
        self.assertEqual(
            shlex.split(converted), ["curl", "-H", "User-Agent: O'Reilly/1.0"]
        )

    def test_an_embedded_double_quote_is_msvcrt_escaped(self) -> None:
        command = "curl -H " + shlex.quote('If-Match: "tag"')
        converted = _to_windows_curl(command)
        self.assertEqual(converted, 'curl -H "If-Match: \\"tag\\""')
        self.assertEqual(shlex.split(converted), ["curl", "-H", 'If-Match: "tag"'])

    def test_a_trailing_backslash_is_doubled_before_the_closing_quote(self) -> None:
        """尾部反斜杠不翻倍会被 CRT 读成字面引号、参数粘连。"""
        command = "curl -H " + shlex.quote("C:\\a\\")
        converted = _to_windows_curl(command)
        self.assertEqual(shlex.split(converted), ["curl", "-H", "C:\\a\\"])

    def test_cmd_metacharacters_stay_inside_quotes(self) -> None:
        """``list2cmdline`` 只对空白/引号加引号；裸 ``&`` 是 cmd 的命令分隔符。"""
        command = "curl " + shlex.quote("https://example.com/api?q=1&x=2")
        converted = _to_windows_curl(command)
        self.assertEqual(converted, 'curl "https://example.com/api?q=1&x=2"')

    def test_safe_tokens_stay_bare(self) -> None:
        """原本正确的裸 token 逐字节不变（curl / flag / 纯 ASCII URL）。"""
        converted = _to_windows_curl(
            "curl -X GET https://example.com/v1/path -H 'content-length: 0'"
        )
        self.assertEqual(
            converted,
            'curl -X GET https://example.com/v1/path -H "content-length: 0"',
        )

    def test_matches_list2cmdline_wherever_it_quotes(self) -> None:
        """空白/引号场合的转义必须与 stdlib 参考实现逐字节一致。

        只收 list2cmdline 真会加引号的参数（含空白或引号）；它裸放的
        `C:\\a\\` 由尾部反斜杠用例单独钉。
        """
        for argument in ("sp ace", 'q"q', 'a\\b" c'):
            command = "curl -d " + shlex.quote(argument)
            converted = _to_windows_curl(command)
            self.assertEqual(
                converted, "curl -d " + subprocess.list2cmdline([argument])
            )

    def test_a_metacharacter_outside_the_quote_state_is_caret_escaped(self) -> None:
        """cmd 的引号状态在每个 ``"``（含 ``\\"`` 里的）上翻转：内嵌引号会把
        ``&`` 翻到引号外，实测会被 cmd 当命令语法执行 —— 导出命令来自抓到的
        流量，这是一条真注入路径，必须补 ``^``。无空白 token 不加外引号，
        唯一的内嵌引号恰好把 ``&`` 罩进引号内，无需转义。"""
        self.assertEqual(_to_windows_curl("curl -d 'a\"b&c'"), "curl -d a\\\"b&c")
        self.assertEqual(
            _to_windows_curl("curl -d '{\"note\": \"x&y\"}'"),
            'curl -d "{\\"note\\": \\"x^&y\\"}"',
        )
        # 引号内的元字符本就是字面字符，补 ^ 反而会被 CRT 收进参数。
        self.assertEqual(_to_windows_curl("curl -d 'a&b'"), 'curl -d "a&b"')


@unittest.skipUnless(sys.platform == "win32", "导出的目标 shell 是 cmd.exe")
@unittest.skipUnless(shutil.which("curl.exe"), "需要系统自带 curl.exe")
class WindowsCurlReplayTests(unittest.TestCase):
    """导出命令的真实回放（issues #82 的验收口径）。

    cmd.exe（``shell=True`` 在 Windows 上就是它）解析导出字符串 → curl.exe 按
    CRT 规则拆 argv → 环回服务器收原始请求字节。cmd 能表达的字符必须原样到达；
    已知边界（``%VAR%`` 展开、控制字符 body）不在断言内，见 `_to_windows_curl`。
    """

    def _replay(self, flow) -> tuple[bytes, bytes]:
        """跑导出命令，返回服务器收到的 (请求头块, 请求体)。"""
        server = socket.create_server(("127.0.0.1", 0))
        port = server.getsockname()[1]
        flow.request.url = f"http://127.0.0.1:{port}{flow.request.path}"
        command = FlowExporter.curl_command(flow)

        received: dict = {}
        ready = threading.Event()

        def serve() -> None:
            server.settimeout(15)
            conn, _ = server.accept()
            try:
                conn.settimeout(15)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                head, _, rest = data.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip() or b"0")
                while len(rest) < length:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    rest += chunk
                received["head"] = head
                received["body"] = rest[:length]
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
            finally:
                ready.set()
                conn.close()
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            env = {
                key: value
                for key, value in os.environ.items()
                if "proxy" not in key.lower()
            }
            subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=20,
                env=env,
                check=False,
            )
        finally:
            if not ready.wait(15):
                server.close()
            thread.join(15)
        return received["head"], received["body"]

    def test_headers_urls_and_bodies_arrive_byte_exact(self) -> None:
        flow = tflow.tflow()
        flow.request.method = "POST"
        flow.request.path = "/v1/p?a=1&b=2"
        flow.request.content = b'{"note": "x&y"}'
        # & 与空格在双引号里必须保持字面（cmd 元字符不得漏出去拆命令），
        # 单引号走 #10 的 MSVCRT 路径，URL 里的既有转义不许二次编码。
        flow.request.headers["X-Note"] = "hello world & more"
        flow.request.headers["X-Name"] = "O'Reilly/1.0"
        # 内嵌引号把 & 翻到 cmd 引号状态外的形状：没有 ^ 转义时这条命令会在
        # 回放时被拆开（值截断，元字符后那段被当第二条命令执行）。
        flow.request.headers["X-Meta"] = 'q"1&2'

        head, body = self._replay(flow)
        request_line = head.split(b"\r\n")[0]
        self.assertEqual(request_line, b"POST /v1/p?a=1&b=2 HTTP/1.1")
        # curl 会原样保留我们的大小写；比对走 lower，避免对 curl 的头名规范化做断言。
        self.assertIn(b"x-note: hello world & more", head.lower())
        self.assertIn(b"x-name: o'reilly/1.0", head.lower())
        self.assertIn(b"x-meta: q\"1&2", head.lower())
        self.assertEqual(body, b'{"note": "x&y"}')

    def test_a_get_with_special_characters_replays_verbatim(self) -> None:
        flow = tflow.tflow()
        flow.request.method = "GET"
        flow.request.path = "/q?q=a%20b&flag"
        flow.request.content = b""

        head, body = self._replay(flow)
        request_line, *_ = head.split(b"\r\n")
        self.assertEqual(request_line, b"GET /q?q=a%20b&flag HTTP/1.1")
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
