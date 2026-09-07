"""cURL 粘贴导入解析器的纯函数测试。

重点钉两条：一、自家 `FlowExporter.curl_command` 的 **Windows 双引号风格**导出
必须能原样吃回（roundtrip）；二、不支持的语法（`-F` / `@file` / 未知 flag）
明确报错而不是静默丢字段。
"""

import base64
import unittest

from mitmproxy.http import Headers
from mitmproxy.test import tflow

from ferret.apps.compose.curl_import import parse_curl
from ferret.core.mitm import FlowExporter


class ParseCurlTests(unittest.TestCase):
    def test_a_bare_get(self) -> None:
        edit = parse_curl("curl https://example.com/api")
        self.assertEqual(edit.method, "GET")
        self.assertEqual(edit.url, "https://example.com/api")
        self.assertEqual(edit.headers, [])
        self.assertEqual(edit.content, b"")

    def test_explicit_method_and_data(self) -> None:
        edit = parse_curl('curl -X PUT "https://example.com/api" -d "a=1"')
        self.assertEqual(edit.method, "PUT")
        self.assertEqual(edit.content, b"a=1")

    def test_data_without_method_implies_post(self) -> None:
        edit = parse_curl("curl https://example.com/api -d a=1")
        self.assertEqual(edit.method, "POST")

    def test_multiple_data_parts_join_with_ampersand(self) -> None:
        edit = parse_curl("curl https://example.com/api -d a=1 -d b=2")
        self.assertEqual(edit.content, b"a=1&b=2")

    def test_duplicate_headers_survive(self) -> None:
        edit = parse_curl(
            'curl https://example.com -H "Cookie: a=1" -H "Cookie: b=2" -H "X-Empty:"'
        )
        self.assertEqual(
            edit.headers,
            [("Cookie", "a=1"), ("Cookie", "b=2"), ("X-Empty", "")],
        )

    def test_get_flag_moves_data_into_the_query(self) -> None:
        edit = parse_curl("curl https://example.com/api?keep=1 -G -d page=2")
        self.assertEqual(edit.method, "GET")
        self.assertEqual(edit.url, "https://example.com/api?keep=1&page=2")
        self.assertEqual(edit.content, b"")

    def test_user_becomes_a_basic_authorization_header(self) -> None:
        edit = parse_curl("curl -u alice:secret https://example.com")
        expected = base64.b64encode(b"alice:secret").decode("ascii")
        self.assertEqual(edit.headers, [("Authorization", f"Basic {expected}")])

    def test_compressed_becomes_an_accept_encoding_header(self) -> None:
        """与导出侧对称：导出见到 Accept-Encoding 写 --compressed，这里还回来。"""
        edit = parse_curl("curl --compressed https://example.com")
        self.assertEqual(edit.headers, [("Accept-Encoding", "gzip, deflate")])

    def test_long_flags_accept_inline_values(self) -> None:
        edit = parse_curl(
            "curl --url=https://example.com --request=PATCH --data='{\"a\": 1}'"
        )
        self.assertEqual(edit.method, "PATCH")
        self.assertEqual(edit.url, "https://example.com")
        self.assertEqual(edit.content, b'{"a": 1}')

    def test_ignored_flags_are_consumed_with_their_values(self) -> None:
        edit = parse_curl(
            "curl -k -L -s -S -v -o out.txt -w '%{http_code}' "
            "--max-time 10 --connect-timeout 5 https://example.com"
        )
        self.assertEqual(edit.url, "https://example.com")
        self.assertEqual(edit.headers, [])

    def test_form_is_rejected_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Multipart"):
            parse_curl("curl -F file=@a.png https://example.com")

    def test_file_references_are_rejected_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "@file"):
            parse_curl("curl --data-binary @body.bin https://example.com")

    def test_data_raw_treats_an_at_sign_literally(self) -> None:
        """--data-raw 是 curl 自己绕过 @file 语法的出口，语义不能丢。"""
        edit = parse_curl("curl --data-raw @literal https://example.com")
        self.assertEqual(edit.content, b"@literal")

    def test_unknown_flags_are_rejected_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "--ntlm"):
            parse_curl("curl --ntlm https://example.com")

    def test_a_missing_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no URL"):
            parse_curl("curl -H 'Accept: json'")

    def test_non_curl_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "curl"):
            parse_curl("httpie GET https://example.com")
        with self.assertRaisesRegex(ValueError, "empty"):
            parse_curl("   ")

    def test_windows_double_quotes_and_escaped_quotes(self) -> None:
        """自家导出的 Windows 风格：双引号包裹 + `\\"` 转义，posix shlex 能吃回。"""
        edit = parse_curl(
            'curl -H "Accept: application/json" '
            '"https://example.com/api?q=a b" -d "{\\"a\\": \\"1\\"}"'
        )
        self.assertEqual(edit.url, "https://example.com/api?q=a b")
        self.assertEqual(edit.content, b'{"a": "1"}')


class ExportRoundtripTests(unittest.TestCase):
    """自家导出 → 导入 roundtrip：四元组逐项对齐。

    在 Windows 上这条同时钉住双引号改写路径；解析器吃不会自家导出就是回归。
    """

    def test_a_posted_json_flow_round_trips(self) -> None:
        flow = tflow.tflow()
        flow.request.method = "POST"
        flow.request.url = "https://api.example.com/v1?x=1"
        flow.request.headers = Headers(
            [
                (b"accept", b"application/json"),
                (b"cookie", b"a=1"),
                (b"cookie", b"b=2"),
            ]
        )
        flow.request.content = b'{"a": "1"}'

        edit = parse_curl(FlowExporter.curl_command(flow))

        self.assertEqual(edit.method, "POST")
        self.assertEqual(edit.url, "https://api.example.com/v1?x=1")
        self.assertEqual(edit.content, b'{"a": "1"}')
        self.assertIn(("accept", "application/json"), edit.headers)
        # 重复头一条都不能丢。
        self.assertIn(("cookie", "a=1"), edit.headers)
        self.assertIn(("cookie", "b=2"), edit.headers)

    def test_a_compressed_flow_round_trips_through_the_flag(self) -> None:
        """Accept-Encoding → --compressed → Accept-Encoding（映射对称即可）。"""
        flow = tflow.tflow()
        flow.request.headers = Headers([(b"accept-encoding", b"gzip")])
        flow.request.content = b""

        edit = parse_curl(FlowExporter.curl_command(flow))

        self.assertEqual(edit.method, "GET")
        self.assertIn(("Accept-Encoding", "gzip, deflate"), edit.headers)


if __name__ == "__main__":
    unittest.main()
