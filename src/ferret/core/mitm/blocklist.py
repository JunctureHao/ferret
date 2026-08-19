"""Block-rule model and mitmproxy ``block_list`` spec construction.

The matching / blocking algorithm itself is mitmproxy's native ``BlockList``
addon (`mitmproxy/addons/blocklist.py`); this module only builds and validates
the option strings that addon consumes. No Qt and no UI labels here.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ferret.core.mitm.bindings import parse_block_spec, status_codes

# 444：原生 addon 见到它走 flow.kill()，直接断连而不是回一个空响应。
BLOCK_STATUS_CLOSE: int = status_codes.NO_RESPONSE
BLOCK_STATUS_DEFAULT: int = 403

# 原生 parse_spec 拿 option[0] 当分隔符，再要求 rem.split(sep, 2) 恰好 2 段。
# URL 天然带 "/" 和 ":"，写死任何一个都会把表达式切成 3 段直接抛 ValueError，
# 所以按表达式内容动态挑一个没出现过的字符。
_SEPARATOR_POOL = "|#@%,;:/!&*^"


class BlockField(StrEnum):
    """Which part of the request a rule matches against."""

    HOST = "host"
    URL = "url"
    METHOD = "method"


class BlockLogic(StrEnum):
    """How a rule value is turned into a regular expression."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


# ~d / ~u / ~m 对应 flowfilter 的 FDomain / FUrl / FMethod。
_FIELD_TO_OP: dict[BlockField, str] = {
    BlockField.HOST: "d",
    BlockField.URL: "u",
    BlockField.METHOD: "m",
}


def escape_literal(text: str) -> str:
    """Escape text for literal matching in a flowfilter expression."""
    return re.escape(text)


def quote_value(value: str) -> str:
    """Quote a flowfilter value when its contents require it."""
    if not value or (" " in value) or ('"' in value) or ("'" in value):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _pick_separator(expression: str) -> str:
    for candidate in _SEPARATOR_POOL:
        if candidate not in expression:
            return candidate
    raise ValueError("无法为该规则挑选分隔符，请简化匹配值")


@dataclass(frozen=True, slots=True)
class BlockRule:
    """A single user-authored blocking rule."""

    field: BlockField = BlockField.HOST
    logic: BlockLogic = BlockLogic.CONTAINS
    value: str = ""
    status_code: int = BLOCK_STATUS_DEFAULT
    enabled: bool = True

    @property
    def expression(self) -> str:
        """The flowfilter expression this rule matches with."""
        value = self.value.strip()
        if not value:
            raise ValueError("匹配值不能为空")
        if self.logic == BlockLogic.REGEX:
            pattern = value
        elif self.logic == BlockLogic.EQUALS:
            pattern = f"^{escape_literal(value)}$"
        else:
            pattern = escape_literal(value)
        return f"~{_FIELD_TO_OP[self.field]} {quote_value(pattern)}"

    def to_spec(self) -> str:
        """Build the ``block_list`` option string, verified by mitmproxy itself."""
        if not 100 <= self.status_code <= 599:
            raise ValueError(f"无效的 HTTP 状态码：{self.status_code}")
        expression = self.expression
        separator = _pick_separator(expression)
        spec = f"{separator}{expression}{separator}{self.status_code}"
        # 最终裁判是原生 parse_spec：分隔符、段数、状态码、flowfilter 语法全过它。
        parse_block_spec(spec)
        return spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": str(self.field),
            "logic": str(self.logic),
            "value": self.value,
            "status_code": self.status_code,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "BlockRule":
        """Rebuild a rule from persisted data; raises on anything unusable."""
        if not isinstance(raw, dict):
            raise TypeError("规则必须是对象")
        try:
            field = BlockField(str(raw.get("field", BlockField.HOST)))
            logic = BlockLogic(str(raw.get("logic", BlockLogic.CONTAINS)))
        except ValueError as exc:
            raise ValueError(f"未知的规则字段：{exc}") from exc
        try:
            status_code = int(raw.get("status_code", BLOCK_STATUS_DEFAULT))
        except (TypeError, ValueError) as exc:
            raise ValueError("无效的 HTTP 状态码") from exc
        return cls(
            field=field,
            logic=logic,
            value=str(raw.get("value", "")),
            status_code=status_code,
            enabled=bool(raw.get("enabled", True)),
        )


def specs_from_rules(rules: Iterable[BlockRule]) -> list[str]:
    """Translate rules into ``block_list`` option strings, skipping inactive ones."""
    specs: list[str] = []
    for rule in rules:
        if not rule.enabled or not rule.value.strip():
            continue
        specs.append(rule.to_spec())
    return specs


def rules_from_config(raw: Any) -> list[BlockRule]:
    """Read rules back from persisted config, dropping entries we cannot parse."""
    if not isinstance(raw, list):
        return []
    rules: list[BlockRule] = []
    for item in raw:
        try:
            rules.append(BlockRule.from_dict(item))
        except (TypeError, ValueError):
            continue
    return rules


def rules_to_config(rules: Iterable[BlockRule]) -> list[dict[str, Any]]:
    """Serialize rules for persistence."""
    return [rule.to_dict() for rule in rules]
