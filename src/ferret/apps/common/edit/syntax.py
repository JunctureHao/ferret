"""
自实现的高亮词法分析器，用于取代 Pygments。

原因：Pygments 的 ``pygments.lexers`` 通过 ``importlib`` 字符串动态加载全部
~260 个 lexer 子模块，Nuitka standalone 打包时会因内置的 ``'.*'`` implicit-imports
规则把整个库（约 43 MB 编译单元）全部编译进 exe，且无法通过配置裁剪。本模块用
正则重写项目中实际用到的 HTTP / JSON / HTML 高亮，体积可忽略（< 10 KB）。

设计要点：
* ``TokenType`` 复刻 Pygments 的 ``_TokenType``（点分路径 + ``parent`` 继承）。
* ``MaterialStyle`` 复刻 ``pygments.styles.material.MaterialStyle`` 的配色表与
  ``style_for_token`` 的继承查找，保证视觉上与原来一致。
* ``tokenize_http / tokenize_json / tokenize_html / tokenize_headers`` 产出
  ``(ttype, value)`` 流，与原 ``get_tokens`` 行为兼容。
* ``MaterialStyle``(暗) / ``MaterialLightStyle``(亮) 两套调色板由 ``theme.py``
  按主题选择；本模块不认识 Qt，也不认识“当前主题”。

**分词器契约（highlighter 依赖，改动务必守住）**：任一 ``tokenize_*(text)`` 的输出
拼接必须**逐字等于** ``text``——``highlightBlock`` 只拿到 ``(长度, 格式)`` 序列并按
顺序上色，少一个字符就会让该行之后的着色整体左移。``tests/apps/common/edit/
test_syntax.py`` 对每个分词器都锁了这条不变式。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from typing import ClassVar, Self

# ---------------------------------------------------------------------------
# Token 类型系统（与 Pygments 同构）
# ---------------------------------------------------------------------------


class _TokenType(str):
    """点分路径的 token 类型，支持 ``.parent`` 继承与属性式子类型访问。

    例：``Token.Name.Attribute`` 等价 ``_TokenType("Token.Name.Attribute")``，
    且 ``Token.Name.Attribute.parent == Token.Name``。
    """

    _cache: ClassVar[dict] = {}

    def __new__(cls, value: str) -> Self:
        obj = super().__new__(cls, value)
        return obj

    def __getattr__(self, name: str) -> "_TokenType":
        # 避免与 str 内部属性（如 __xxx__）及已定义的 parent 冲突
        if name.startswith("_"):
            raise AttributeError(name)
        return _TokenType._get(f"{self}.{name}")

    @property
    def parent(self) -> "_TokenType | None":
        idx = self.rfind(".")
        if idx == -1:
            return None
        return _TokenType._get(self[:idx])

    @classmethod
    def _get(cls, value: str) -> "_TokenType":
        cached = cls._cache.get(value)
        if cached is None:
            cached = cls(value)
            cls._cache[value] = cached
        return cached


Token = _TokenType._get("Token")
TokenType = _TokenType  # 兼容 ``from ... import TokenType`` 的旧引用

# 常用 token 常量（与 highlighter.py 中引用保持一致）
Text = Token.Text
Error = Token.Error
Keyword = Token.Keyword
KeywordConstant = Token.Keyword.Constant
Name = Token.Name
NameAttribute = Token.Name.Attribute
NameTag = Token.Name.Tag
NameEntity = Token.Name.Entity
Url = Token.Name.Tag  # 复用 Name.Tag(红色) 作为 URL 上色，避免与默认前景混同
Literal = Token.Literal
String = Token.Literal.String
StringDouble = Token.Literal.String.Double
# JSON 对象键名专用 token：键与值字符串同属 Literal.String，但键需单独上色
# 以示区分（键=蓝，值=绿）。作为 Literal.String.Key 子类型，未单独配置时
# 会继承 Literal.String(绿)，因此在 MaterialStyle 中显式给键配色。
StringKey = Token.Literal.String.Key
Number = Token.Literal.Number
NumberInteger = Token.Literal.Number.Integer
NumberFloat = Token.Literal.Number.Float
Operator = Token.Operator
Punctuation = Token.Punctuation
Comment = Token.Comment
CommentMultiline = Token.Comment.Multiline
CommentPreproc = Token.Comment.Preproc
CommentSingle = Token.Comment.Single
Generic = Token.Generic
# 不可读的二进制 body：不调子语言词法器，整体一个灰斜体 token。原实现用字符串
# ``"__BINARY__"`` 当伪 token，逼得 ``_get_format`` 里要先判一次 str 再走正常查表；
# 作为真 token 类型后，配色和其它 token 走完全相同的路径。
Binary = Token.Generic.Binary
Escape = Token.Escape


# ---------------------------------------------------------------------------
# 语言标识
# ---------------------------------------------------------------------------


class Language(StrEnum):
    """编辑器支持的高亮语言。

    取代原先 ``set_language(lang: str)`` 的魔法字符串 + 静默 fallback：
    非法值在构造 ``Language(...)`` 时就会抛 ``ValueError``，而不是悄悄退回 HTTP。
    继承 ``str`` 以兼容 ``flow/views.py::_body_lang`` 这类按字符串比较的旧代码。
    """

    HTTP = "http"  # 完整报文：请求/状态行 + 头 + 空行 + body
    HEADERS = "headers"  # 纯 Key: Value 文本（无起始行）
    JSON = "json"
    XML = "xml"  # xml/html 共用 html 词法器
    TEXT = "text"  # 不做词法分析，整体走默认前景色

    @classmethod
    def coerce(cls, value: object) -> Language:
        """把外部传入的任意值收敛成合法 Language，无法识别时退回 HTTP。

        仅用于跨模块边界（如 mitmproxy 上报的 syntax 名），包内应直接传 Language。
        """
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            return cls.HTTP


# ---------------------------------------------------------------------------
# Material 配色风格（复刻 pygments.styles.material.MaterialStyle）
# ---------------------------------------------------------------------------


class MaterialStyle:
    """Material 主题配色，复刻 Pygments 同名 Style 的调色板与继承查找。"""

    name = "material"

    # 调色板
    dark_teal = "#263238"
    white = "#FFFFFF"
    black = "#000000"
    red = "#FF5370"
    orange = "#F78C6C"
    yellow = "#FFCB6B"
    green = "#C3E88D"
    cyan = "#89DDFF"
    blue = "#82AAFF"
    paleblue = "#B2CCD6"
    purple = "#C792EA"
    brown = "#C17E70"
    pink = "#F07178"
    violet = "#BB80B3"
    foreground = "#EEFFFF"
    faded = "#546E7A"
    gray = "#808080"  # 二进制 body 占位文本

    background_color = dark_teal

    # 规则表：token 类型 -> 样式字符串（颜色 / bold / italic / underline）
    styles: ClassVar[dict] = {
        Text: foreground,
        Escape: cyan,
        Error: red,
        Keyword: violet,
        Keyword.Constant: cyan,
        Keyword.Type: violet,
        Name: foreground,
        Name.Attribute: violet,
        Name.Tag: red,
        Name.Entity: cyan,
        Literal: green,
        String: green,
        String.Double: green,
        String.Key: blue,  # JSON 对象键名：与值(green)区分
        String.Affix: violet,
        Number: orange,
        Operator: cyan,
        Punctuation: cyan,
        Comment: "italic " + faded,
        Comment.Multiline: "italic " + faded,
        Comment.Preproc: "italic " + faded,
        Comment.Single: "italic " + faded,
        Generic: foreground,
        Generic.Binary: "italic " + gray,
    }

    @classmethod
    def style_for_token(cls, ttype: _TokenType) -> dict:
        """沿 token 路径自根向叶查找，子类型覆盖父类型（与 Pygments 一致）。"""
        color = None
        bold = False
        italic = False
        underline = False

        parts = str(ttype).split(".")
        for i in range(len(parts)):
            node = _TokenType._get(".".join(parts[: i + 1]))
            styledefs = cls.styles.get(node)
            if not styledefs:
                continue
            for word in styledefs.split():
                if word == "bold":
                    bold = True
                elif word == "italic":
                    italic = True
                elif word == "underline":
                    underline = True
                elif word == "noinherit":
                    continue
                else:
                    # 与 Pygments 对齐：返回不带 '#' 的 hex（theme.py 负责拼 '#'）
                    color = word.removeprefix("#")

        return {
            "color": color or None,
            "bold": bold,
            "italic": italic,
            "underline": underline,
        }


class MaterialLightStyle(MaterialStyle):
    """浅色主题调色板。

    Material 本身是暗色主题，它的调色板放在白底上普遍对比度不足。历史实现只有
    一套表，靠在 highlighter 里按 hex 字符串比对改写两个颜色
    （``c3e88d``→``388E3C``、``89ddff``→``00ACC1``）来凑，而且**不分主题一律改**
    ——等于暗色也在用为浅色挑的颜色，同时 violet(头字段名)/orange(数字)/red(标签)
    在白底上依旧过浅，没人管。这里把整表按 Material Design 500~800 号色重挑一遍，
    暗色恢复 Material 原值，两处 hex 比对 hack 随之删除。
    """

    foreground = "#212121"  # 原 #EEFFFF
    red = "#D32F2F"  # 原 #FF5370
    orange = "#E65100"  # 原 #F78C6C
    green = "#388E3C"  # 原 #C3E88D
    cyan = "#00838F"  # 原 #89DDFF（: { } 等标点/操作符）
    blue = "#1565C0"  # 原 #82AAFF（JSON 键名）
    violet = "#8E24AA"  # 原 #BB80B3（HTTP 头字段名 / 关键字）
    string_green = "#107C10"  # 字符串字面量：VSCode 风格绿
    faded = "#78909C"  # 注释：比暗色的 #546E7A 略提亮，避免白底上过重
    gray = "#757575"  # 二进制占位文本：白底上比暗色的 #808080 略压深

    background_color = MaterialStyle.white

    # 逐条重写受影响的项；红/黄等未列入 styles 的调色板成员无需处理
    styles: ClassVar[dict] = {
        **MaterialStyle.styles,
        Text: foreground,
        Escape: cyan,
        Error: red,
        Keyword: violet,
        Keyword.Constant: cyan,
        Keyword.Type: violet,
        Name: foreground,
        Name.Attribute: violet,
        Name.Tag: red,
        Name.Entity: cyan,
        Literal: green,
        String: string_green,
        String.Double: string_green,
        String.Key: blue,
        String.Affix: violet,
        Number: orange,
        Operator: cyan,
        Punctuation: cyan,
        Comment: "italic " + faded,
        Comment.Multiline: "italic " + faded,
        Comment.Preproc: "italic " + faded,
        Comment.Single: "italic " + faded,
        Generic: foreground,
        Generic.Binary: "italic " + gray,
    }


# ---------------------------------------------------------------------------
# 正则分词器
# ---------------------------------------------------------------------------

_RE_REQUEST = re.compile(
    r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|TRACE)\s+(\S+)\s+(HTTP/\d\.\d)$"
)
_RE_STATUS = re.compile(r"^(HTTP/\d\.\d)\s+(\d{3})\s*(.*)$")
# 分隔符单独成组：原实现只捕 key/value 然后硬编码发 ``": "``，遇到 ``Key:Value``
# （无空格）时 token 长度合计比源行多 1，highlightBlock 按长度顺序上色会整行错位。
_RE_HEADER = re.compile(r"^([^:]+)(:\s?)(.*)$")

#: header/body 分界：空行。LF（手工拼的报文）与 CRLF（Raw 面板走 mitmproxy
#: ``export.raw_request``，是线格式）都要认。``highlighter._tokenize_message``
#: 用它切 head/body，与 ``tokenize_http`` 的逐行判定保持同一套语义。
RE_BODY_SEPARATOR = re.compile(r"\r?\n\r?\n")

_RE_JSON = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<string>"(?:\\.|[^"\\])*")      # 字符串（键与值）
    | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
    | (?P<boolean>true|false|null)
    | (?P<punct>[{}\[\]:,])
    | (?P<error>[^\s])
    """,
    re.VERBOSE,
)

