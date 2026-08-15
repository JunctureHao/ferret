"""File-based session repository backed directly by mitmproxy flow files."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ferret.apps.session.models import SessionMeta, SessionSource
from ferret.core.mitm import Flow, FlowFile, HTTPFlow


def normalize_session_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if not name:
        raise ValueError("会话名称不能为空")
    if len(name) > 80:
        raise ValueError("会话名称不能超过 80 个字符")
    if any(char in name for char in '<>:"/\\|?*'):
        raise ValueError("会话名称包含文件名不允许的字符")
    return name


class SessionRepository:
    """Expose ``sessions/*.flow`` as sessions without sidecar metadata."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from ferret.core.settings import get_sessions_dir

            root = get_sessions_dir()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        name = Path(session_id).name
        if not name.endswith(".flow"):
            name += ".flow"
        return self.root / name

    def _unique_path(self, stem: str) -> Path:
        path = self.root / f"{stem}.flow"
        index = 2
        while path.exists():
            path = self.root / f"{stem}-{index}.flow"
            index += 1
        return path

    @staticmethod
    def _read_http(path: Path) -> list[HTTPFlow]:
        return [f for f in FlowFile.read_valid_prefix(path) if isinstance(f, HTTPFlow)]

    def _meta(self, path: Path, source: SessionSource = SessionSource.CAPTURE) -> SessionMeta:
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime).astimezone()
        created = datetime.fromtimestamp(stat.st_ctime).astimezone()
        return SessionMeta(
            schema_version=1,
            session_id=path.name,
            name=path.stem,
            path=path,
            created_at=created,
            modified_at=modified,
            flow_count=len(self._read_http(path)),
            file_size=stat.st_size,
            source=source,
        )

    def create(
        self,
        name: str,
        flows: Iterable[Flow],
        source: SessionSource = SessionSource.CAPTURE,
    ) -> SessionMeta:
        path = self._unique_path(normalize_session_name(name))
        tmp = path.with_suffix(".flow.tmp")
        try:
            FlowFile.write(tmp, flows)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return self._meta(path, source)

    def import_file(self, source_path: Path, name: str | None = None) -> SessionMeta:
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"源文件不存在: {source_path}")
        if source_path.resolve().parent == self.root.resolve():
            raise ValueError("不能导入会话目录中的内部文件")
        if not self._read_http(source_path):
            raise ValueError("该文件中没有可导入的 HTTP 流量")
        stem = normalize_session_name(name or source_path.stem)
        destination = self._unique_path(stem)
        shutil.copy2(source_path, destination)
        return self._meta(destination, SessionSource.IMPORT)

    def list_all(self) -> list[SessionMeta]:
        sessions: list[SessionMeta] = []
        for path in self.root.glob("*.flow"):
            try:
                if path.stat().st_size == 0:
                    continue
                sessions.append(self._meta(path))
            except (OSError, ValueError):
                continue
        sessions.sort(key=lambda session: session.modified_at, reverse=True)
        return sessions

    def get(self, session_id: str) -> SessionMeta:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        return self._meta(path)

    def load_flows(self, session_id: str) -> list[HTTPFlow]:
        path = self._path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        return self._read_http(path)

    def rename(self, session_id: str, name: str) -> SessionMeta:
        source = self._path(session_id)
        if not source.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        destination = self._unique_path(normalize_session_name(name))
        source.rename(destination)
        return self._meta(destination)

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()

    def export(self, session_id: str, destination: Path) -> None:
        source = self._path(session_id)
        if not source.exists():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        destination = Path(destination)
        tmp = destination.with_suffix(".flow.tmp")
        try:
            shutil.copyfile(source, tmp)
            os.replace(tmp, destination)
        finally:
            tmp.unlink(missing_ok=True)
