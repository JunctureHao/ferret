"""System proxy state shared by platform backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(slots=True)
class ProxySnapshot:
    values: dict[str, Any] = field(default_factory=dict)
