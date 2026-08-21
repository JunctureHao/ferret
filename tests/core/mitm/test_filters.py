"""Tests for the flowfilter expression helpers."""

import re
import unittest

from mitmproxy.flowfilter import parse as parse_filter

from ferret.core.mitm.filters import escape_literal, quote_value


class EscapeLiteralTests(unittest.TestCase):
    def test_regex_metacharacters_are_escaped(self) -> None:
        self.assertEqual(escape_literal("api.example.com"), r"api\.example\.com")
        self.assertEqual(escape_literal("a+b?c"), r"a\+b\?c")

    def test_plain_text_survives_unchanged(self) -> None:
        self.assertEqual(escape_literal("example"), "example")

    def test_escaped_literal_matches_itself(self) -> None:
        self.assertTrue(re.search(escape_literal("a.b"), "a.b"))
        self.assertIsNone(re.search(escape_literal("a.b"), "axb"))


class QuoteValueTests(unittest.TestCase):
    def test_simple_value_is_left_bare(self) -> None:
        self.assertEqual(quote_value("example.com"), "example.com")

    def test_empty_value_becomes_an_empty_quoted_string(self) -> None:
        # 裸空串会被词法器整个吞掉，表达式就只剩一个孤零零的操作符。
        self.assertEqual(quote_value(""), '""')

    def test_value_with_space_is_quoted(self) -> None:
        self.assertEqual(quote_value("a b"), '"a b"')

    def test_value_with_single_quote_is_quoted(self) -> None:
        self.assertEqual(quote_value("it's"), '"it\'s"')

    def test_embedded_double_quotes_are_escaped(self) -> None:
        self.assertEqual(quote_value('say "hi"'), '"say \\"hi\\""')


class NativeAcceptanceTests(unittest.TestCase):
    """Why the quoting exists at all: apps/capture/services.py builds ``~d <value>``."""

    def test_quoted_expression_parses_while_the_bare_one_does_not(self) -> None:
        escaped = escape_literal("a b.com")
        self.assertIsNotNone(parse_filter(f"~d {quote_value(escaped)}"))
        # 原生词法把空白当分隔符，不加引号这一串会被切成两个 token。
        with self.assertRaises(ValueError):
            parse_filter(f"~d {escaped}")

    def test_bare_value_still_parses_without_quoting(self) -> None:
        escaped = escape_literal("api.example.com")
        self.assertEqual(quote_value(escaped), escaped)
        self.assertIsNotNone(parse_filter(f"~d {escaped}"))


if __name__ == "__main__":
    unittest.main()
