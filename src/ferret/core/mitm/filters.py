"""Helpers for building mitmproxy ``flowfilter`` expressions.

只有两个函数，但它们决定了抓包页过滤条写出来的表达式能不能被
`mitmproxy/flowfilter.py` 的 ``parse`` 接受。这里不做匹配、也不认识规则模型
—— 匹配算法是原生的。
"""

import re


def escape_literal(text: str) -> str:
    """Escape text for literal matching in a flowfilter expression."""
    return re.escape(text)


def quote_value(value: str) -> str:
    """Quote a flowfilter value when its contents require it.

    原生词法把空白当分隔符，带空格的正则不加引号会被切成两个 token。
    """
    if not value or (" " in value) or ('"' in value) or ("'" in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def quote_regex(value: str) -> str:
    r"""Always quote a regex token, doubling backslashes so the lexer keeps them.

    比 `quote_value` 更严：正则**必须**加引号。原生未引用 token 的词法是
    ``CharsNotIn("()~'\"" + 空白)``，正则里的分组括号会直接把 token 截断。
    而引号内又是 ``QuotedString('"', escChar="\\")``：它会把 ``\x`` 反转义成 ``x``，
    ``\.`` 进去出来就成了 ``.``（点变成"任意字符"，匹配范围悄悄变宽）。
    所以引号内的反斜杠得先翻倍 —— 顺序不能反，先翻倍反斜杠再转义引号。
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