_RE_HTML = re.compile(
    r"""
      (?P<comment><!--.*?-->)
    | (?P<doctype><!DOCTYPE[^>]*>)
    | (?P<tag><\/?[a-zA-Z][^>]*>)
    | (?P<entity>&\#?\w+;)
    | (?P<text>[^<]+)
    | (?P<stray>.)
    """,
    re.VERBOSE | re.DOTALL,
)

# 标签内部细分：< div class="x" >  ->  Punct / Name.Tag / Name.Attribute / Operator / String
_RE_TAG_INNER = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<name>[a-zA-Z][\w-]*)
    | (?P<string>"[^"]*"|'[^']*')
    | (?P<op>=)
    | (?P<punct>[<>/])
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


def _header_tokens(m: re.Match[str]) -> list[tuple[_TokenType, str]]:
    """把 ``_RE_HEADER`` 的匹配拆成 字段名 / 分隔符 / 值 三段。

    ``tokenize_http`` 与 ``tokenize_headers`` 共用，避免两处各写一遍（历史上这段
    连同正则一共有三份拷贝：highlighter.py 的 UniversalHighlighter、
    HeadersHighlighter，以及本文件）。
    """
    return [
        (NameAttribute, m.group(1)),
        (Operator, m.group(2)),
        (Literal, m.group(3)),
    ]


