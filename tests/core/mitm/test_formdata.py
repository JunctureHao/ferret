"""Multipart editing preserves payload bytes and rejects unrepresentable input."""

from __future__ import annotations

import os
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core.mitm import (
    FormField,
    encode_multipart,
    multipart_boundary,
    parse_multipart,
    read_body_file,
)


def _body(part: bytes, boundary: bytes = b"test") -> bytes:
    return b"--" + boundary + b"\r\n" + part + b"\r\n--" + boundary + b"--\r\n"


class MultipartEncodingTests(unittest.TestCase):
    def test_unicode_duplicate_fields_and_binary_file_match_an_independent_parser(self):
        payload = b"\x00\xff\xfe\r\n\r\n\x89PNG\nlast\r\n"
        fields = [
            FormField(name="同名", value="中文\r\nsecond\nthird\r"),
            FormField(name="同名", value="末尾 "),
            FormField(name="", value="empty name"),
            FormField(
                kind="file",
                name="upload",
                filename="截图.bin",
                content_type="application/octet-stream",
                payload=payload,
            ),
        ]
        content, content_type = encode_multipart(fields, "known-boundary")

        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
        )
        self.assertEqual(message.defects, [])
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 4)
        self.assertEqual(
            parts[0].get_param("name", header="content-disposition"), "同名"
        )
        self.assertEqual(parts[0].get_payload(decode=True), fields[0].value.encode())
        self.assertEqual(parts[1].get_payload(decode=True), fields[1].value.encode())
        self.assertEqual(parts[2].get_param("name", header="content-disposition"), "")
        self.assertEqual(parts[3].get_filename(), "截图.bin")
        self.assertEqual(parts[3].get_payload(decode=True), payload)
        self.assertEqual(parse_multipart(content, content_type), fields)

    def test_quotes_and_backslashes_are_escaped_without_changing_parameter_values(self):
        field = FormField(
            kind="file",
            name='a"b\\c',
            filename='file"\\.bin',
            content_type="application/octet-stream",
            payload=b"",
        )
        content, content_type = encode_multipart([field], "safe")
        self.assertIn(b'name="a\\"b\\\\c"', content)
        self.assertIn(b'filename="file\\"\\\\.bin"', content)
        self.assertEqual(parse_multipart(content, content_type), [field])

    def test_no_fields_and_disabled_invalid_files_make_a_valid_empty_form(self):
        content, content_type = encode_multipart(
            [FormField(enabled=False, kind="file", file_path="missing")], "empty"
        )
        self.assertEqual(content, b"--empty--\r\n")
        self.assertEqual(parse_multipart(content, content_type), [])

    def test_selected_empty_file_differs_from_unselected_file(self):
        content, content_type = encode_multipart(
            [FormField(kind="file", filename="", payload=b"")], "empty-file"
        )
        fields = parse_multipart(content, content_type)
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields[0].payload, b"")
        self.assertEqual(fields[0].kind, "file")
        self.assertEqual(fields[0].filename, "")
        with self.assertRaisesRegex(ValueError, "尚未选择文件"):
            encode_multipart([FormField(kind="file", name="upload")])

    def test_boundary_is_fresh_for_each_send_and_quoted_when_needed(self):
        _, first_type = encode_multipart([])
        _, second_type = encode_multipart([])
        self.assertNotEqual(first_type, second_type)
        self.assertRegex(first_type, r"^multipart/form-data; boundary=[0-9a-f]{32}$")
        content, content_type = encode_multipart([FormField(value="x")], "has space:ok")
        self.assertEqual(content_type, 'multipart/form-data; boundary="has space:ok"')
        self.assertEqual(multipart_boundary(content_type), "has space:ok")
        self.assertEqual(parse_multipart(content, content_type), [FormField(value="x")])

    def test_user_boundary_collision_fails_and_generated_boundary_is_retried(self):
        field = FormField(value="before\r\n--collision\r\nafter")
        with self.assertRaisesRegex(ValueError, "boundary 冲突"):
            encode_multipart([field], "collision")
        with patch(
            "ferret.core.mitm.body.secrets.token_hex", side_effect=["collision", "safe"]
        ):
            content, content_type = encode_multipart([field])
        self.assertEqual(multipart_boundary(content_type), "safe")
        self.assertEqual(parse_multipart(content, content_type), [field])

    def test_boundary_prefixes_are_payload_but_padded_delimiters_are_collisions(self):
        field = FormField(value="before\r\n--testX\r\n--test--suffix\r\nafter")
        content, content_type = encode_multipart([field], "test")
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
        )
        parts = list(message.iter_parts())
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].get_payload(decode=True), field.value.encode())
        self.assertEqual(parse_multipart(content, content_type), [field])
        for delimiter in ("--test \t\r\n", "--test--\t\r\n", "--test"):
            with (
                self.subTest(delimiter=delimiter),
                self.assertRaisesRegex(ValueError, "boundary 冲突"),
            ):
                encode_multipart([FormField(value="before\r\n" + delimiter)], "test")

    def test_text_encoding_respects_supported_part_charset_and_rejects_bad_edits(self):
        for charset in ("ascii", "us-ascii", "UTF-8", "utf8"):
            field = FormField(
                name="a",
                value="ascii text",
                content_type="text/plain; charset=" + charset,
            )
            content, content_type = encode_multipart([field], "charset")
            self.assertEqual(parse_multipart(content, content_type), [field])
        for charset in ("ascii", "us-ascii"):
            with (
                self.subTest(charset=charset),
                self.assertRaisesRegex(ValueError, "文本无法使用字符集"),
            ):
                encode_multipart(
                    [
                        FormField(
                            name="a",
                            value="中文",
                            content_type="text/plain; charset=" + charset,
                        )
                    ]
                )
        for charset in ("latin-1", "utf-16", "bz2"):
            with (
                self.subTest(charset=charset),
                self.assertRaisesRegex(ValueError, "不支持的字符集"),
            ):
                encode_multipart(
                    [
                        FormField(
                            name="a",
                            value="text",
                            content_type="text/plain; charset=" + charset,
                        )
                    ]
                )

    def test_field_parameters_cannot_inject_part_headers(self):
        for value in ("a\r\nX-Extra: injected", "a\nb", "a\x00b", "a\tb"):
            for field in (
                FormField(name=value),
                FormField(kind="file", filename=value, payload=b"x"),
            ):
                with (
                    self.subTest(field=field),
                    self.assertRaisesRegex(ValueError, "换行或控制字符"),
                ):
                    encode_multipart([field])
        for value in ("text/plain\r\nX-Extra: bad", "nonsense", "text/plain; broken"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "Content-Type 无效"),
            ):
                encode_multipart([FormField(content_type=value)])

    def test_invalid_unicode_is_reported_before_sending(self):
        with self.assertRaisesRegex(ValueError, "无法编码为 UTF-8"):
            encode_multipart([FormField(value="\udcff")])


