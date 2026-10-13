"""Serializable guards for edits prepared while an HTTP flow is suspended."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import uuid4

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import Flow, HTTPFlow
from ferret.core.mitm.edit_body import BodySource, inspect_edit_body

HeldEditPhase = Literal["request", "response"]
HELD_EDIT_EPOCH_KEY = "ferret.held_edit.epoch"
HELD_EDIT_GUARD_KEY = "ferret.held_edit.guard"
HELD_EDIT_BODY_KEY = "ferret.held_edit.body"


@dataclass(frozen=True)
class HeldEditGuard:
    """Identity of one hold plus the version of the message shown to its editor."""

    generation: int
    flow_id: str
    phase: HeldEditPhase
    edit_epoch: str
    source_version: str

    @property
    def identity(self) -> tuple[int, str, HeldEditPhase, str]:
        """Queue refresh identity; a source change should not discard a draft."""
        return self.generation, self.flow_id, self.phase, self.edit_epoch


def advance_edit_epoch(flow: Flow) -> None:
    """Invalidate prior drafts on every Ferret hold/release lifecycle boundary.

    The metadata is a small native-serializable scalar, not a second held-flow
    ledger. A new UUID also distinguishes different flow objects reusing an id.
    Third-party direct resume/intercept calls need their own lifecycle hook.
    """
    flow.metadata[HELD_EDIT_EPOCH_KEY] = uuid4().hex


def _edit_epoch(flow: Flow) -> str:
    epoch = flow.metadata.get(HELD_EDIT_EPOCH_KEY)
    if not isinstance(epoch, str) or not epoch:
        advance_edit_epoch(flow)
        epoch = flow.metadata[HELD_EDIT_EPOCH_KEY]
    return epoch


def _phase(flow: HTTPFlow) -> HeldEditPhase:
    return "response" if flow.response is not None else "request"


def _hash_value(digest: Any, value: Any) -> None:
    """Hash native state without making another body-sized encoded string."""
    if isinstance(value, bytes):
        digest.update(b"b" + str(len(value)).encode("ascii") + b":")
        digest.update(value)
    elif isinstance(value, str):
        digest.update(b"s")
        _hash_value(digest, value.encode("utf-8", "surrogatepass"))
    elif isinstance(value, dict):
        digest.update(b"d[")
        for key in sorted(value):
            _hash_value(digest, key)
            _hash_value(digest, value[key])
        digest.update(b"]")
    elif isinstance(value, (list, tuple)):
        digest.update(b"l[")
        for item in value:
            _hash_value(digest, item)
        digest.update(b"]")
    elif value is None:
        digest.update(b"n;")
    else:
        digest.update(type(value).__name__.encode("ascii") + b":")
        digest.update(repr(value).encode("utf-8", "surrogatepass") + b";")


def _source_version(flow: HTTPFlow, phase: HeldEditPhase) -> str:
    message = flow.request if phase == "request" else flow.response
    assert message is not None
    state = message.get_state()
    # Timing, marks and comments are not editor inputs. Headers, trailers,
    # raw body bytes (including None), URL/method/status and HTTP version are.
    state.pop("timestamp_start", None)
    state.pop("timestamp_end", None)
    digest = hashlib.sha256()
    _hash_value(digest, state)
    if phase == "response":
        _hash_value(digest, flow.request.method)
    return digest.hexdigest()


def attach_held_edit_snapshot(
    flow: HTTPFlow, snapshot: HTTPFlow, generation: int
) -> None:
    """Attach immutable editing inputs only to the cross-thread snapshot.

    The potentially large decoded content never goes into live-flow metadata,
    avoiding a second retained body in captures and ordinary exports.
    """
    phase = _phase(flow)
    guard = HeldEditGuard(
        generation=generation,
        flow_id=flow.id,
        phase=phase,
        edit_epoch=_edit_epoch(flow),
        source_version=_source_version(flow, phase),
    )
    snapshot.metadata[HELD_EDIT_GUARD_KEY] = asdict(guard)
    message = flow.request if phase == "request" else flow.response
    assert message is not None
    source = inspect_edit_body(message)
    snapshot.metadata[HELD_EDIT_BODY_KEY] = asdict(source)


def held_edit_guard(snapshot: HTTPFlow) -> HeldEditGuard | None:
    """Read the guard delivered with a snapshot, without inspecting live state."""
    raw = snapshot.metadata.get(HELD_EDIT_GUARD_KEY)
    if not isinstance(raw, dict):
        return None
    generation = raw.get("generation")
    flow_id = raw.get("flow_id")
    phase = raw.get("phase")
    epoch = raw.get("edit_epoch")
    version = raw.get("source_version")
    if (
        type(generation) is not int
        or not isinstance(flow_id, str)
        or phase not in ("request", "response")
        or not isinstance(epoch, str)
        or not isinstance(version, str)
    ):
        return None
    return HeldEditGuard(generation, flow_id, phase, epoch, version)


def held_edit_body(snapshot: HTTPFlow) -> BodySource | None:
    """Read the already-decoded editing body; unavailable is distinct from empty."""
    raw = snapshot.metadata.get(HELD_EDIT_BODY_KEY)
    if not isinstance(raw, dict):
        return None
    content = raw.get("content")
    available, decode_ok = raw.get("available"), raw.get("decode_ok")
    if (
        not isinstance(content, (bytes, type(None)))
        or type(available) is not bool
        or type(decode_ok) is not bool
    ):
        return None
    return BodySource(content=content, available=available, decode_ok=decode_ok)


def validate_held_edit(
    flow: Flow | None,
    guard: HeldEditGuard,
    *,
    generation: int,
    flow_id: str,
    phase: HeldEditPhase,
) -> None:
    """Check a frozen draft immediately before mutation on the mitm loop."""
    if guard.generation != generation:
        raise ValueError(
            QCoreApplication.translate(
                "HeldEdit", "内核已重新启动，编辑内容已过期，请重新选择这条流量。"
            )
        )
    if guard.flow_id != flow_id:
        raise ValueError(
            QCoreApplication.translate(
                "HeldEdit", "编辑目标已变化，请重新选择这条流量。"
            )
        )
    if not isinstance(flow, HTTPFlow):
        # 运行期状态而非调用方类型错误：目标已从队列消失，与「找不到 id」同义。
        raise ValueError(  # noqa: TRY004
            QCoreApplication.translate("MitmFacade", "这条流量已不在列表中")
        )
    if not flow.intercepted:
        raise ValueError(
            QCoreApplication.translate("HeldEdit", "这条流量已不再挂起，无法应用编辑。")
        )
    if guard.phase != phase or _phase(flow) != phase:
        raise ValueError(
            QCoreApplication.translate(
                "HeldEdit", "流量的拦截阶段已变化，请重新选择这条流量。"
            )
        )
    if guard.edit_epoch != _edit_epoch(flow):
        raise ValueError(
            QCoreApplication.translate(
                "HeldEdit", "这条流量已重新挂起，编辑内容已过期。"
            )
        )
    if guard.source_version != _source_version(flow, phase):
        raise ValueError(
            QCoreApplication.translate(
                "HeldEdit", "这条流量的报文已变化，请重新载入后编辑。"
            )
        )
