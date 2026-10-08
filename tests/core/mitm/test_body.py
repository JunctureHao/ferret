"""Body preview budgets preserve native full reads and never mutate a flow."""

from __future__ import annotations

import bz2
import gzip
import os
import subprocess
import sys
import tracemalloc
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import brotli
import zstandard
from mitmproxy.net import encoding
from mitmproxy.test import tflow

from ferret.core.mitm.bindings import bounded_content_decoding
from ferret.core.mitm.body import MAX_PREVIEW_SIZE, MAX_TEXT_SIZE, build_body
from ferret.core.mitm.detail import build_flow_body, build_flow_messages
from ferret.core.mitm.export import FlowExporter
from ferret.core.mitm.sse import SSE_EVENT_COUNT_KEY


def _compressed_flow(content: bytes, codec: str = "gzip"):
    flow = tflow.tflow(resp=True)
    assert flow.response is not None
    flow.response.headers["content-type"] = "text/plain; charset=utf-8"
    flow.response.headers["content-encoding"] = codec
    if codec == "gzip":
        compressed = gzip.compress(content)
    elif codec in ("deflate", "deflateraw"):
        compressed = zlib.compress(content)
        if codec == "deflateraw":
            compressed = compressed[2:-4]
    elif codec == "br":
        compressed = brotli.compress(content)
    elif codec == "zstd":
        compressed = zstandard.ZstdCompressor().compress(content)
    else:
        raise AssertionError(codec)
    flow.response.raw_content = compressed
    return flow