class MultipartParsingTests(unittest.TestCase):
    content_type = "multipart/form-data; boundary=test"

    def test_browser_style_import_keeps_exact_bytes_without_a_disk_path(self):
        content = _body(
            b'Content-Disposition: form-data; name="file"; filename="x.bin"\r\n'
            b"Content-Type: application/octet-stream\r\n\r\n\xff\x00\r\n"
        )
        fields = parse_multipart(content, self.content_type)
        self.assertEqual(
            fields,
            [
                FormField(
                    kind="file",
                    name="file",
                    filename="x.bin",
                    content_type="application/octet-stream",
                    payload=b"\xff\x00\r\n",
                )
            ],
        )

    def test_boundary_like_payload_lines_are_not_actual_delimiters(self):
        content = _body(
            b'Content-Disposition: form-data; name="data"\r\n\r\n'
            b"--testX\r\n--test--suffix\r\nend"
        )
        self.assertEqual(
            parse_multipart(content, self.content_type),
            [FormField(name="data", value="--testX\r\n--test--suffix\r\nend")],
        )

    def test_valid_header_casing_token_parameters_and_utf8_charset_are_supported(self):
        content = _body(
            b"content-disposition: FORM-DATA; name=field\r\n"
            b'CONTENT-TYPE: text/plain; charset="UTF-8"\r\n\r\n' + "正文".encode()
        )
        self.assertEqual(
            parse_multipart(content, 'Multipart/Form-Data ; boundary="test"'),
            [
                FormField(
                    name="field",
                    value="正文",
                    content_type='text/plain; charset="UTF-8"',
                )
            ],
        )

    def test_final_crlf_is_optional_and_empty_values_remain_empty(self):
        content = _body(b'Content-Disposition: form-data; name="empty"\r\n\r\n')
        self.assertEqual(
            parse_multipart(content[:-2], self.content_type), [FormField(name="empty")]
        )

    def test_unknown_or_unrepresentable_parts_fall_back_without_dropping_fields(self):
        parts = (
            b'Content-Disposition: form-data; name="a"\r\nX-Extra: 1\r\n\r\nx',
            (
                b'Content-Disposition: form-data; name="a"\r\n'
                b"Content-Transfer-Encoding: base64\r\n\r\neA=="
            ),
            b'Content-Disposition: form-data; name="a"\r\n\r\n\xff',
            (
                b'Content-Disposition: form-data; name="a"\r\n'
                b"Content-Type: text/plain; charset=latin-1\r\n\r\nx"
            ),
            b"Content-Disposition: form-data; name=\"a\"; filename*=utf-8''x\r\n\r\nx",
            b'Content-Disposition: form-data; name="a"; extra="x"\r\n\r\nx',
            b'Content-Disposition: form-data; name="a"; name="b"\r\n\r\nx',
            b'Content-Disposition: attachment; name="a"\r\n\r\nx',
            b'Content-Disposition: form-data; filename="x"\r\n\r\nx',
            (
                b'Content-Disposition: form-data; name="a"\r\n'
                b'Content-Disposition: form-data; name="b"\r\n\r\nx'
            ),
            b'Content-Disposition: form-data; name="unterminated\r\n\r\nx',
            b'Content-Disposition: form-data; name="a"\r\n folded: header\r\n\r\nx',
        )
        for part in parts:
            with self.subTest(part=part):
                self.assertIsNone(parse_multipart(_body(part), self.content_type))

    def test_malformed_framing_and_non_form_multipart_stay_raw(self):
        good = _body(b'Content-Disposition: form-data; name="a"\r\n\r\nx')
        for content in (
            b"",
            b"preamble\r\n" + good,
            good + b"epilogue",
            good.removesuffix(b"--test--\r\n"),
            good.replace(b"\r\n", b"\n"),
            good + good,
        ):
            with self.subTest(content=content):
                self.assertIsNone(parse_multipart(content, self.content_type))
        for content_type in (
            "text/plain",
            "multipart/mixed; boundary=test",
            "multipart/form-data",
            'multipart/form-data; boundary="broken',
        ):
            with self.subTest(content_type=content_type):
                self.assertIsNone(parse_multipart(good, content_type))


