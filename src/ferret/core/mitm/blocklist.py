"""Legacy block-rule model, kept only to migrate old config into the gateway.

网关页（`core/mitm/gateway.py` + `apps/gateway/`）取代了屏蔽页之后，这个模型
唯一的用途是把 ``Proxy.BlockList`` 里的老规则读回来交给
:func:`ferret.core.mitm.gateway.gateway_rules_from_block_config`。
不再有任何代码写入它，也不再构造原生 ``block_list`` 选项串
—— 原生 ``BlockList`` addon 已经从链上撤掉（理由见 `core/mitm/master.py`）。
迁移一次即清空老键，等用户升级面铺开之后整个模块可以删掉。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

BLOCK_STATUS_DEFAULT: int = 403


class BlockField(StrEnum):
    """Which part of the request a legacy rule matched against."""

    HOST = "host"
    URL = "url"
    METHOD = "method"


class BlockLogic(StrEnum):
    """How a legacy rule value was turned into a regular expression."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


@dataclass(frozen=True, slots=True)
class BlockRule:
    """A single rule as persisted by the retired blocklist page."""

    field: BlockField = BlockField.HOST
    logic: BlockLogic = BlockLogic.CONTAINS
    value: str = ""
    status_code: int = BLOCK_STATUS_DEFAULT
    enabled: bool = True

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
