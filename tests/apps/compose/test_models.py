"""Compose 请求库数据模型测试：序列化往返、from_dict 校验、create/touched/duplicate/summary。"""

from __future__ import annotations

import base64
import unittest

from ferret.apps.compose.models import (
    BODY_KINDS,
    SCHEMA_VERSION,
    SavedBody,
    SavedFormField,
    SavedRequest,
    SavedWebSocket,
    duplicate,
)


class FormFieldRoundTripTests(unittest.TestCase):
    def test_text_field_round_trip(self) -> None:
        field = SavedFormField(
            enabled=False, kind="text", name="desc", value="hello 世界"
        )
        self.assertEqual(SavedFormField.from_dict(field.to_dict()), field)

    def test_file_field_round_trip(self) -> None:
        field = SavedFormField(
            kind="file",
            name="upload",
            file_path="C:/tmp/a.bin",
            filename="a.bin",
            content_type="application/octet-stream",
        )
        self.assertEqual(SavedFormField.from_dict(field.to_dict()), field)


class BodyRoundTripTests(unittest.TestCase):
    def test_none_body_round_trip(self) -> None:
        self.assertEqual(SavedBody.from_dict(SavedBody().to_dict()), SavedBody())

    def test_text_kinds_round_trip(self) -> None:
        for kind in ("json", "text", "html", "xml"):
            with self.subTest(kind=kind):
                body = SavedBody(kind=kind, text="你好 <html>")
                self.assertEqual(SavedBody.from_dict(body.to_dict()), body)

    def test_raw_body_round_trip_as_base64(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n\xff\x00"
        body = SavedBody(kind="raw", raw_b64=base64.b64encode(payload).decode("ascii"))
        restored = SavedBody.from_dict(body.to_dict())
        self.assertEqual(restored, body)
        self.assertEqual(restored.raw_content(), payload)

    def test_urlencoded_pairs_round_trip_with_duplicates(self) -> None:
        body = SavedBody(kind="urlencoded", urlencoded=(("a", "1"), ("a", "2")))
        self.assertEqual(SavedBody.from_dict(body.to_dict()), body)

    def test_multipart_fields_round_trip(self) -> None:
        body = SavedBody(
            kind="multipart",
            multipart=(
                SavedFormField(name="meta", value="x"),
                SavedFormField(
                    kind="file",
                    name="upload",
                    file_path="D:/data/f.csv",
                    filename="f.csv",
                    content_type="text/csv",
                ),
            ),
        )
        self.assertEqual(SavedBody.from_dict(body.to_dict()), body)

    def test_file_field_round_trip(self) -> None:
        body = SavedBody(
            kind="file",
            file=SavedFormField(kind="file", file_path="D:/a.bin", filename="a.bin"),
        )
        self.assertEqual(SavedBody.from_dict(body.to_dict()), body)

    def test_text_bytes_is_utf8(self) -> None:
        self.assertEqual(SavedBody(text="你好").text_bytes(), "你好".encode())


class RequestRoundTripTests(unittest.TestCase):
    def test_full_request_round_trip(self) -> None:
        request = SavedRequest.create(
            name="下单",
            method="POST",
            url="https://api.example.com/orders",
            params=(("page", "2"), ("tag", "a b")),
            headers=(
                ("cookie", "a=1"),
                ("cookie", "b=2"),  # 合法重复名头必须保序保留
                ("x-empty", ""),
            ),
            body=SavedBody(kind="json", text='{"a": 1}'),
            record=False,
            folder="订单",
        )
        restored = SavedRequest.from_dict(request.to_dict())
        self.assertEqual(restored, request)
        self.assertEqual(
            [list(pair) for pair in restored.headers],
            [["cookie", "a=1"], ["cookie", "b=2"], ["x-empty", ""]],
        )

    def test_empty_folder_round_trip(self) -> None:
        request = SavedRequest.create(name="r", method="GET", url="https://x.test/")
        restored = SavedRequest.from_dict(request.to_dict())
        self.assertEqual(restored.folder, "")
        self.assertEqual(restored, request)

    def test_every_body_kind_serializes(self) -> None:
        for kind in BODY_KINDS:
            with self.subTest(kind=kind):
                body = SavedBody(kind=kind)
                self.assertEqual(SavedBody.from_dict(body.to_dict()).kind, kind)


class FromDictValidationTests(unittest.TestCase):
    def test_request_must_be_an_object(self) -> None:
        for bad in (None, [], "x", 42):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                SavedRequest.from_dict(bad)

    def test_missing_or_empty_required_fields(self) -> None:
        base = SavedRequest.create(
            name="r", method="GET", url="https://x.test/"
        ).to_dict()
        for key in ("id", "name", "method", "url"):
            with self.subTest(key=key):
                missing = {k: v for k, v in base.items() if k != key}
                with self.assertRaises(ValueError):
                    SavedRequest.from_dict(missing)
                empty = {**base, key: ""}
                if key == "url":
                    self.assertEqual(SavedRequest.from_dict(empty).url, "")
                else:
                    with self.assertRaises(ValueError):
                        SavedRequest.from_dict(empty)

    def test_non_string_folder_or_non_bool_record(self) -> None:
        base = SavedRequest.create(
            name="r", method="GET", url="https://x.test/"
        ).to_dict()
        with self.assertRaises(ValueError):
            SavedRequest.from_dict({**base, "folder": 1})
        with self.assertRaises(ValueError):
            SavedRequest.from_dict({**base, "record": "yes"})

    def test_non_number_updated_at(self) -> None:
        base = SavedRequest.create(
            name="r", method="GET", url="https://x.test/"
        ).to_dict()
        with self.assertRaises(ValueError):
            SavedRequest.from_dict({**base, "updated_at": "yesterday"})

    def test_invalid_body_kind(self) -> None:
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"kind": "spreadsheet"})

    def test_body_must_be_an_object(self) -> None:
        with self.assertRaises(ValueError):
            SavedBody.from_dict("not an object")

    def test_invalid_base64_raw(self) -> None:
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"kind": "raw", "raw_b64": "!!!not-base64!!!"})

    def test_non_string_text_or_raw_b64(self) -> None:
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"text": 1})
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"raw_b64": 1})

    def test_pairs_must_be_lists_of_pairs(self) -> None:
        base = SavedRequest.create(
            name="r", method="GET", url="https://x.test/"
        ).to_dict()
        for key in ("params", "headers"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    SavedRequest.from_dict({**base, key: "not-a-list"})
                with self.assertRaises(ValueError):
                    SavedRequest.from_dict({**base, key: [["only-name"]]})
                with self.assertRaises(ValueError):
                    SavedRequest.from_dict({**base, key: [[1, "v"]]})
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"urlencoded": {"a": "b"}})

    def test_form_field_validation(self) -> None:
        with self.assertRaises(ValueError):
            SavedFormField.from_dict([])
        with self.assertRaises(ValueError):
            SavedFormField.from_dict({"kind": "blob"})
        with self.assertRaises(ValueError):
            SavedFormField.from_dict({"enabled": "yes"})
        with self.assertRaises(ValueError):
            SavedFormField.from_dict({"name": 1})

    def test_body_multipart_must_be_a_list_of_valid_fields(self) -> None:
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"multipart": "nope"})
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"multipart": [{"kind": "blob"}]})

    def test_body_file_field_must_be_valid_when_present(self) -> None:
        self.assertIsNone(SavedBody.from_dict({"kind": "file"}).file)
        with self.assertRaises(ValueError):
            SavedBody.from_dict({"kind": "file", "file": {"kind": "blob"}})


