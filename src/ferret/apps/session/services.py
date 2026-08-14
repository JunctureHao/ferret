"""Session repository — manages session files, metadata, and lifecycle."""

import json
import os
import time
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ferret.apps.session.models import RecordingHandle, SessionMeta, SessionSource
from ferret.core.log import get_logger
from ferret.core.mitm import Flow, FlowFile, HTTPFlow

log = get_logger("session")

SCHEMA_VERSION = 1
STALE_TMP_AGE_SECONDS = 60 * 60


def normalize_session_name(value: str) -> str:
    name = " ".join(value.strip().split())
    if not name:
        raise ValueError("会话名称不能为空")
    if len(name) > 80:
        raise ValueError("会话名称不能超过 80 个字符")
    return name


def _meta_to_dict(meta: SessionMeta) -> dict:
    return {
        "schema_version": meta.schema_version,
        "session_id": meta.session_id,
        "name": meta.name,
        "created_at": meta.created_at.isoformat(),
        "modified_at": meta.modified_at.isoformat(),
        "flow_count": meta.flow_count,
        "file_size": meta.file_size,
        "source": meta.source.value,
    }


def _dict_to_meta(d: dict, flow_path: Path) -> SessionMeta:
    source_str = d.get("source", "capture")
    try:
        source = SessionSource(source_str)
    except ValueError:
        source = SessionSource.CAPTURE

    return SessionMeta(
        schema_version=d.get("schema_version", 1),
        session_id=d["session_id"],
        name=d["name"],
        path=flow_path,
        created_at=datetime.fromisoformat(d["created_at"]),
        modified_at=datetime.fromisoformat(d["modified_at"]),
        flow_count=d.get("flow_count", 0),
        file_size=d.get("file_size", 0),
        source=source,
    )


