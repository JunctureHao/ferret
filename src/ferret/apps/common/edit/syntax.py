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
* ``tokenize_http / tokenize_json / tokenize_html`` 产出 ``(ttype, value)`` 流，
  与原 ``get_tokens`` 行为兼容，便于 ``highlighter.py`` 直接替换。
"""

import re
from typing import List, Tuple

# ---------------------------------------------------------------------------
# Token 类型系统（与 Pygments 同构）
# ---------------------------------------------------------------------------


class _TokenType(str):
    """点分路径的 token 类型，支持 ``.parent`` 继承与属性式子类型访问。

    例：``Token.Name.Attribute`` 等价 ``_TokenType("Token.Name.Attribute")``，
    且 ``Token.Name.Attribute.parent == Token.Name``。
    """

    _cache = {}

    def __new__(cls, value: str) -> "_TokenType":
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
Literal = Token.Literal
String = Token.Literal.String
StringDouble = Token.Literal.String.Double
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
Escape = Token.Escape


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

    background_color = dark_teal

    # 规则表：token 类型 -> 样式字符串（颜色 / bold / italic / underline）
    styles = {
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
        String.Affix: violet,
        Number: orange,
        Operator: cyan,
        Punctuation: cyan,
        Comment: "italic " + faded,
        Comment.Multiline: "italic " + faded,
        Comment.Preproc: "italic " + faded,
        Comment.Single: "italic " + faded,
        Generic: foreground,
    }

    @classmethod
    def style_for_token(cls, ttype: _TokenType) -> dict:
        """沿 token 路径自根向叶查找，子类型覆盖父类型（与 Pygments 一致）。"""
        color = None
        bold = False
        italic = False
        underline = False
        bgcolor = None

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
                    # 与 Pygments 对齐：返回不带 '#' 的 hex（highlighter 负责拼 '#'）
                    color = word[1:] if word.startswith("#") else word

        return {
            "color": color or None,
            "bold": bold,
            "italic": italic,
            "underline": underline,
            "bgcolor": bgcolor or None,
        }


# ---------------------------------------------------------------------------
# 正则分词器
# ---------------------------------------------------------------------------

_RE_REQUEST = re.compile(
    r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|CONNECT|TRACE)\s+(\S+)\s+(HTTP/\d\.\d)$"
)
_RE_STATUS = re.compile(r"^(HTTP/\d\.\d)\s+(\d{3})\s*(.*)$")
_RE_HEADER = re.compile(r"^([^:]+):\s?(.*)$")

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
    """,
    re.VERBOSE,
)


def tokenize_http(text: str) -> List[Tuple[_TokenType, str]]:
    """分词 HTTP 报文（请求/状态行 + 头 + 空行后的 body 视为 Text）。

    body 的真实高亮由 ``highlighter.HTTPHighlighter`` 按 Content-Type 二次分派，
    这里先整体标记为 ``Token.Text``，行为与原 ``HttpLexer.get_tokens`` 一致。
    """
    out: List[Tuple[_TokenType, str]] = []
    in_body = False
    for line in text.split("\n"):
        if not in_body:
            if line == "":
                out.append((Text, "\n"))
                in_body = True
                continue
            m = _RE_REQUEST.match(line)
            if m:
                out.extend(
                    [
                        (Keyword, m.group(1)),
                        (Text, " "),
                        (Name, m.group(2)),
                        (Text, " "),
                        (KeywordConstant, m.group(3)),
                        (Text, "\n"),
                    ]
                )
                continue
            m = _RE_STATUS.match(line)
            if m:
                out.extend(
                    [
                        (KeywordConstant, m.group(1)),
                        (Text, " "),
                        (NumberInteger, m.group(2)),
                        (Text, " "),
                        (Generic, m.group(3)),
                        (Text, "\n"),
                    ]
                )
                continue
            m = _RE_HEADER.match(line)
            if m:
                out.extend(
                    [
                        (NameAttribute, m.group(1)),
                        (Operator, ": "),
                        (Literal, m.group(2)),
                        (Text, "\n"),
                    ]
                )
                continue
            out.extend([(Text, line), (Text, "\n")])
        else:
            out.extend([(Text, line), (Text, "\n")])
    return out


def tokenize_json(text: str) -> List[Tuple[_TokenType, str]]:
    """分词 JSON。字符串/数字/布尔/标点/空白分别映射，无法识别的字符标为 Error。"""
    out: List[Tuple[_TokenType, str]] = []
    for m in _RE_JSON.finditer(text):
        kind = m.lastgroup
        value = m.group()
        if kind == "ws":
            ttype = Text
        elif kind == "string":
            ttype = StringDouble
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


def _tokenize_tag(tag: str) -> List[Tuple[_TokenType, str]]:
    """把 ``<div class="x">`` 这样的整段标签细分为 token。"""
    inner = tag[1:-1] if tag.endswith(">") else tag[1:]
    out: List[Tuple[_TokenType, str]] = [(Punctuation, "<")]
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
        else:
            ttype = Punctuation
        out.append((ttype, value))
    out.append((Punctuation, ">"))
    return out


def tokenize_html(text: str) -> List[Tuple[_TokenType, str]]:
    """分词 HTML/XML 片段。"""
    out: List[Tuple[_TokenType, str]] = []
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
        else:  # text
            out.append((Text, value))
    return out


__all__ = [
    "TokenType",
    "Token",
    "Text",
    "Error",
    "Keyword",
    "KeywordConstant",
    "Name",
    "NameAttribute",
    "NameTag",
    "NameEntity",
    "Literal",
    "String",
    "StringDouble",
    "Number",
    "NumberInteger",
    "NumberFloat",
    "Operator",
    "Punctuation",
    "Comment",
    "CommentMultiline",
    "CommentPreproc",
    "CommentSingle",
    "Generic",
    "Escape",
    "MaterialStyle",
    "tokenize_http",
    "tokenize_json",
    "tokenize_html",
]
