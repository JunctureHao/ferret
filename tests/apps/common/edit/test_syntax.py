"""``edit/syntax.py`` 的词法器与调色板测试。

本文件只碰 ``syntax.py``：它刻意不认识 Qt，也不认识"当前主题"，测试保持同样的边界
（Qt 侧的取色在 ``test_theme.py``）。但导入子模块会先执行包的 ``__init__.py``，
PySide6 因此仍会被间接拉进来，所以照 AGENTS.md 的约定设 offscreen——否则单独跑
这个文件时就依赖宿主有桌面会话。

最重要的一条是 ``RoundTripTests``：任一 ``tokenize_*`` 的输出拼接必须**逐字等于**
输入。``highlightBlock`` 只拿到 ``(长度, 格式)`` 序列并按顺序上色，token 少一个
字符，该行之后的着色就整体左移——这是历史上出过的错位 bug 的根因，必须锁死。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.apps.common.edit.syntax import (
    RE_BODY_SEPARATOR,
    TOKENIZERS,
    Binary,
    Comment,
    Error,
    Generic,
    Keyword,
    KeywordConstant,
    Language,
    Literal,
    MaterialLightStyle,
    MaterialStyle,
    NameAttribute,
    NameTag,
    Number,
    Operator,
    Punctuation,
    String,
    StringDouble,
    StringKey,
    Text,
    Token,
    _tokenize_tag,
    tokenize_headers,
    tokenize_html,
    tokenize_http,
    tokenize_http_line,
    tokenize_json,
    tokenize_text,
    tokenizer_for,
)

#: 覆盖各分词器的刁钻输入：空串 / 纯换行 / CRLF / 无空格分隔符 / 值里带冒号 /
#: 未闭合字符串与标签 / 非 ASCII / 制表符。
SAMPLES = [
    "",
    "\n",
    "\n\n\n",
    "\r\n\r\n",
    "GET / HTTP/1.1",
    "GET /a/b?c=1&d=%20 HTTP/1.1\nHost: api.example.com\n\n",
    'HTTP/1.1 200 OK\nContent-Type: application/json\n\n{"a": 1}',
    'GET / HTTP/1.1\r\nHost: a\r\nContent-Type: application/json\r\n\r\n{"a": 1}',
    "HTTP/1.1 404 \nX-Empty:\n\n",
    "Key:Value",  # 冒号后无空格
    "Key:  Value",  # 冒号后两个空格
    "Host: api.example.com:8080",  # 值里带冒号
    ":leading-colon",
    "no-colon-line",
    "\tTab-Indented: yes",
    "中文头: 中文值 🦊",
    '{"a": 1, "b": [true, null, -2.5e10], "c": {"d": "e"}}',
    '{"unterminated": "abc',
    "{,,,}",
    "[1 2 3]",
    "@#$%^&",
    "<html><body><p class='x'>hi &amp; bye</p></body></html>",
    "<!DOCTYPE html><!-- c --><br/>",
    "<div",  # 未闭合标签
    "a < b > c",  # 裸尖括号
    "plain text body\nsecond line",
]

TOKENIZER_CASES = {
    "http": tokenize_http,
    "headers": tokenize_headers,
    "json": tokenize_json,
    "html": tokenize_html,
    "text": tokenize_text,
}


class RoundTripTests(unittest.TestCase):
    """分词器契约：拼接 == 原文。少一个字符就会让整行着色左移。"""

    def test_all_tokenizers_are_lossless(self) -> None:
        for name, tokenize in TOKENIZER_CASES.items():
            for text in SAMPLES:
                with self.subTest(tokenizer=name, text=text):
                    joined = "".join(value for _, value in tokenize(text))
                    self.assertEqual(joined, text)

    def test_tokenize_http_line_is_lossless(self) -> None:
        # 逐行模式（文档 > LEX_LIMIT）直接调它，同样不能吞字符。
        for text in SAMPLES:
            for line in text.split("\n"):
                with self.subTest(line=line):
                    joined = "".join(v for _, v in tokenize_http_line(line))
                    self.assertEqual(joined, line)

    def test_tokenize_tag_does_not_fabricate_closing_bracket(self) -> None:
        """未闭合标签不能凭空补出 ``>``——原实现固定发 ``<`` / ``>`` 就会多字符。"""
        self.assertEqual(
            "".join(v for _, v in _tokenize_tag("<div")),
            "<div",
        )
        self.assertEqual(
            "".join(v for _, v in _tokenize_tag('<a href="x">')),
            '<a href="x">',
        )


class HttpLineTests(unittest.TestCase):
    def test_request_line(self) -> None:
        tokens = tokenize_http_line("POST /login HTTP/1.1")
        self.assertEqual(tokens[0], (Keyword, "POST"))
        self.assertEqual(tokens[4], (KeywordConstant, "HTTP/1.1"))

    def test_status_line(self) -> None:
        tokens = tokenize_http_line("HTTP/1.1 200 OK")
        self.assertEqual(tokens[0], (KeywordConstant, "HTTP/1.1"))
        self.assertEqual(tokens[2][1], "200")
        self.assertEqual(tokens[4], (Generic, "OK"))

    def test_header_separator_is_its_own_token(self) -> None:
        """``Key:Value``（无空格）不能被硬编码成 ``": "``，否则整行错位。"""
        self.assertEqual(
            tokenize_http_line("Key:Value"),
            [(NameAttribute, "Key"), (Operator, ":"), (Literal, "Value")],
        )

    def test_header_value_may_contain_colon(self) -> None:
        tokens = tokenize_http_line("Host: api.example.com:8080")
        self.assertEqual(tokens[0], (NameAttribute, "Host"))
        self.assertEqual(tokens[2], (Literal, "api.example.com:8080"))

    def test_unrecognized_line_is_plain_text(self) -> None:
        self.assertEqual(tokenize_http_line("no-colon-line"), [(Text, "no-colon-line")])

    def test_empty_line_yields_no_token(self) -> None:
        self.assertEqual(tokenize_http_line(""), [])


class CrlfTests(unittest.TestCase):
    """Raw 面板的文本来自 mitmproxy ``export.raw_request``，是 ``\\r\\n`` 线格式。

    只认 ``\\n`` 的话请求行/状态行会因为行尾的 ``\\r`` 匹配不上正则而整行变纯文本，
    分界空行（切行后只剩 ``"\\r"``）也认不出来——整个 body 会继续按 header 上色。
    """

    def test_request_line_survives_trailing_cr(self) -> None:
        tokens = tokenize_http_line("POST /login HTTP/1.1\r")
        self.assertEqual(tokens[0], (Keyword, "POST"))
        self.assertEqual(tokens[4], (KeywordConstant, "HTTP/1.1"))
        self.assertEqual(tokens[5], (Text, "\r"))

    def test_status_line_survives_trailing_cr(self) -> None:
        tokens = tokenize_http_line("HTTP/1.1 200 OK\r")
        self.assertEqual(tokens[0], (KeywordConstant, "HTTP/1.1"))
        self.assertEqual(tokens[-1], (Text, "\r"))
        # "\r" 单独成 token，不能被塞进 reason phrase 里
        self.assertEqual(tokens[4], (Generic, "OK"))

    def test_header_survives_trailing_cr(self) -> None:
        tokens = tokenize_http_line("Host: a\r")
        self.assertEqual(tokens[0], (NameAttribute, "Host"))
        self.assertEqual(tokens[2], (Literal, "a"))
        self.assertEqual(tokens[3], (Text, "\r"))

    def test_lone_cr_is_the_body_boundary(self) -> None:
        text = "GET / HTTP/1.1\r\nHost: a\r\n\r\nKey: not-a-header"
        types = [t for t, _ in tokenize_http(text)]
        # 只有 "Host: a" 一个字段名；body 里的 "Key: not-a-header" 不再上色
        self.assertEqual(types.count(NameAttribute), 1)

    def test_body_separator_regex_matches_both_line_endings(self) -> None:
        for text, expected in (("a\n\nb", "\n\n"), ("a\r\n\r\nb", "\r\n\r\n")):
            with self.subTest(text=text):
                match = RE_BODY_SEPARATOR.search(text)
                self.assertEqual(match.group() if match else None, expected)
        self.assertIsNone(RE_BODY_SEPARATOR.search("a\nb"))


class HttpVsHeadersTests(unittest.TestCase):
    """空行语义在两个分词器里必须不同——这是 HeadersHighlighter 曾是 no-op 的病灶。"""

    TEXT = "A: 1\n\nB: 2"

    def test_http_treats_blank_line_as_body_boundary(self) -> None:
        types = [t for t, _ in tokenize_http(self.TEXT)]
        self.assertIn(NameAttribute, types)
        # 空行之后是 body，"B: 2" 整体是 Text，不再有第二个字段名。
        self.assertEqual(types.count(NameAttribute), 1)

    def test_headers_has_no_body_boundary(self) -> None:
        """纯 Key: Value 文本里空行只是空行，之后的行仍按 header 上色。"""
        types = [t for t, _ in tokenize_headers(self.TEXT)]
        self.assertEqual(types.count(NameAttribute), 2)

    def test_headers_tokenizer_is_actually_wired_up(self) -> None:
        self.assertIs(tokenizer_for(Language.HEADERS), tokenize_headers)


class JsonTests(unittest.TestCase):
    def test_key_and_value_strings_get_different_tokens(self) -> None:
        tokens = tokenize_json('{"a": "b"}')
        types = {value: ttype for ttype, value in tokens}
        self.assertEqual(types['"a"'], StringKey)
        self.assertEqual(types['"b"'], StringDouble)

    def test_key_detection_skips_whitespace_before_colon(self) -> None:
        tokens = tokenize_json('{"a"\n  : 1}')
        self.assertEqual(tokens[1], (StringKey, '"a"'))

    def test_string_in_array_is_a_value(self) -> None:
        tokens = tokenize_json('["a"]')
        self.assertEqual(tokens[1], (StringDouble, '"a"'))

    def test_literals_and_numbers(self) -> None:
        tokens = dict((v, t) for t, v in tokenize_json("[true, null, -2.5e10]"))
        self.assertEqual(tokens["true"], KeywordConstant)
        self.assertEqual(tokens["null"], KeywordConstant)
        self.assertEqual(tokens["-2.5e10"], Number)
        self.assertEqual(tokens[","], Punctuation)

    def test_stray_character_is_error(self) -> None:
        self.assertIn(Error, [t for t, _ in tokenize_json("{@}")])

    def test_large_document_key_lookup_is_not_quadratic(self) -> None:
        """键判定用索引游标而不是切片拷贝：切片版在这个规模上要几十秒。"""
        count = 20_000
        text = "{" + ",".join(f'"k{i}": {i}' for i in range(count)) + "}"
        tokens = tokenize_json(text)
        self.assertEqual("".join(v for _, v in tokens), text)
        self.assertEqual(sum(1 for t, _ in tokens if t is StringKey), count)


class HtmlTests(unittest.TestCase):
    def test_tag_name_then_attributes(self) -> None:
        tokens = _tokenize_tag('<div class="x">')
        self.assertEqual(tokens[0], (Punctuation, "<"))
        self.assertEqual(tokens[1], (NameTag, "div"))
        self.assertIn((NameAttribute, "class"), tokens)
        self.assertIn((Operator, "="), tokens)
        self.assertIn((String, '"x"'), tokens)

    def test_closing_tag_name_is_still_a_tag(self) -> None:
        self.assertIn((NameTag, "p"), _tokenize_tag("</p>"))

    def test_comment_and_doctype_and_entity(self) -> None:
        types = [t for t, _ in tokenize_html("<!DOCTYPE html><!-- c -->&amp;")]
        self.assertEqual(str(types[0]), "Token.Comment.Preproc")
        self.assertEqual(str(types[1]), "Token.Comment.Multiline")
        self.assertEqual(str(types[2]), "Token.Name.Entity")


class LanguageTests(unittest.TestCase):
    def test_coerce_accepts_known_names(self) -> None:
        self.assertIs(Language.coerce("json"), Language.JSON)
        self.assertIs(Language.coerce("headers"), Language.HEADERS)
        self.assertIs(Language.coerce(Language.XML), Language.XML)

    def test_coerce_falls_back_to_http(self) -> None:
        for bad in ("javascript", "", None, 42, object()):
            with self.subTest(value=bad):
                self.assertIs(Language.coerce(bad), Language.HTTP)

    def test_compares_equal_to_its_string_value(self) -> None:
        """StrEnum：``flow/views.py`` 等按字符串比较的旧代码不能被破坏。"""
        self.assertEqual(Language.JSON, "json")

    def test_every_language_has_a_tokenizer(self) -> None:
        for lang in Language:
            with self.subTest(lang=lang):
                self.assertIn(lang, TOKENIZERS)


class StyleTests(unittest.TestCase):
    def test_color_has_no_hash_prefix(self) -> None:
        """``theme.py`` 负责拼 ``#``；这里返回裸 hex（与 Pygments 对齐）。"""
        for style in (MaterialStyle, MaterialLightStyle):
            with self.subTest(style=style.__name__):
                color = style.style_for_token(Text)["color"]
                self.assertRegex(color, r"^[0-9A-Fa-f]{6}$")

    def test_child_token_overrides_parent(self) -> None:
        """``Literal.String.Key`` 显式配了蓝，不能继承 ``Literal.String`` 的绿。"""
        key = MaterialStyle.style_for_token(StringKey)["color"]
        value = MaterialStyle.style_for_token(StringDouble)["color"]
        self.assertNotEqual(key, value)
        self.assertEqual(key.upper(), MaterialStyle.blue.removeprefix("#"))

    def test_unconfigured_subtype_inherits_parent(self) -> None:
        parent = MaterialStyle.style_for_token(Number)["color"]
        child = MaterialStyle.style_for_token(Token.Literal.Number.Hex)["color"]
        self.assertEqual(parent, child)

    def test_italic_flag_is_parsed(self) -> None:
        self.assertTrue(MaterialStyle.style_for_token(Comment)["italic"])
        self.assertTrue(MaterialStyle.style_for_token(Binary)["italic"])
        self.assertFalse(MaterialStyle.style_for_token(Number)["italic"])

    def test_binary_is_a_real_token_not_a_magic_string(self) -> None:
        """原实现用字符串 ``"__BINARY__"`` 当伪 token；现在它走正常查表。"""
        spec = MaterialStyle.style_for_token(Binary)
        self.assertEqual(spec["color"].upper(), MaterialStyle.gray.removeprefix("#"))

    def test_light_style_covers_every_dark_token(self) -> None:
        """浅色表不能漏项——漏一个就会在白底上继续用为暗底挑的颜色。"""
        self.assertLessEqual(
            set(MaterialStyle.styles), set(MaterialLightStyle.styles)
        )

    def test_light_style_actually_differs_on_low_contrast_tokens(self) -> None:
        """历史上靠 hex 比对只改了 green/cyan 两项，其余在白底上过浅无人管。"""
        for ttype in (Text, StringDouble, Number, NameTag, NameAttribute, Operator):
            with self.subTest(ttype=str(ttype)):
                self.assertNotEqual(
                    MaterialStyle.style_for_token(ttype)["color"],
                    MaterialLightStyle.style_for_token(ttype)["color"],
                )

    def test_every_styled_token_resolves_to_a_color(self) -> None:
        for style in (MaterialStyle, MaterialLightStyle):
            for ttype in style.styles:
                with self.subTest(style=style.__name__, ttype=str(ttype)):
                    self.assertIsNotNone(style.style_for_token(ttype)["color"])


if __name__ == "__main__":
    unittest.main()
