"""``edit/highlighter.py`` 的 ``TokenHighlighter`` 测试。

重构前这里有四个 highlighter 类，其中三个靠"覆写 ``_generate_tokens`` 换词法器"
来区分，但那个钩子**零调用点**——``HeadersHighlighter`` 整个类是 no-op。这里锁住
收敛后的行为：语言直接映射到词法器，且换语言不重建实例。

另外两条容易回归的：

* ``highlightBlock`` 上色靠 ``(长度, 格式)`` 序列，每行长度合计必须等于该行字符数，
  否则该行之后整体左移；
* body 的语言由 header 的 Content-Type 决定，这段跨行上下文只有全文模式保得住，
  超大文档降级逐行时必须静默退化而不是卡死。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication

from ferret.apps.common.edit import highlighter as hl
from ferret.apps.common.edit.highlighter import BINARY, TokenHighlighter
from ferret.apps.common.edit.syntax import (
    Binary,
    Language,
    NameAttribute,
    StringKey,
    Text,
    tokenize_headers,
    tokenize_json,
)

JSON_MESSAGE = (
    "HTTP/1.1 200 OK\n"
    "Content-Type: application/json; charset=utf-8\n"
    "\n"
    '{"user": "jun", "n": 1}'
)

CRLF_JSON_MESSAGE = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: application/json\r\n"
    "\r\n"
    '{"user": "jun"}'
)


class HighlighterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make(
        self, text: str = "", language: Language = Language.HTTP
    ) -> TokenHighlighter:
        # 文档挂在 self 上，避免 QTextDocument 被 GC 掉后 highlighter 悬空
        self.doc = QTextDocument()
        self.doc.setPlainText(text)
        highlighter = TokenHighlighter(self.doc, language)
        highlighter.relex_now()
        return highlighter


class LanguageSwitchTests(HighlighterTestCase):
    def test_default_language_is_http(self) -> None:
        self.assertIs(self.make().language, Language.HTTP)

    def test_set_language_switches_in_place(self) -> None:
        """原实现 deleteLater() 旧实例再 new 一个；现在只换词法器。"""
        highlighter = self.make('{"a": 1}')
        highlighter.set_language(Language.JSON)
        self.assertIs(highlighter.language, Language.JSON)
        self.assertIs(highlighter.document(), self.doc)

    def test_headers_language_is_not_a_noop(self) -> None:
        """HeadersHighlighter 曾整个是 no-op：headers 面板一直在走 HTTP 词法器。

        判据是空行之后的行还认不认字段名——HTTP 词法器会把它当 body。
        """
        highlighter = self.make("A: 1\n\nB: 2", Language.HEADERS)
        types = [t for t, _ in highlighter._tokenize("A: 1\n\nB: 2")]
        self.assertEqual(types.count(NameAttribute), 2)

    def test_each_language_routes_to_its_tokenizer(self) -> None:
        highlighter = self.make()
        for lang, probe, expected in (
            (Language.JSON, '{"a": 1}', StringKey),
            (Language.HEADERS, "A: 1", NameAttribute),
        ):
            with self.subTest(lang=lang):
                highlighter.set_language(lang)
                self.assertIn(expected, [t for t, _ in highlighter._tokenize(probe)])

    def test_text_language_does_no_lexing(self) -> None:
        highlighter = self.make("{{{ not json", Language.TEXT)
        self.assertEqual(
            highlighter._tokenize("{{{ not json"), [(Text, "{{{ not json")]
        )


class BodyDispatchTests(HighlighterTestCase):
    """body 的语言由 header 的 Content-Type 决定（必须全文分词才拿得到）。"""

    def test_json_body_is_lexed_as_json(self) -> None:
        highlighter = self.make(JSON_MESSAGE)
        types = [t for t, _ in highlighter._tokenize(JSON_MESSAGE)]
        self.assertIn(StringKey, types)

    def test_crlf_message_body_is_lexed_as_json(self) -> None:
        """Raw 面板是 \\r\\n 线格式；只找 "\\n\\n" 的话 body 永远切不出来。"""
        highlighter = self.make(CRLF_JSON_MESSAGE)
        types = [t for t, _ in highlighter._tokenize(CRLF_JSON_MESSAGE)]
        self.assertIn(StringKey, types)

    def test_body_falls_back_to_first_char_heuristic(self) -> None:
        """抓包里 body 常常没有 Content-Type，历史行为是看首字符。"""
        text = 'GET / HTTP/1.1\nHost: a\n\n{"a": 1}'
        highlighter = self.make(text)
        self.assertIn(StringKey, [t for t, _ in highlighter._tokenize(text)])

    def test_binary_body_is_one_binary_token(self) -> None:
        text = "HTTP/1.1 200 OK\nContent-Type: image/png\n\n\x89PNG\r\n\x1a\n"
        highlighter = self.make(text)
        tokens = highlighter._tokenize(text)
        self.assertEqual(tokens[-1][0], Binary)

    def test_blank_binary_body_stays_binary(self) -> None:
        """二进制判定必须先于"空白就别动"短路，否则不可见字节会丢掉灰斜体。"""
        self.assertEqual(hl._body_tokens("   ", BINARY), [(Binary, "   ")])

    def test_unknown_body_is_left_as_text(self) -> None:
        text = "GET / HTTP/1.1\nHost: a\n\nhello world"
        highlighter = self.make(text)
        self.assertEqual(highlighter._tokenize(text)[-1], (Text, "hello world"))

    def test_content_type_detection(self) -> None:
        cases = {
            "Content-Type: application/json": Language.JSON,
            "content-type: TEXT/HTML": Language.XML,
            "Content-Type: application/xml": Language.XML,
            "Content-Type: application/octet-stream": BINARY,
            "Content-Type: application/pdf": BINARY,
            "Content-Type: text/plain": None,
            "X-Other: 1": None,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertIs(hl._content_type_lang(header), expected)

    def test_binary_sentinel_is_not_confused_with_a_language(self) -> None:
        """哨兵不是 str，也不等于任何 Language——原实现用 "binary" 字符串。"""
        self.assertNotIsInstance(BINARY, str)
        for lang in Language:
            self.assertIsNot(BINARY, lang)


class LineFormatTests(HighlighterTestCase):
    """``highlightBlock`` 按 (长度, 格式) 顺序上色，长度合计错一个字符就整行左移。"""

    def test_one_entry_per_block(self) -> None:
        highlighter = self.make(JSON_MESSAGE)
        self.assertEqual(
            len(highlighter._line_formats), JSON_MESSAGE.count("\n") + 1
        )

    def test_lengths_match_each_line(self) -> None:
        for text in (JSON_MESSAGE, CRLF_JSON_MESSAGE, "A: 1\n\nB: 2"):
            with self.subTest(text=text):
                highlighter = self.make(text)
                for i, line in enumerate(text.split("\n")):
                    total = sum(n for n, _ in highlighter._line_formats[i])
                    self.assertEqual(total, len(line), f"line {i}: {line!r}")

    def test_formats_reach_the_document(self) -> None:
        """真的把格式写进了 QTextLayout（不只是缓存里算对了）。"""
        self.make(JSON_MESSAGE)
        layout = self.doc.findBlockByNumber(0).layout()
        self.assertIsNotNone(layout)
        self.assertTrue(layout.formats() if layout else [])


class LazyModeTests(HighlighterTestCase):
    """超过 LEX_LIMIT 静默降级逐行：大 body 宁可少点上下文，也不能卡住 UI。"""

    def setUp(self) -> None:
        self._limit = hl.LEX_LIMIT
        hl.LEX_LIMIT = 64

    def tearDown(self) -> None:
        hl.LEX_LIMIT = self._limit

    def test_large_document_drops_the_cache(self) -> None:
        highlighter = self.make("A: 1\n" * 200)
        self.assertTrue(highlighter._lazy)
        self.assertEqual(highlighter._line_formats, [])

    def test_small_document_stays_in_full_mode(self) -> None:
        highlighter = self.make("A: 1")
        self.assertFalse(highlighter._lazy)
        self.assertTrue(highlighter._line_formats)

    def test_per_line_tokens_match_the_full_tokenizer(self) -> None:
        """两条路径（缓存表 / 现场分词）必须给同一行同样的着色。"""
        highlighter = self.make("", Language.HEADERS)
        self.assertEqual(
            highlighter._line_tokens("Host: a"), tokenize_headers("Host: a")
        )
        highlighter.set_language(Language.JSON)
        self.assertEqual(
            highlighter._line_tokens('{"a": 1}'), tokenize_json('{"a": 1}')
        )

    def test_lazy_lines_are_still_lossless(self) -> None:
        highlighter = self.make("", Language.HEADERS)
        for line in ("Host: a", "Key:Value", "", "\r", "plain"):
            with self.subTest(line=line):
                joined = "".join(v for _, v in highlighter._line_tokens(line))
                self.assertEqual(joined, line)


class DebounceTests(HighlighterTestCase):
    """写入路径防抖：原实现每敲一个键就物化全文 + 重分词 + 全文 rehighlight。"""

    def test_contents_changed_defers_instead_of_relexing(self) -> None:
        highlighter = self.make("A: 1")
        self.doc.setPlainText("B: 2")
        # 定时器已挂起，但还没跑（事件循环没转）
        self.assertTrue(highlighter._relex_timer.isActive())

    def test_relex_now_skips_the_debounce_window(self) -> None:
        highlighter = self.make("A: 1")
        self.doc.setPlainText("Host: example.com")
        highlighter.relex_now()
        self.assertFalse(highlighter._relex_timer.isActive())
        total = sum(n for n, _ in highlighter._line_formats[0])
        self.assertEqual(total, len("Host: example.com"))

    def test_relex_is_reentrancy_guarded(self) -> None:
        """``_relex`` 内部的 rehighlight() 会再触发 contentsChanged，不挡就是永动机。"""
        highlighter = self.make("A: 1")
        highlighter.relex_now()
        self.assertFalse(highlighter._relexing)
        self.assertFalse(highlighter._relex_timer.isActive())


if __name__ == "__main__":
    unittest.main()
