"""Stable device identities for independent native WireGuard listeners."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.modes import WIREGUARD_PORT


@dataclass(frozen=True)
class WireGuardDevice:
    id: str
    name: str
    port: int = WIREGUARD_PORT
    enabled: bool = True
    key_revision: int = 0

    @property
    def display_name(self) -> str:
        return self.name or QCoreApplication.translate("WireGuard", "默认设备")

    def key_path(self, certs_dir: Path) -> Path:
        validate_wireguard_devices([self])
        # Preserve the legacy identity in place: upgrading must not invalidate
        # the profile already installed on the user's first device.
        if self.id == "default" and self.key_revision == 0:
            return certs_dir / "wireguard.conf"
        return certs_dir / "wireguard" / f"{self.id}-{self.key_revision}.conf"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WireGuardDevice:
        if not isinstance(data, dict):
            raise ValueError("WireGuard device must be an object")  # noqa: TRY004
        required = {"id", "name", "port", "enabled", "key_revision"}
        if not required.issubset(data):
            raise ValueError("WireGuard device is missing required fields")
        return cls(**{field: data[field] for field in required})


def validate_wireguard_devices(devices: Sequence[WireGuardDevice]) -> None:
    """Reject invalid records and aliases before deriving files or listeners."""
    ids: set[str] = set()
    ports: set[int] = set()
    for device in devices:
        if not isinstance(device, WireGuardDevice):
            raise ValueError(  # noqa: TRY004
                QCoreApplication.translate("WireGuard", "WireGuard 设备配置格式无效。")
            )
        # Lower-case file-safe IDs also prevent case aliases on Windows.
        if not isinstance(device.id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", device.id
        ):
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备标识无效。")
            )
        if not isinstance(device.name, str) or (
            not device.name.strip()
            and not (device.id == "default" and device.name == "")
        ):
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备名称不能为空。")
            )
        if type(device.port) is not int or not 1 <= device.port <= 65535:
            raise ValueError(
                QCoreApplication.translate(
                    "WireGuard", "WireGuard 设备端口必须在 1 到 65535 之间。"
                )
            )
        if type(device.enabled) is not bool:
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备启用状态无效。")
            )
        if type(device.key_revision) is not int or device.key_revision < 0:
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备密钥版本无效。")
            )
        if device.id in ids:
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备标识不能重复。")
            )
        if device.port in ports:
            raise ValueError(
                QCoreApplication.translate("WireGuard", "WireGuard 设备端口不能重复。")
            )
        ids.add(device.id)
        ports.add(device.port)


def wireguard_devices_from_config(value: Any) -> list[WireGuardDevice]:
    # None means the old setting did not exist. An explicit [] is an intentional
    # empty registry and must never recreate a deleted legacy identity.
    if value is None:
        return [WireGuardDevice(id="default", name="")]
    if not isinstance(value, list):
        raise ValueError("WireGuard devices must be a list")  # noqa: TRY004
    devices = [WireGuardDevice.from_dict(item) for item in value]
    validate_wireguard_devices(devices)
    return devices


def wireguard_devices_to_config(
    devices: Sequence[WireGuardDevice],
) -> list[dict[str, Any]]:
    validate_wireguard_devices(devices)
    return [device.to_dict() for device in devices]
