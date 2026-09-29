"""`FlowExporter` 的 body 两件：解压后口径 + 永不抛契约。

`request_body` / `response_body` 与 raw 三件是两个口径：raw 是线上字节（body 仍
压缩态），body 是 `get_content(strict=False)` 的解压内容。这里钉死三件事：
明文往返、gzip 解压（body ≠ 线上字节）、无体 / 挂起回 ``b""`` 而不是抛。

`WindowsCurlQuotingTests` 另钉 curl 导出 Windows 引号改写的契约：POSIX 单引号
参数表必须先还原再重加引号，内嵌单引号才不会把命令切碎（.plans/issues.md #10）。
"""

import gzip
import shlex
import subprocess
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


if __name__ == "__main__":
    unittest.main()
