"""Ownership-aware system proxy attachment service."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ferret.core.settings import get_config_dir
from ferret.core.system_proxy.backends import (
    SystemProxyBackend,
    create_system_proxy_backend,
)
from ferret.core.system_proxy.models import ProxyEndpoint, ProxySnapshot

_DEFAULT_JOURNAL = object()


class SystemProxyService:
    def __init__(
        self,
        backend: SystemProxyBackend | None = None,
        *,
        journal_path: Path | None | object = _DEFAULT_JOURNAL,
    ) -> None:
        self._backend = backend or create_system_proxy_backend()
        self._journal_path = (
            get_config_dir() / "system-proxy-state.json"
            if journal_path is _DEFAULT_JOURNAL
            else journal_path
        )
        self._snapshot: ProxySnapshot | None = None
        self._endpoint: ProxyEndpoint | None = None

    @property
    def is_attached(self) -> bool:
        return self._endpoint is not None

    @property
    def endpoint(self) -> ProxyEndpoint | None:
        return self._endpoint

    def attach(self, host: str, port: int) -> None:
        if not host or not (1 <= int(port) <= 65535):
            raise ValueError("无效的系统代理地址")
        endpoint = ProxyEndpoint(host, port)
        if self._endpoint == endpoint and self._backend.owns(endpoint):
            return
        if self._endpoint is not None:
            if not self.detach():
                raise RuntimeError("恢复原系统代理失败")
        snapshot = self._backend.snapshot()
        self._write_journal(endpoint, snapshot)
        try:
            applied = self._backend.set(endpoint)
        except Exception as exc:  # noqa: BLE001
            applied = False
            apply_error = exc
        else:
            apply_error = None
        if not applied:
            if self._backend.restore(snapshot):
                self._clear_journal()
            if apply_error is not None:
                raise RuntimeError("设置系统代理失败") from apply_error
            raise RuntimeError("设置系统代理失败")
        self._snapshot = snapshot
        self._endpoint = endpoint

    def detach(self) -> bool:
        endpoint = self._endpoint
        snapshot = self._snapshot
        self._endpoint = None
        self._snapshot = None
        if endpoint is None or snapshot is None:
            return True
        if not self._backend.owns(endpoint):
            self._clear_journal()
            return True
        if not self._backend.restore(snapshot):
            self._endpoint = endpoint
            self._snapshot = snapshot
            return False
        self._clear_journal()
        return True

    def recover(self) -> bool:
        state = self._read_journal()
        if state is None:
            return True
        endpoint, snapshot = state
        if not self._backend.owns(endpoint):
            self._clear_journal()
            return True
        if not self._backend.restore(snapshot):
            return False
        self._clear_journal()
        return True

    def _write_journal(
        self, endpoint: ProxyEndpoint, snapshot: ProxySnapshot
    ) -> None:
        path = self._journal_path
        if not isinstance(path, Path):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "endpoint": {"host": endpoint.host, "port": endpoint.port},
            "snapshot": snapshot.values,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
        os.replace(tmp, path)

    def _read_journal(self) -> tuple[ProxyEndpoint, ProxySnapshot] | None:
        path = self._journal_path
        if not isinstance(path, Path) or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            endpoint_data = data["endpoint"]
            return (
                ProxyEndpoint(
                    str(endpoint_data["host"]), int(endpoint_data["port"])
                ),
                ProxySnapshot(dict(data["snapshot"])),
            )
        except (OSError, ValueError, KeyError, TypeError):
            self._clear_journal()
            return None

    def _clear_journal(self) -> None:
        path = self._journal_path
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
