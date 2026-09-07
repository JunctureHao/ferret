"""`FlowExporter` 的 body 两件：解压后口径 + 永不抛契约。

`request_body` / `response_body` 与 raw 三件是两个口径：raw 是线上字节（body 仍
压缩态），body 是 `get_content(strict=False)` 的解压内容。这里钉死三件事：
明文往返、gzip 解压（body ≠ 线上字节）、无体 / 挂起回 ``b""`` 而不是抛。
"""

import gzip
import unittest

from mitmproxy.test import tflow

from ferret.core.mitm import FlowExporter


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


if __name__ == "__main__":
    unittest.main()
