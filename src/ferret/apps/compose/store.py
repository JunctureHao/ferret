"""Compose 请求库持久化：Roaming 单个 JSON，原子写 + 防抖落盘。

- 权威副本在内存（list 保序），每次变更后防抖 300ms 写盘，退出前 ``flush``
  冲刷——与 ``Config.defer_save`` 同一模式。
- 读取复用 ``Config._read``，写入采用同目录暂存 + replace，并转义请求头里的
  非 Unicode 字节。文件写入完成才报告保存成功。
- 文件损坏时留 ``.corrupt-<uuid>`` 副本并回退空库（``Config._preserve_corrupt``
  同款），损坏提示经 ``warning`` 交给界面，加载不阻塞 Compose 页打开。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal

from ferret.apps.compose.models import SCHEMA_VERSION, SavedRequest
from ferret.core.settings import Config, get_compose_requests_file

__all__ = ["ComposeRequestStore"]


class ComposeRequestStore(QObject):
    """已保存请求的内存权威副本 + 落盘。"""

    changed = Signal()
    save_failed = Signal(str)

    def __init__(self, parent: QObject | None = None, *, path: Path | None = None):
        super().__init__(parent)
        self._path = path if path is not None else get_compose_requests_file()
        self._requests: list[SavedRequest] = []
        self._folders: list[str] = []
        self._save_timer: QTimer | None = None
        self._save_pending = False
        self._write_blocked = ""
        self.warning = ""
        self.load()

    @property
    def path(self) -> Path:
        return self._path

    def requests(self) -> list[SavedRequest]:
        return list(self._requests)

    def folders(self) -> list[str]:
        return list(self._folders)

    def get(self, request_id: str) -> SavedRequest | None:
        for request in self._requests:
            if request.id == request_id:
                return request
        return None

    # —— 查询 / 装载 ——

    def load(self) -> None:
        """从磁盘重建内存副本；损坏留副本回退空库，单条坏记录跳过不拖垮整库。"""
        self._requests = []
        self._folders = []
        self.warning = ""
        self._write_blocked = ""
        try:
            data = Config._read(self._path)
        except FileNotFoundError:
            return
        except OSError as exc:
            self._write_blocked = QCoreApplication.translate(
                "ComposeLibrary", "无法读取请求库，已暂停保存以保留原文件：{}"
            ).format(exc)
            self.warning = self._write_blocked
            return
        except (ValueError, UnicodeError):
            self._preserve_source()
            return
        version = data.get("version", 1)
        if type(version) is not int or version not in (1, SCHEMA_VERSION):
            self._write_blocked = QCoreApplication.translate(
                "ComposeLibrary", "请求库版本不受支持，已保留原文件并暂停保存：{}"
            ).format(version)
            self.warning = self._write_blocked
            return
        raw_requests = data.get("requests", [])
        folders = data.get("folders", [])
        if not isinstance(raw_requests, list) or not isinstance(folders, list):
            self._preserve_source()
            return
        damaged = False
        for name in folders:
            if not isinstance(name, str) or not name.strip():
                damaged = True
            elif name not in self._folders:
                self._folders.append(name)
        seen: set[str] = set()
        for item in raw_requests:
            try:
                if version == 1 and isinstance(item, dict):
                    # Old libraries contain HTTP drafts, including manually
                    # entered Upgrade headers. Never turn them into active WS
                    # connections by guessing from their URLs or headers.
                    item = {**item, "protocol": "http", "websocket": {}}
                request = SavedRequest.from_dict(item)
            except (ValueError, TypeError):
                damaged = True
                continue
            if request.id in seen:
                damaged = True
                continue
            seen.add(request.id)
            self._requests.append(request)
            if request.folder and request.folder not in self._folders:
                self._folders.append(request.folder)
        if damaged:
            self._preserve_source()
        elif version == 1:
            self._defer_save()

    def _preserve_source(self) -> None:
        # Never replace a partially readable library until a recovery copy exists.
        backup = self._path.with_name(f"{self._path.name}.corrupt-{uuid.uuid4().hex}")
        try:
            shutil.copy2(self._path, backup)
        except OSError as exc:
            self._write_blocked = QCoreApplication.translate(
                "ComposeLibrary", "无法备份损坏的请求库，已暂停保存以保留原文件：{}"
            ).format(exc)
            self.warning = self._write_blocked
        else:
            self.warning = QCoreApplication.translate(
                "ComposeLibrary",
                "请求库包含损坏的数据，已加载可恢复的请求；原文件已备份：{}",
            ).format(backup)

    # —— 变更（内存 → 防抖落盘 → changed）——

    def upsert(self, request: SavedRequest) -> None:
        """按 id 覆盖或追加；请求引用的未知文件夹自动登记。"""
        for index, existing in enumerate(self._requests):
            if existing.id == request.id:
                self._requests[index] = request
                break
        else:
            self._requests.append(request)
        if request.folder and request.folder not in self._folders:
            self._folders.append(request.folder)
        self._commit()

    def remove(self, request_id: str) -> None:
        before = len(self._requests)
        self._requests = [r for r in self._requests if r.id != request_id]
        if len(self._requests) != before:
            self._commit()

    def add_folder(self, name: str) -> None:
        name = name.strip()
        if name and name not in self._folders:
            self._folders.append(name)
            self._commit()

    def rename_folder(self, old: str, new: str) -> None:
        new = new.strip()
        if not old or not new or new == old or new in self._folders:
            return
        if old not in self._folders:
            return
        self._folders = [new if name == old else name for name in self._folders]
        self._requests = [
            request.touched(folder=new) if request.folder == old else request
            for request in self._requests
        ]
        self._commit()

    def remove_folder(self, name: str) -> None:
        """删除文件夹不删请求：其中的请求回落到未分组。"""
        if name not in self._folders:
            return
        self._folders = [folder for folder in self._folders if folder != name]
        self._requests = [
            request.touched(folder="") if request.folder == name else request
            for request in self._requests
        ]
        self._commit()

    # —— 落盘 ——

    def _commit(self) -> None:
        self._defer_save()
        self.changed.emit()

    def _defer_save(self) -> None:
        self._save_pending = True
        app = QCoreApplication.instance()
        if app is None:
            self._save()
            return
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.setInterval(300)
            self._save_timer.timeout.connect(self._save)
            app.aboutToQuit.connect(self.flush)
        self._save_timer.start()

    def flush(self) -> bool:
        # 与 Config.flush_pending_save 同理：单发定时器回调期间已不活跃，
        # 写盘义务单独持有，失败的写入退出时仍可重试。
        if self._save_pending:
            return self._save()
        return not self._write_blocked

    def _save(self) -> bool:
        if self._save_timer is not None:
            self._save_timer.stop()
        if self._write_blocked:
            self.save_failed.emit(self._write_blocked)
            return False
        payload = {
            "version": SCHEMA_VERSION,
            "folders": self._folders,
            "requests": [request.to_dict() for request in self._requests],
        }
        try:
            self._write(payload)
        except (OSError, ValueError, UnicodeError) as exc:
            # 保留 pending：退出前的 flush 还能再试一次。
            self.save_failed.emit(
                QCoreApplication.translate(
                    "ComposeLibrary", "请求库保存失败：{}"
                ).format(exc)
            )
            return False
        self._save_pending = False
        return True

    def _write(self, payload: dict) -> None:
        # Captured headers can contain surrogateescape characters. JSON escapes
        # preserve them without sending them through a lossy UTF-8 text encoder.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f".{self._path.name}.", dir=self._path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, allow_nan=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self._path)
        finally:
            temporary.unlink(missing_ok=True)
