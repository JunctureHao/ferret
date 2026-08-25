"""Capture-specific services.

Shared mitmproxy runtime, flow I/O, and export helpers live in
``ferret.core.mitm``. This module only contains behavior owned by the capture
application.
"""

from ferret.core.mitm import escape_literal, parse_filter, quote_value

#: 键是 `FilterRow.get_condition()` 送出的取值，不是下拉框上的文案 —— 文案会随语言变，
#: 拿它当键筛选会在切到英文时静默失配（见 `apps.common.filter.FILTER_FIELDS`）。
_FIELD_TO_OP: dict[str, str] = {
    "all": "u",
    "URL": "u",
    "Method": "m",
    "Header": "h",
    "Body": "b",
}

#: 不吃值的字段 → 原生动作过滤器。`~websocket` 是 `flowfilter.FWebSocket`：它只看
#: `flow.websocket is not None`，没有可比的正则，所以这类条件不能走下面那套
#: `~op <regex>` 的拼法（`quote_value` 会给它塞个参数进去，直接解析失败）。
_FLAG_FIELDS: dict[str, str] = {
    "WebSocket": "~websocket",
}


def _condition_to_expr(condition: dict) -> str | None:
    field = condition.get("field", "all")
    logic = condition.get("logic", "contains")
    value = (condition.get("value") or "").strip()

    flag = _FLAG_FIELDS.get(field)
    if flag is not None:
        # 标志字段刻意忽略 value：界面上那个输入框对它是禁用的。
        return f"!{flag}" if logic == "is not" else flag

    if not value:
        return None

    operator = _FIELD_TO_OP.get(field, "u")
    if logic == "regex":
        regex = value
    elif logic == "equals":
        regex = f"^{escape_literal(value)}$"
    else:
        regex = escape_literal(value)

    expression = f"~{operator} {quote_value(regex)}"
    return f"!{expression}" if logic == "excludes" else expression


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