class CreateTouchDuplicateTests(unittest.TestCase):
    def test_create_generates_non_empty_id_and_timestamp(self) -> None:
        request = SavedRequest.create(name="r", method="GET", url="https://x.test/")
        self.assertTrue(request.id)
        self.assertGreater(request.updated_at, 0.0)
        other = SavedRequest.create(name="r", method="GET", url="https://x.test/")
        self.assertNotEqual(request.id, other.id)

    def test_touched_keeps_id_and_refreshes_timestamp(self) -> None:
        request = SavedRequest.create(name="r", method="GET", url="https://x.test/")
        touched = request.touched(name="r2")
        self.assertEqual(touched.id, request.id)
        self.assertEqual(touched.name, "r2")
        self.assertGreaterEqual(touched.updated_at, request.updated_at)

    def test_duplicate_gets_new_id_and_name(self) -> None:
        request = SavedRequest.create(
            name="r", method="POST", url="https://x.test/a", folder="F"
        )
        copy = duplicate(request, name="r 副本")
        self.assertNotEqual(copy.id, request.id)
        self.assertEqual(copy.name, "r 副本")
        self.assertEqual(copy.method, request.method)
        self.assertEqual(copy.url, request.url)
        self.assertEqual(copy.folder, request.folder)
        self.assertGreaterEqual(copy.updated_at, request.updated_at)


class SummaryTests(unittest.TestCase):
    def test_method_and_path_from_full_url(self) -> None:
        request = SavedRequest.create(
            name="r", method="POST", url="https://api.example.com/api/orders?page=2"
        )
        self.assertEqual(request.summary(), "POST /api/orders")

    def test_url_without_path(self) -> None:
        request = SavedRequest.create(
            name="r", method="GET", url="https://api.example.com"
        )
        self.assertEqual(request.summary(), "GET /")

    def test_url_without_scheme(self) -> None:
        request = SavedRequest.create(name="r", method="PUT", url="example.com/v1/x")
        self.assertEqual(request.summary(), "PUT /v1/x")


class WebSocketDraftTests(unittest.TestCase):
    def test_draft_round_trip_and_duplicate_preserve_both_documents(self) -> None:
        draft = SavedWebSocket(("chat.v2", "chat"), "binary", " hello\r\n", "00 ff a")
        request = SavedRequest.create(
            name="chat",
            method="GET",
            url="wss://example.test/chat?q=a%20b",
            protocol="websocket",
            websocket=draft,
        )
        restored = SavedRequest.from_dict(request.to_dict())
        copied = duplicate(restored, name="copy")
        self.assertEqual(restored, request)
        self.assertEqual(copied.websocket, draft)
        self.assertNotEqual(copied.id, request.id)
        self.assertEqual(copied.protocol, "websocket")
        self.assertEqual(copied.summary(), "WS /chat")
        self.assertEqual(copied.method, "GET")

    def test_draft_validation_does_not_require_complete_message_input(self) -> None:
        for kind in ("text", "json", "binary"):
            draft = SavedWebSocket(kind=kind, text="{unfinished", binary_hex="0z")
            self.assertEqual(SavedWebSocket.from_dict(draft.to_dict()), draft)
        for data in (
            [],
            {"kind": []},
            {"kind": "file"},
            {"text": None},
            {"binary_hex": 2},
            {"subprotocols": "chat"},
            {"subprotocols": [4]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                SavedWebSocket.from_dict(data)
        data = SavedRequest.create(name="r", method="GET", url="").to_dict()
        for value in ([], "ftp", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SavedRequest.from_dict({**data, "protocol": value})

    def test_schema_supports_distinct_protocols(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