def tokenize_http_line(line: str) -> list[tuple[_TokenType, str]]:
    """分词 HTTP 头部区的**单行**：请求行 / 状态行 / ``Key: Value`` / 其它。

    ``tokenize_http`` 逐行调它；``highlighter`` 在超大文档降级为逐行模式时也调它
    （那时没有全文，只有当前 block 的文本）。抽出来是为了两条路径的着色完全一致。
    """
    # CRLF 报文按 "\n" 切行后每行尾留一个 "\r"（Raw 面板的文本来自 mitmproxy
    # ``export.raw_request``，是 \r\n 线格式）。先摘掉再匹配、最后原样补回，
    # 而不是在每条正则里写 \r?——那样既难读，漏掉一处就让整行着色错位。
    if line.endswith("\r"):
        return [*tokenize_http_line(line[:-1]), (Text, "\r")]

    m = _RE_REQUEST.match(line)
    if m:
        return [
            (Keyword, m.group(1)),
            (Text, " "),
            (Url, m.group(2)),
            (Text, " "),
            (KeywordConstant, m.group(3)),
        ]
    m = _RE_STATUS.match(line)
    if m:
        return [
            (KeywordConstant, m.group(1)),
            (Text, " "),
            (NumberInteger, m.group(2)),
            (Text, " "),
            (Generic, m.group(3)),
        ]
    m = _RE_HEADER.match(line)
    if m:
        return _header_tokens(m)
    return [(Text, line)] if line else []


