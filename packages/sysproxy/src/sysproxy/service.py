"""Ownership-aware system proxy attachment service.

这里抛出来的异常会被宿主（GUI）以 ``str(exc)`` 的形式展示。包**零依赖、不做
翻译**：不 import Qt，也不 import 宿主，只抛本模块顶部的英文常量 —— 常量就是
包与宿主之间的对账表，宿主在展示边界按常量对照译成当前语言。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sysproxy.backends import SystemProxyBackend, create_system_proxy_backend
from sysproxy.models import ProxyEndpoint, ProxySnapshot

#: 挂到系统上的代理地址非法（空 host、端口越界）。
ERR_INVALID_ADDRESS = "Invalid system proxy address"
#: 换新代理之前恢复上一次快照失败。
ERR_RESTORE_FAILED = "Restoring the previous system proxy failed"
#: 写入系统代理失败。
ERR_SET_FAILED = "Setting the system proxy failed"
#: 另一个存活实例还持有系统代理的所有权（锁文件被占），拒绝并发 attach。
ERR_OWNER_ACTIVE = "Another running instance owns the system proxy"

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


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
        journal（测试、或宿主自管恢复）——此时也没有跨进程所有权可言。
        """
        self._backend = backend or create_system_proxy_backend()
        self._journal_path = journal_path
        self._snapshot: ProxySnapshot | None = None
        self._endpoint: ProxyEndpoint | None = None
        # 锁文件句柄：attach 成功后一直持有，直到 detach。进程死亡时 OS 关句柄、
        # 锁自动释放 —— 这是「所有者还活着吗」的可靠判据，没有 PID 复用的误判。
        self._lock_fd: int | None = None

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
        if not self._acquire_ownership():
            raise RuntimeError(ERR_OWNER_ACTIVE)
        # journal 里若还压着一份没恢复成功的旧快照（上次 recover 失败后宿主继续
        # 抓包），恢复目标必须是那份旧快照，而不是「当前系统代理」——后者很可能
        # 就是上一进程残留的死代理地址，直接快照会把用户原配置覆盖掉（#72）。
        snapshot: ProxySnapshot | None = None
        stale = self._read_journal()
        if stale is not None:
            stale_endpoint, stale_snapshot = stale
            if not self._backend.owns(stale_endpoint):
                # 系统代理已经不指向残留地址（用户或别的工具改过），journal 作废。
                self._clear_journal()
            elif self._backend.restore(stale_snapshot):
                # 这次恢复下来了：journal 清掉，按正常 attach 取当前快照。
                self._clear_journal()
            else:
                # 还是恢复不下来：把旧快照顶到这次 attach 的「上次状态」上继续用，
                # journal 重写后仍带它 —— 用户原配置在恢复成功之前绝不落袋。
                snapshot = stale_snapshot
        if snapshot is None:
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
            self._release_ownership()
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
            self._release_ownership()
            return True
        if not self._backend.owns(endpoint):
            self._clear_journal()
            self._release_ownership()
            return True
        if not self._backend.restore(snapshot):
            # 恢复失败继续持有所有权与 journal：本实例还挂着，别的实例不许插手，
            # 宿主可以重试 detach 或留到下次启动 recover 兜底。
            self._endpoint = endpoint
            self._snapshot = snapshot
            return False
        self._clear_journal()
        self._release_ownership()
        return True

    def recover(self) -> bool:
        state = self._read_journal()
        if state is None:
            return True
        # journal 还在只说明「有一份没恢复的快照」，不代表 owner 已死：另一个
        # 存活实例（比如同时开了两个 Ferret）正靠这份 journal 在自己 detach 时
        # 恢复用户配置（#73）。拿不到锁 = 有活着的所有者，跳过恢复且**保留**
        # journal，所有权协议交给锁文件裁决。
        if not self._acquire_ownership():
            return True
        endpoint, snapshot = state
        try:
            if not self._backend.owns(endpoint):
                self._clear_journal()
                return True
            if not self._backend.restore(snapshot):
                return False
            self._clear_journal()
            return True
        finally:
            self._release_ownership()

    def _lock_path(self) -> Path | None:
        path = self._journal_path
        if not isinstance(path, Path):
            return None
        return path.with_suffix(path.suffix + ".lock")

    def _acquire_ownership(self) -> bool:
        """抢锁文件的所有权；拿不到说明有存活实例仍是 owner。

        锁文件**不删除**：删文件与加锁之间存在经典竞态（拿到的是被换掉的新
        inode），留一个空锁文件是标准做法。句柄挂在实例上，detach/进程退出释放。
        """
        if self._lock_fd is not None:
            return True
        path = self._lock_path()
        if path is None:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def _release_ownership(self) -> None:
        """释放锁（关句柄即解锁）；幂等，供 detach 失败重试与测试模拟进程死亡。"""
        fd = self._lock_fd
        self._lock_fd = None
        if fd is not None:
            os.close(fd)

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