class BodyPreviewTests(unittest.TestCase):
    def test_compressed_bombs_are_bounded_and_exports_stay_complete(self):
        content = b"a" * (MAX_PREVIEW_SIZE * 8)
        for codec in ("gzip", "deflate", "deflateraw", "zstd"):
            with self.subTest(codec=codec):
                flow = _compressed_flow(content, codec)
                assert flow.response is not None
                state = flow.response.get_state()
                body = build_body(flow, flow.response)
                self.assertEqual(body["raw"], content[:MAX_PREVIEW_SIZE])
                self.assertEqual(body["text"], "a" * MAX_TEXT_SIZE)
                self.assertTrue(body["truncated"])
                self.assertTrue(body["notice"])
                self.assertIsNone(body["decoded_size"])
                self.assertIsNone(body["pretty"])
                self.assertEqual(flow.response.get_state(), state)
                self.assertEqual(FlowExporter.response_body(flow), content)
                self.assertIsNone(encoding._cache.decoded)

    def test_preview_peak_is_independent_of_large_decoded_output(self):
        flow = _compressed_flow(b"a" * (MAX_PREVIEW_SIZE * 16))
        tracemalloc.start()
        try:
            body = build_body(flow, flow.response)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(body["raw"]), MAX_PREVIEW_SIZE)
        self.assertLess(peak, 6 * MAX_PREVIEW_SIZE)

    def test_brotli_preview_is_explicitly_deferred_but_native_export_works(self):
        content = b"brotli" * 100
        flow = _compressed_flow(content, "br")
        body = build_body(flow, flow.response)
        self.assertEqual(body["raw"], b"")
        self.assertEqual(body["text"], "")
        self.assertTrue(body["notice"])
        self.assertIsNone(body["decoded_size"])
        self.assertEqual(FlowExporter.response_body(flow), content)

    def test_small_decoding_matches_native_codecs_and_charset(self):
        content = "正文 café".encode("utf-16")
        for codec in ("gzip", "deflate", "deflateraw", "zstd"):
            with self.subTest(codec=codec):
                flow = _compressed_flow(content, codec)
                assert flow.response is not None
                flow.response.headers["content-type"] = "text/plain; charset=utf-16"
                body = build_body(flow, flow.response)
                self.assertEqual(body["raw"], flow.response.get_content(strict=False))
                self.assertEqual(body["text"], flow.response.get_text(strict=False))
                self.assertEqual(body["decoded_size"], len(content))
                self.assertFalse(body["truncated"])

    def test_malformed_compression_uses_native_raw_fallback(self):
        for codec in ("gzip", "deflate"):
            flow = _compressed_flow(b"", codec)
            assert flow.response is not None
            flow.response.raw_content = b"invalid compressed data"
            body = build_body(flow, flow.response)
            self.assertEqual(body["raw"], flow.response.get_content(strict=False))

    def test_gzip_trailing_members_and_padding_match_native_decoding(self):
        # The installed native codec stops after its first gzip member, even
        # when further members/padding exist. Preview must not invent different
        # content from full export; multi-member support is an upstream limit.
        for suffix in (gzip.compress(b"b"), bytes(8), b"trailing data"):
            with self.subTest(suffix=suffix):
                flow = _compressed_flow(b"a")
                assert flow.response is not None
                flow.response.raw_content += suffix
                body = build_body(flow, flow.response)
                self.assertEqual(body["raw"], b"a")
                self.assertEqual(body["raw"], FlowExporter.response_body(flow))
        flow = _compressed_flow(b"a" * (MAX_PREVIEW_SIZE + 1))
        assert flow.response is not None
        flow.response.raw_content += gzip.compress(b"b")
        body = build_body(flow, flow.response)
        self.assertEqual(len(body["raw"]), MAX_PREVIEW_SIZE)
        self.assertTrue(body["truncated"])

    def test_large_identity_body_keeps_exact_size_without_decoding_full_text(self):
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.content = b"x" * (MAX_PREVIEW_SIZE + 99)
        body = build_flow_body(flow, "Response")
        self.assertEqual(body["res_decoded_size"], MAX_PREVIEW_SIZE + 99)
        self.assertEqual(len(body["Response Body Text"]), MAX_TEXT_SIZE)

    def test_urlencoded_form_does_not_reopen_the_original_large_body(self):
        flow = _compressed_flow(b"a=" + b"x" * (MAX_PREVIEW_SIZE * 4))
        assert flow.response is not None
        flow.request.raw_content = flow.response.raw_content
        flow.request.headers["content-encoding"] = "gzip"
        flow.request.headers["content-type"] = "application/x-www-form-urlencoded"
        native_get = type(flow.request).get_content

        def bounded_only(message, strict=True):
            self.assertIsNot(message, flow.request)
            return native_get(message, strict)

        with patch.object(type(flow.request), "get_content", bounded_only):
            body = build_flow_body(flow, "Request")
        self.assertNotIn("Request Form", body)
        self.assertTrue(body["Request Body Truncated"])
        self.assertNotIn("req_decoded_size", body)

    def test_bogus_compression_charset_does_not_bypass_budget(self):
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.content = bz2.compress(b"x" * (MAX_PREVIEW_SIZE * 8))
        flow.response.headers["content-type"] = "text/plain; charset=bz2"
        body = build_body(flow, flow.response)
        self.assertLessEqual(len(body["text"]), MAX_TEXT_SIZE)

    def test_decoding_budget_is_local_to_the_preview_context(self):
        content = b"a" * 1000
        compressed = gzip.compress(content)
        with ThreadPoolExecutor(max_workers=1) as pool, bounded_content_decoding(10):
            self.assertEqual(encoding.decode(compressed, "gzip"), content[:11])
            self.assertEqual(
                pool.submit(lambda: encoding.decode(compressed, "gzip")).result(),
                content,
            )
        self.assertEqual(encoding.decode(compressed, "gzip"), content)

    def test_large_native_encoding_results_are_not_pinned_by_shared_cache(self):
        content = b"x" * (MAX_PREVIEW_SIZE * 4)
        compressed = encoding.encode(content, "gzip")
        self.assertIsNone(encoding._cache.decoded)
        self.assertEqual(gzip.decompress(compressed), content)
        self.assertEqual(encoding.decode(compressed, "gzip"), content)
        self.assertIsNone(encoding._cache.decoded)

    def test_deep_json_does_not_enter_prettify(self):
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["content-type"] = "application/json"
        flow.response.content = b"[" * 1000 + b"0" + b"]" * 1000
        with patch(
            "ferret.core.mitm.body.contentviews.prettify_message",
            side_effect=AssertionError,
        ):
            body = build_body(flow, flow.response)
        self.assertIsNone(body["pretty"])
        self.assertEqual(body["raw"], flow.response.raw_content)

    def test_historical_sse_has_bounded_events_and_an_explicit_unknown_total(self):
        content = b"data: x\n\n" * 100000
        flow = _compressed_flow(content)
        assert flow.response is not None
        flow.response.headers["content-type"] = "text/event-stream"
        result = build_flow_messages(flow)
        self.assertLessEqual(len(result["events"]), 2000)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["count_exact"])
        self.assertTrue(result["notice"])
        self.assertLess(result["count"], 100000)
        self.assertTrue(all(event.data == "x" for event in result["events"]))
        flow.metadata[SSE_EVENT_COUNT_KEY] = 100000
        result = build_flow_messages(flow)
        self.assertEqual(result["count"], 100000)
        self.assertTrue(result["count_exact"])
        self.assertEqual(FlowExporter.response_body(flow), content)

    def test_historical_body_helper_imports_work_in_both_fresh_orders(self):
        for prefix in ("", "import ferret.core.mitm; "):
            with self.subTest(prefix=prefix):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        prefix
                        + "from ferret.utils.http_parser import build_body; assert callable(build_body)",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