class SessionRepository:
    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from ferret.core.settings import get_sessions_dir

            root = get_sessions_dir()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _flow_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.flow"

    def _meta_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def create(
        self,
        name: str,
        flows: Iterable[Flow],
        source: SessionSource = SessionSource.CAPTURE,
    ) -> SessionMeta:
        name = normalize_session_name(name)
        session_id = uuid.uuid4().hex
        flow_path = self._flow_path(session_id)
        meta_path = self._meta_path(session_id)
        flow_tmp = flow_path.with_suffix(".flow.tmp")
        meta_tmp = meta_path.with_suffix(".json.tmp")

        now = datetime.now().astimezone()
        flow_committed = False
        meta_committed = False
        try:
            count = FlowFile.write(flow_tmp, flows)
            os.replace(flow_tmp, flow_path)
            flow_committed = True
            flow_size = flow_path.stat().st_size

            meta = SessionMeta(
                schema_version=SCHEMA_VERSION,
                session_id=session_id,
                name=name,
                path=flow_path,
                created_at=now,
                modified_at=now,
                flow_count=count,
                file_size=flow_size,
                source=source,
            )

            with meta_tmp.open("w", encoding="utf-8") as f:
                json.dump(_meta_to_dict(meta), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(meta_tmp, meta_path)
            meta_committed = True

            return meta
        except Exception:
            for tmp in (flow_tmp, meta_tmp):
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
            if flow_committed and not meta_committed:
                try:
                    flow_path.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "清理未完成的会话文件失败: %s", flow_path, exc_info=True
                    )
            raise

    def import_file(self, source_path: Path, name: str | None = None) -> SessionMeta:
        source_path = Path(source_path)
        if not source_path.exists():
            raise FileNotFoundError(f"源文件不存在: {source_path}")

        resolved = source_path.resolve()
        if resolved.parent == self.root.resolve():
            raise ValueError("不能导入会话目录中的内部文件")

        flows = FlowFile.read(source_path)
        http_flows = [f for f in flows if isinstance(f, HTTPFlow)]
        if not http_flows:
            raise ValueError("该文件中没有可导入的 HTTP 流量")

        if name is None:
            name = source_path.stem

        return self.create(name, http_flows, SessionSource.IMPORT)

    def list_all(self) -> list[SessionMeta]:
        self._cleanup_stale_tmp()
        sessions: list[SessionMeta] = []
        for meta_file in self.root.glob("*.json"):
            try:
                with meta_file.open("r", encoding="utf-8") as f:
                    d = json.load(f)
                session_id = d.get("session_id", meta_file.stem)
                flow_path = self._flow_path(session_id)
                if not flow_path.exists():
                    log.warning("跳过缺失 .flow 的会话: %s", session_id)
                    continue
                sessions.append(_dict_to_meta(d, flow_path))
            except Exception:
                log.warning("元数据损坏，跳过: %s", meta_file.name, exc_info=True)
                continue

        sessions.sort(key=lambda s: s.modified_at, reverse=True)
        return sessions

    def get(self, session_id: str) -> SessionMeta:
        meta_path = self._meta_path(session_id)
        if not meta_path.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        with meta_path.open("r", encoding="utf-8") as f:
            d = json.load(f)
        flow_path = self._flow_path(session_id)
        return _dict_to_meta(d, flow_path)

    def load_flows(self, session_id: str) -> list[HTTPFlow]:
        flow_path = self._flow_path(session_id)
        if not flow_path.exists():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        flows = FlowFile.read(flow_path)
        return [f for f in flows if isinstance(f, HTTPFlow)]

    def rename(self, session_id: str, name: str) -> SessionMeta:
        name = normalize_session_name(name)
        meta = self.get(session_id)
        now = datetime.now().astimezone()
        updated = SessionMeta(
            schema_version=meta.schema_version,
            session_id=meta.session_id,
            name=name,
            path=meta.path,
            created_at=meta.created_at,
            modified_at=now,
            flow_count=meta.flow_count,
            file_size=meta.file_size,
            source=meta.source,
        )
        meta_path = self._meta_path(session_id)
        meta_tmp = meta_path.with_suffix(".json.tmp")
        try:
            with meta_tmp.open("w", encoding="utf-8") as f:
                json.dump(_meta_to_dict(updated), f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(meta_tmp, meta_path)
        except Exception:
            try:
                meta_tmp.unlink(missing_ok=True)
            except OSError:
                log.warning("清理重命名临时文件失败: %s", meta_tmp, exc_info=True)
            raise
        return updated

    def delete(self, session_id: str) -> None:
        meta_path = self._meta_path(session_id)
        flow_path = self._flow_path(session_id)
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError:
                log.exception("删除会话元数据失败: %s", meta_path)
                raise
        if flow_path.exists():
            try:
                flow_path.unlink()
            except OSError:
                log.exception("删除会话 Flow 文件失败: %s", flow_path)
                raise

    def export(self, session_id: str, destination: Path) -> None:
        flow_path = self._flow_path(session_id)
        if not flow_path.exists():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        destination = Path(destination)
        dest_tmp = destination.with_suffix(".flow.tmp")
        try:
            with flow_path.open("rb") as src, dest_tmp.open("wb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
            os.replace(dest_tmp, destination)
        except Exception:
            if dest_tmp.exists():
                dest_tmp.unlink()
            raise

    def _cleanup_stale_tmp(self) -> None:
        now = time.time()
        for pattern in ("*.flow.tmp", "*.json.tmp"):
            for tmp in self.root.glob(pattern):
                try:
                    if now - tmp.stat().st_mtime < STALE_TMP_AGE_SECONDS:
                        continue
                    tmp.unlink()
                except OSError:
                    log.warning("清理临时文件失败: %s", tmp, exc_info=True)


RECORDING_VERSION = 1


class SessionRecorder:
    """Prepare, commit, and recover files written by mitmproxy Save."""

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            from ferret.core.settings import get_sessions_dir

            root = get_sessions_dir()
        self.root = Path(root)
        self.recordings_dir = self.root / "recordings"
        self.recordings_dir.mkdir(parents=True, exist_ok=True)

    def _flow_recording_path(self, session_id: str) -> Path:
        return self.recordings_dir / f"{session_id}.flow.recording"

    def _meta_recording_path(self, session_id: str) -> Path:
        return self.recordings_dir / f"{session_id}.json.recording"

    def _flow_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.flow"

    def _meta_path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def begin(self, name: str, started_at: datetime) -> RecordingHandle:
        name = normalize_session_name(name)
        session_id = uuid.uuid4().hex
        flow_path = self._flow_recording_path(session_id)
        meta_path = self._meta_recording_path(session_id)

        try:
            self._write_recording_meta(meta_path, session_id, name, started_at, 0)
        except Exception:
            self._safe_unlink(flow_path)
            raise

        return RecordingHandle(
            session_id=session_id,
            name=name,
            created_at=started_at,
            flow_path=flow_path,
            meta_path=meta_path,
            flow_count=0,
        )

    def finish(self, handle: RecordingHandle) -> SessionMeta | None:
        """Commit a flow file after mitmproxy's Save addon closed it."""
        flow_recording = handle.flow_path
        meta_recording = handle.meta_path

        if not flow_recording.exists():
            self._delete_recording_files(handle)
            return None

        flows = FlowFile.read_valid_prefix(flow_recording)
        handle.flow_count = len(flows)

        if handle.flow_count == 0:
            self._delete_recording_files(handle)
            return None

        flow_path = self._flow_path(handle.session_id)
        meta_path = self._meta_path(handle.session_id)

        try:
            os.replace(flow_recording, flow_path)
        except OSError:
            log.exception("提交录制 flow 文件失败: %s", flow_recording)
            self._delete_recording_files(handle)
            raise

        try:
            flow_size = flow_path.stat().st_size
            now = datetime.now().astimezone()
            meta = SessionMeta(
                schema_version=SCHEMA_VERSION,
                session_id=handle.session_id,
                name=handle.name,
                path=flow_path,
                created_at=handle.created_at,
                modified_at=now,
                flow_count=handle.flow_count,
                file_size=flow_size,
                source=SessionSource.RECORDING,
            )
            self._write_formal_meta(meta_path, meta)
            try:
                meta_recording.unlink(missing_ok=True)
            except OSError:
                log.warning("删除录制元数据失败: %s", meta_recording, exc_info=True)
            return meta
        except Exception:
            self._delete_recording_files(handle)
            raise

    def fail(self, handle: RecordingHandle) -> None:
        self._delete_recording_files(handle)

    def recover_all(self) -> list[SessionMeta]:
        recovered: list[SessionMeta] = []
        meta_files = sorted(self.recordings_dir.glob("*.json.recording"))
        for meta_recording in meta_files:
            try:
                meta = self._recover_one(meta_recording)
            except Exception:
                log.warning("恢复录制文件失败: %s", meta_recording, exc_info=True)
                continue
            if meta is not None:
                recovered.append(meta)
        return recovered

    def _recover_one(self, meta_recording: Path) -> SessionMeta | None:
        with meta_recording.open("r", encoding="utf-8") as f:
            d = json.load(f)
        session_id = d["session_id"]
        name = d.get("name", f"session-{session_id[:8]}")
        created_at = datetime.fromisoformat(d["created_at"])

        flow_recording = self._flow_recording_path(session_id)
        if not flow_recording.exists():
            self._safe_unlink(meta_recording)
            return None

        flows = FlowFile.read_valid_prefix(flow_recording)
        flow_count = len(flows)

        flow_path = self._flow_path(session_id)
        meta_path = self._meta_path(session_id)

        if flow_count == 0:
            self._safe_unlink(flow_recording)
            self._safe_unlink(meta_recording)
            return None

        if flow_path.exists():
            log.info("正式会话已存在，删除录制残留: %s", session_id)
            self._safe_unlink(flow_recording)
            self._safe_unlink(meta_recording)
            return None

        os.replace(flow_recording, flow_path)
        flow_size = flow_path.stat().st_size
        now = datetime.now().astimezone()
        meta = SessionMeta(
            schema_version=SCHEMA_VERSION,
            session_id=session_id,
            name=name,
            path=flow_path,
            created_at=created_at,
            modified_at=now,
            flow_count=flow_count,
            file_size=flow_size,
            source=SessionSource.RECORDING,
        )
        self._write_formal_meta(meta_path, meta)
        self._safe_unlink(meta_recording)
        return meta

    def _write_recording_meta(
        self,
        path: Path,
        session_id: str,
        name: str,
        created_at: datetime,
        flow_count: int,
    ) -> None:
        data = {
            "recording_version": RECORDING_VERSION,
            "session_id": session_id,
            "name": name,
            "created_at": created_at.isoformat(),
            "flow_count": flow_count,
        }
        tmp = path.with_suffix(".json.recording.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _write_formal_meta(self, path: Path, meta: SessionMeta) -> None:
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(_meta_to_dict(meta), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _delete_recording_files(self, handle: RecordingHandle) -> None:
        self._safe_unlink(handle.flow_path)
        self._safe_unlink(handle.meta_path)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("删除文件失败: %s", path, exc_info=True)
