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