def tokenize_http(text: str) -> list[tuple[_TokenType, str]]:
    """分词 HTTP 报文（请求/状态行 + 头 + 空行后的 body 视为 Text）。

    body 的真实高亮由 ``highlighter.TokenHighlighter`` 按 Content-Type 二次分派，
    这里先整体标记为 ``Token.Text``。
    """
    out: list[tuple[_TokenType, str]] = []
    in_body = False
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append((Text, "\n"))
        if in_body:
            if line:
                out.append((Text, line))
            continue
        if line in ("", "\r"):
            # 空行 = header/body 分界。CRLF 报文切行后分界行只剩一个 "\r"，
            # 必须一并认——否则整个 body 会继续按 header 规则上色。
            # 换行已由上面补，这里只需补回 "\r" 本身以守住"拼接 == 原文"。
            if line:
                out.append((Text, line))
            in_body = True
            continue
        out.extend(tokenize_http_line(line))
    return out


def tokenize_headers(text: str) -> list[tuple[_TokenType, str]]:
    """分词纯 ``Key: Value`` 文本（Headers / Params 面板）。

    这类文本没有起始请求行，也不存在 header/body 分界——空行只是空行，不能像
    ``tokenize_http`` 那样把它之后的内容全判成 body Text。
    """
    out: list[tuple[_TokenType, str]] = []
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append((Text, "\n"))
        m = _RE_HEADER.match(line)
        if m:
            out.extend(_header_tokens(m))
        elif line:
            out.append((Text, line))
    return out


def tokenize_text(text: str) -> list[tuple[_TokenType, str]]:
    """不做词法分析：整体走 widget 默认前景色。"""
    return [(Text, text)]


def tokenize_json(text: str) -> list[tuple[_TokenType, str]]:
    """分词 JSON。字符串/数字/布尔/标点/空白分别映射，无法识别的字符标为 Error。

    字符串进一步区分“键名”（后面紧跟 `:`）与“值”：键名用 StringKey（蓝），
    值用 StringDouble（绿），从而在 JSON 高亮中让 key / value 一眼可辨。
    """
    out: list[tuple[_TokenType, str]] = []
    matches = list(_RE_JSON.finditer(text))
    total = len(matches)
    for i, m in enumerate(matches):
        kind = m.lastgroup
        value = m.group()
        if kind == "ws":
            ttype = Text
        elif kind == "string":
            # 向后跳过空白，看下一个有意义的 token 是否为标点 ':'。
            # 用索引游标而不是 ``matches[i + 1:]``——后者对每个字符串 token 都全量
            # 复制一次剩余列表，在大 JSON 上是 O(n²)（5 万字符串 ≈ 5 万次长列表拷贝）。
            j = i + 1
            while j < total and matches[j].lastgroup == "ws":
                j += 1
            is_key = (
                j < total
                and matches[j].lastgroup == "punct"
                and matches[j].group() == ":"
            )
            ttype = StringKey if is_key else StringDouble
        elif kind == "number":
            ttype = Number
        elif kind == "boolean":
            ttype = KeywordConstant
        elif kind == "punct":
            ttype = Punctuation
        else:
            ttype = Error
        out.append((ttype, value))
    return out