class MultipartBoundaryTests(unittest.TestCase):
    def test_non_multipart_and_missing_boundary_return_none(self):
        for value in ("", "text/plain; boundary=a", "multipart/form-data"):
            with self.subTest(value=value):
                self.assertIsNone(multipart_boundary(value))

    def test_invalid_boundaries_and_ambiguous_parameters_fail(self):
        for boundary in ("", "x" * 71, "a ", "a\r\nb", "中", 'a"b', "a\\b"):
            with self.subTest(boundary=boundary), self.assertRaises(ValueError):
                encode_multipart([], boundary)
        for content_type in (
            'multipart/form-data; boundary=""',
            "multipart/form-data; boundary=one; boundary=two",
            'multipart/form-data; boundary="not closed',
            'multipart/form-data; boundary="ends in space "',
            "multipart/form-data; boundary=one\r\nX-Extra: bad",
        ):
            with self.subTest(content_type=content_type), self.assertRaises(ValueError):
                multipart_boundary(content_type)


class BodyFileTests(unittest.TestCase):
    def test_disk_files_are_read_as_current_bytes_and_keep_the_selected_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "测试.bin"
            path.write_bytes(b"\x00\xffold")
            self.assertEqual(read_body_file(str(path)), b"\x00\xffold")
            field = FormField(kind="file", name="upload", file_path=str(path))
            path.write_bytes(b"\r\nnew\xff")
            content, content_type = encode_multipart([field])
            fields = parse_multipart(content, content_type)
            assert fields is not None
            self.assertEqual(fields[0].filename, path.name)
            self.assertEqual(fields[0].payload, b"\r\nnew\xff")
            self.assertEqual(fields[0].file_path, "")

    def test_missing_non_regular_and_unreadable_files_report_useful_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in (directory, str(Path(directory) / "missing.bin")):
                with (
                    self.subTest(path=path),
                    self.assertRaisesRegex(ValueError, "文件不存在或不是普通文件"),
                ):
                    read_body_file(path)
            path = Path(directory) / "present.bin"
            path.write_bytes(b"data")
            with (
                patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(ValueError, "无法读取文件"),
            ):
                read_body_file(str(path))
        with self.assertRaisesRegex(ValueError, "请先选择"):
            read_body_file("")


if __name__ == "__main__":
    unittest.main()
