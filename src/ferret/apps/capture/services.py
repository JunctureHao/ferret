"""Capture-specific services.

Shared mitmproxy runtime, flow I/O, export, and certificate integration live in
``ferret.core.mitm``. This module only contains behavior owned by the capture
application.
"""

import re

from ferret.core.mitm import parse_filter

_FIELD_TO_OP: dict[str, str] = {
    "全部": "u",
    "URL": "u",
    "Method": "m",
    "Header": "h",
    "Body": "b",
}


def _escape_regex(text: str) -> str:
    """Escape text for literal matching in a flowfilter expression."""
    return re.escape(text)


def _quote_value(value: str) -> str:
    """Quote a flowfilter value when its contents require it."""
    if not value or (" " in value) or ('"' in value) or ("'" in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _condition_to_expr(condition: dict) -> str | None:
    field = condition.get("field", "全部")
    logic = condition.get("logic", "包含")
    value = (condition.get("value") or "").strip()
    if not value:
        return None

    operator = _FIELD_TO_OP.get(field, "u")
    if logic == "正则表达式":
        regex = value
    elif logic == "等于":
        regex = f"^{_escape_regex(value)}$"
    else:
        regex = _escape_regex(value)

    expression = f"~{operator} {_quote_value(regex)}"
    return f"!{expression}" if logic == "不包含" else expression


def build_filter_expression(conditions: list[dict] | None) -> str:
    """Translate capture UI conditions into a mitmproxy flowfilter string."""
    atoms = ["~http"]
    for condition in conditions or []:
        expression = _condition_to_expr(condition)
        if expression:
            atoms.append(expression)
    return " & ".join(atoms)


def compile_filter(conditions: list[dict] | None):
    """Compile capture UI conditions into a mitmproxy filter."""
    return parse_filter(build_filter_expression(conditions))
