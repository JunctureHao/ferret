"""Tests for the raw-message preview in the flow detail panel."""

from __future__ import annotations

import gzip
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core.mitm.bindings import Response
from ferret.core.mitm.body import build_raw_preview


class _Flow:
    def __init__(self, response):
        self.request = None
        self.response = response


def _response(wire: bytes, encoding: str) -> Response:
    resp = Response.make(
        200,
        wire,
        [(b"content-type", b"application/json;charset=UTF-8")],
    )
    resp.headers["content-encoding"] = encoding
    return resp


def _gzip_response(body: bytes, encoding: str = "gzip") -> Response:
    return _response(gzip.compress(body), encoding)


class TestRawPreview(unittest.TestCase):
    def test_gzip_body_is_decoded(self):
        body = b'{"hello":"world"}'
        result = build_raw_preview(_Flow(_gzip_response(body)), "Response")
        self.assertIn('{"hello":"world"}', result["text"])
        self.assertNotIn("content-encoding", result["text"].lower())

    def test_identity_body_is_shown_as_is(self):
        resp = _response(b"plain text", encoding="")
        result = build_raw_preview(_Flow(resp), "Response")
        self.assertIn("plain text", result["text"])

    def test_unsupported_encoding_falls_back_to_wire(self):
        resp = _gzip_response(b"whatever", encoding="br")
        result = build_raw_preview(_Flow(resp), "Response")
        self.assertIn("content-encoding: br", result["text"].lower())

    def test_no_response(self):
        result = build_raw_preview(_Flow(None), "Response")
        self.assertEqual(result, {"text": "", "notice": ""})


if __name__ == "__main__":
    unittest.main()
