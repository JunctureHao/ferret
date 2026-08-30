"""Ownership-aware system proxy attachment service.

这里抛出来的异常会被宿主（GUI）以 ``str(exc)`` 的形式展示。包**零依赖、不做
翻译**：不 import Qt，也不 import 宿主，只抛本模块顶部的英文常量 —— 常量就是
包与宿主之间的对账表，宿主在展示边界按常量对照译成当前语言。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sysproxy.backends import SystemProxyBackend, create_system_proxy_backend
from sysproxy.models import ProxyEndpoint, ProxySnapshot

#: 挂到系统上的代理地址非法（空 host、端口越界）。
ERR_INVALID_ADDRESS = "Invalid system proxy address"
#: 换新代理之前恢复上一次快照失败。
ERR_RESTORE_FAILED = "Restoring the previous system proxy failed"
#: 写入系统代理失败。
ERR_SET_FAILED = "Setting the system proxy failed"


class SystemProxyService:
    def __init__(
        self,
        backend: SystemProxyBackend | None = None,
        *,
        journal_path: Path | None = None,
    ) -> None:
        """``journal_path`` 必须由宿主注入（通常是其配置目录下的固定文件名）。

        包**刻意不提供默认目录**：journal 的意义是「崩溃后下次启动还能找到」，
        落错目录等于没有，还会和宿主的配置路径分叉。传 ``None`` 表示不写
        journal（测试、或宿主自管恢复）。
        """
        self._backend = backend or create_system_proxy_backend()
        self._journal_path = journal_path
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
            raise ValueError(ERR_INVALID_ADDRESS)
        endpoint = ProxyEndpoint(host, port)
        if self._endpoint == endpoint and self._backend.owns(endpoint):
            return
        if self._endpoint is not None and not self.detach():
            raise RuntimeError(ERR_RESTORE_FAILED)
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
                raise RuntimeError(ERR_SET_FAILED) from apply_error
            raise RuntimeError(ERR_SET_FAILED)
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

    def _write_journal(self, endpoint: ProxyEndpoint, snapshot: ProxySnapshot) -> None:
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
                ProxyEndpoint(str(endpoint_data["host"]), int(endpoint_data["port"])),
                ProxySnapshot(dict(data["snapshot"])),
            )
        except (OSError, ValueError, KeyError, TypeError):
            self._clear_journal()
            return None

    def _clear_journal(self) -> None:
        path = self._journal_path
        if isinstance(path, Path):
            path.unlink(missing_ok=True)