class ResponseImagePreviewTests(unittest.TestCase):
    def test_image_snapshot_contains_decoded_bytes_without_live_references(self):
        image = bytes.fromhex(
            "47494638396101000100800000000000ffffff21f9040100000000"
            "2c00000000010001000002024401003b"
        )
        flow = _compressed_flow(image)
        assert flow.response is not None
        flow.response.headers["content-type"] = " Image/GIF; charset=binary "
        state = flow.response.get_state()

        body = build_flow_body(flow, "Response")

        self.assertEqual(body["Response Body Image"], image)
        self.assertIsInstance(body["Response Body Image"], bytes)
        self.assertEqual(body["Response Body Image Notice"], "")
        self.assertEqual(flow.response.get_state(), state)
        flow.response.content = b"changed"
        self.assertEqual(body["Response Body Image"], image)

    def test_image_keeps_complete_bytes_when_only_the_text_preview_is_truncated(self):
        for size in (MAX_TEXT_SIZE + 1, MAX_PREVIEW_SIZE):
            for codec in ("identity", "gzip", "deflate", "deflateraw", "zstd"):
                with self.subTest(size=size, codec=codec):
                    image = b"x" * size
                    if codec == "identity":
                        flow = tflow.tflow(resp=True)
                        assert flow.response is not None
                        flow.response.content = image
                    else:
                        flow = _compressed_flow(image, codec)
                    assert flow.response is not None
                    flow.response.headers["content-type"] = "image/png"

                    body = build_flow_body(flow, "Response")

                    self.assertEqual(body["Response Body Image"], image)
                    self.assertEqual(body["Response Body Image Notice"], "")
                    self.assertTrue(body["Response Body Truncated"])
                    self.assertEqual(len(body["Response Body Text"]), MAX_TEXT_SIZE)

    def test_image_preview_never_receives_a_partial_image(self):
        image = b"x" * (MAX_PREVIEW_SIZE + 1)
        for codec in ("identity", "gzip", "zstd"):
            with self.subTest(codec=codec):
                if codec == "identity":
                    flow = tflow.tflow(resp=True)
                    assert flow.response is not None
                    flow.response.content = image
                else:
                    flow = _compressed_flow(image, codec)
                assert flow.response is not None
                flow.response.headers["content-type"] = "image/png"

                body = build_flow_body(flow, "Response")

                self.assertIsNone(body["Response Body Image"])
                self.assertEqual(
                    body["Response Body Image Notice"],
                    "图片超过预览大小限制，请导出完整响应体查看。",
                )
                self.assertEqual(len(body["Response Body"]), MAX_PREVIEW_SIZE)
                self.assertEqual(FlowExporter.response_body(flow), image)

    def test_unsupported_image_encoding_keeps_the_decoding_notice(self):
        flow = _compressed_flow(b"image bytes", "br")
        assert flow.response is not None
        flow.response.headers["content-type"] = "image/webp"

        body = build_flow_body(flow, "Response")

        self.assertIsNone(body["Response Body Image"])
        self.assertTrue(body["Response Body Image Notice"])
        self.assertEqual(
            body["Response Body Image Notice"], body["Response Body Notice"]
        )

    def test_text_response_and_image_request_do_not_offer_an_image_preview(self):
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["content-type"] = "text/plain"
        flow.request.headers["content-type"] = "image/png"

        response = build_flow_body(flow, "Response")
        request = build_flow_body(flow, "Request")

        self.assertIsNone(response["Response Body Image"])
        self.assertEqual(response["Response Body Image Notice"], "")
        self.assertNotIn("Request Body Image", request)
        self.assertNotIn("Response Body Image", request)


if __name__ == "__main__":
    unittest.main()
