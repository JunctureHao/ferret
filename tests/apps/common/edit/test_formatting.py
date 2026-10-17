"""Formatting must not change JSON data, literal spellings or error offsets."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.apps.common.edit import formatting
from ferret.apps.common.edit.formatting import JsonFormatLimitError, format_json


class JsonFormattingTests(unittest.TestCase):
    def test_nested_containers_have_two_space_indentation(self) -> None:
        self.assertEqual(
            format_json(' {"b":[1,{"c":true}],"a":{"d":null}} '),
            '{\n  "b": [\n    1,\n    {\n      "c": true\n    }\n  ],\n'
            '  "a": {\n    "d": null\n  }\n}',
        )

    def test_order_duplicate_keys_and_number_spellings_are_preserved(self) -> None:
        source = (
            '{"z":-0,"a":1.230000000000000000000000000001,'
            '"z":9007199254740993123456789,"a":1E+009,"b":-2.00e-9999}'
        )
        self.assertEqual(
            format_json(source),
            '{\n  "z": -0,\n  "a": 1.230000000000000000000000000001,\n'
            '  "z": 9007199254740993123456789,\n  "a": 1E+009,\n'
            '  "b": -2.00e-9999\n}',
        )

    def test_huge_integers_do_not_hit_python_conversion_limit(self) -> None:
        literal = "9" * 5000
        self.assertEqual(format_json(literal), literal)

    def test_strings_and_escaped_keys_are_preserved_verbatim(self) -> None:
        key = r'"\u0061"'
        value = r'"\"\\\/\b\f\n\r\t\u0041\ud800\uD83D\uDE00 中文 🦊 {}[]:,"'
        self.assertEqual(
            format_json("{" + key + ":" + value + "}"),
            "{\n  " + key + ": " + value + "\n}",
        )

    def test_empty_containers_stay_compact(self) -> None:
        self.assertEqual(format_json(" { \t } "), "{}")
        self.assertEqual(format_json("[\n]"), "[]")
        self.assertEqual(
            format_json('{"a":{},"b":[],"c":[{},[]]}'),
            '{\n  "a": {},\n  "b": [],\n  "c": [\n    {},\n    []\n  ]\n}',
        )

    def test_root_scalars(self) -> None:
        for value in ('"literal"', "false", "true", "null", "-0", "1e99999"):
            with self.subTest(value=value):
                self.assertEqual(format_json(" \t" + value + "  "), value)

    def test_formatting_is_idempotent(self) -> None:
        for source in ('{"a":[1,{},[true,null]]}', '["\\n", "中文"]\n', '"x"'):
            with self.subTest(source=source):
                formatted = format_json(source)
                self.assertEqual(format_json(formatted), formatted)

    def test_trailing_newline_is_preserved_once_and_normalized(self) -> None:
        for trailing in ("\n", "\r\n", "\r", "\n\n", "\r\n \t\n  "):
            with self.subTest(trailing=trailing):
                self.assertEqual(format_json("{}" + trailing), "{}\n")
        self.assertEqual(format_json("\n{} \t"), "{}")

    def test_invalid_json_keeps_standard_error_position(self) -> None:
        invalid = (
            "",
            " \n ",
            '{\n  "a": [1,]\n}',
            '{\n  "a" 1\n}',
            '{"a":1 "b":2}',
            "[1 2]",
            "{'a':1}",
            "[01]",
            "[+1]",
            "[.2]",
            "[1.]",
            "[true,false,]",
            '{"a":1}//comment',
            '"unterminated',
            '"raw\nnewline"',
            '"raw\ttab"',
            r'"\x41"',
            '"\x00"',
            "{} []",
            "\ufeff{}",
            "\u00a0{}",
            '{"a": invalid, "later": NaN}',
        )
        for source in invalid:
            with self.subTest(source=source):
                with self.assertRaises(json.JSONDecodeError) as expected:
                    json.loads(source)
                with self.assertRaises(json.JSONDecodeError) as actual:
                    format_json(source)
                self.assertEqual(actual.exception.pos, expected.exception.pos)
                self.assertEqual(actual.exception.lineno, expected.exception.lineno)
                self.assertEqual(actual.exception.colno, expected.exception.colno)

    def test_non_finite_constants_have_accurate_positions(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            for source in (
                constant,
                '{\n  "NaN": "Infinity -Infinity",\n  "value": ' + constant + "\n}",
            ):
                with self.subTest(source=source):
                    position = source.rindex(constant)
                    expected = json.JSONDecodeError("Expecting value", source, position)
                    with self.assertRaises(json.JSONDecodeError) as actual:
                        format_json(source)
                    self.assertEqual(actual.exception.pos, expected.pos)
                    self.assertEqual(actual.exception.lineno, expected.lineno)
                    self.assertEqual(actual.exception.colno, expected.colno)


class JsonFormattingLimitTests(unittest.TestCase):
    def test_input_limit_includes_exact_boundary(self) -> None:
        source = '"' + "x" * (formatting.MAX_INPUT_CHARS - 2) + '"'
        self.assertEqual(format_json(source), source)
        with self.assertRaises(JsonFormatLimitError):
            format_json(source + " ")

    def test_nesting_limit_includes_exact_boundary(self) -> None:
        depth = formatting.MAX_NESTING_DEPTH
        source = "[" * depth + "0" + "]" * depth
        self.assertEqual(format_json(format_json(source)), format_json(source))
        with self.assertRaises(JsonFormatLimitError):
            format_json("[" + source + "]")

    def test_nesting_counts_objects_and_ignores_escaped_string_content(self) -> None:
        source = (
            '{"a":' * formatting.MAX_NESTING_DEPTH
            + '"[\\"{"'
            + "}" * (formatting.MAX_NESTING_DEPTH)
        )
        self.assertIn('"[\\"{"', format_json(source))
        with self.assertRaises(JsonFormatLimitError):
            format_json('{"a":' + source + "}")

    def test_excessive_nesting_is_rejected_before_recursive_decoding(self) -> None:
        with self.assertRaises(JsonFormatLimitError):
            format_json("[" * 10000 + "0" + "]" * 10000)

    def test_output_limit_includes_exact_boundary_and_final_newline(self) -> None:
        source = "[0,1]\n"
        expected = "[\n  0,\n  1\n]\n"
        with patch.object(formatting, "MAX_OUTPUT_CHARS", len(expected)):
            self.assertEqual(format_json(source), expected)
        with (
            patch.object(formatting, "MAX_OUTPUT_CHARS", len(expected) - 1),
            self.assertRaises(JsonFormatLimitError),
        ):
            format_json(source)

    def test_indentation_expansion_cannot_exceed_output_limit(self) -> None:
        depth = formatting.MAX_NESTING_DEPTH
        source = "[" * depth + ",".join(["0"] * 40000) + "]" * depth
        self.assertLess(len(source), formatting.MAX_INPUT_CHARS)
        with self.assertRaises(JsonFormatLimitError):
            format_json(source)


if __name__ == "__main__":
    unittest.main()