def _tokenize_tag(tag: str) -> list[tuple[_TokenType, str]]:
    """把 ``<div class="x">`` 这样的整段标签细分为 token。

    产出的 value 拼起来必须**逐字等于** ``tag``：``highlightBlock`` 按 token 长度
    顺序上色，少一个字符后面整行都会错位。原实现固定发 ``"<"`` / ``">"``，遇到
    ``</p>``（结束斜杠在 ``[1:-1]`` 之外？不，斜杠在内）尚可，但遇到未闭合的
    ``<div`` 就会凭空多出一个 ``>``。
    """
    open_bracket, inner, close_bracket = tag[:1], tag[1:], ""
    if inner.endswith(">"):
        inner, close_bracket = inner[:-1], ">"

    out: list[tuple[_TokenType, str]] = [(Punctuation, open_bracket)]
    first_name = True
    for m in _RE_TAG_INNER.finditer(inner):
        kind = m.lastgroup
        value = m.group()
        if kind == "ws":
            ttype = Text
        elif kind == "name":
            ttype = NameTag if first_name else NameAttribute
            first_name = False
        elif kind == "string":
            ttype = String
        elif kind == "op":
            ttype = Operator
        else:  # punct / other
            ttype = Punctuation
        out.append((ttype, value))
    if close_bracket:
        out.append((Punctuation, close_bracket))
    return out


def tokenize_html(text: str) -> list[tuple[_TokenType, str]]:
    """分词 HTML/XML 片段。"""
    out: list[tuple[_TokenType, str]] = []
    for m in _RE_HTML.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind == "comment":
            out.append((CommentMultiline, value))
        elif kind == "doctype":
            out.append((CommentPreproc, value))
        elif kind == "tag":
            out.extend(_tokenize_tag(value))
        elif kind == "entity":
            out.append((NameEntity, value))
        else:  # text / stray（未能成 tag 的裸 '<' 等）
            out.append((Text, value))
    return out


Tokenizer = Callable[[str], list[tuple[_TokenType, str]]]

# 语言 → 词法器。取代原先散在 editor.py::set_language 里的 dict 与 highlighter 的
# 子类继承：加语言只需在这里加一行，不再需要新建 QSyntaxHighlighter 子类。
TOKENIZERS: dict[Language, Tokenizer] = {
    Language.HTTP: tokenize_http,
    Language.HEADERS: tokenize_headers,
    Language.JSON: tokenize_json,
    Language.XML: tokenize_html,
    Language.TEXT: tokenize_text,
}


def tokenizer_for(lang: Language) -> Tokenizer:
    """取语言对应的词法器。未登记的语言退回 HTTP（与历史 fallback 行为一致）。"""
    return TOKENIZERS.get(lang, tokenize_http)


__all__ = [
    "RE_BODY_SEPARATOR",
    "TOKENIZERS",
    "Binary",
    "Comment",
    "CommentMultiline",
    "CommentPreproc",
    "CommentSingle",
    "Error",
    "Escape",
    "Generic",
    "Keyword",
    "KeywordConstant",
    "Language",
    "Literal",
    "MaterialLightStyle",
    "MaterialStyle",
    "Name",
    "NameAttribute",
    "NameEntity",
    "NameTag",
    "Number",
    "NumberFloat",
    "NumberInteger",
    "Operator",
    "Punctuation",
    "String",
    "StringDouble",
    "StringKey",
    "Text",
    "Token",
    "TokenType",
    "Tokenizer",
    "Url",
    "tokenize_headers",
    "tokenize_html",
    "tokenize_http",
    "tokenize_http_line",
    "tokenize_json",
    "tokenize_text",
    "tokenizer_for",
]
