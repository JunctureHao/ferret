"""A single Qt wakeup for coalesced capture notifications.

Queued Qt events never own message bodies: WS/SSE arrivals are reduced to a
per-flow counter before entering the mailbox, and flow row updates coalesce by
key, so a busy GUI leaves at most one pending notification per flow instead of
one queued event per message. Only immutable values enter the mailbox; native
flow ownership is unchanged.
"""

from __future__ import annotations

import itertools
from collections import OrderedDict, deque
from threading import RLock

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot


class UiEventQueue(QObject):
    ready = Signal()

    def __init__(self, target: QObject) -> None:
        super().__init__(target)
        self._target = target
        self._lock = RLock()
        # key → (sequence, signal name, args). Insertion order is delivery order
        # and sequence grows monotonically with it; re-posting a coalescable key
        # re-sequences the entry to the end, its actual place in lifecycle order.
        self._events: OrderedDict[str, tuple[int, str, tuple]] = OrderedDict()
        self._sequence = itertools.count(1)
        self._last = 0
        self._scheduled = False
        self._pending: deque[tuple[str, str, tuple]] = deque()
        self.ready.connect(self._drain, Qt.ConnectionType.QueuedConnection)

    def post(self, name: str, *args) -> None:
        if (
            QThread.currentThread() == self.thread()
            and not self._scheduled
            and not self._pending
        ):
            getattr(self._target, name).emit(*args)
            return
        # GUI reads the bounded history window once, rather than receiving a
        # body-owning queued event for each message while it is busy elsewhere.
        if name in {"websocket_frame", "sse_event"}:
            flow_id, item = args
            args = (
                flow_id,
                "websocket" if name == "websocket_frame" else "sse",
                item.index + 1,
            )
            name = "messages_changed"
        identity = getattr(args[0], "id", args[0]) if args else ""
        key = (
            f"{name}:{identity}"
            if name in {"flow_updated", "messages_changed"}
            else f"#{next(self._sequence)}"
        )
        with self._lock:
            # Move the latest update to its actual place in lifecycle order.
            self._events.pop(key, None)
            self._events[key] = (next(self._sequence), name, args)
            self._last = self._events[key][0]
            if not self._scheduled:
                self._scheduled = True
                self.ready.emit()

    @Slot()
    def _drain(self) -> None:
        for _ in range(256):
            if not self._emit_next():
                break
        with self._lock:
            more = bool(self._pending) or bool(self._events)
            self._scheduled = more
        if more:
            self.ready.emit()

    def _emit_next(self, through: int | None = None) -> bool:
        with self._lock:
            if not self._pending:
                for _ in range(256):
                    if not self._events:
                        break
                    key, entry = next(iter(self._events.items()))
                    if through is not None and entry[0] > through:
                        break
                    self._events.pop(key)
                    self._pending.append((key, entry[1], entry[2]))
            if not self._pending:
                return False
            _, name, args = self._pending.popleft()
        # Keep the batch in a shared deque: a signal handler may synchronously
        # stop capture and flush the remainder before changing its write gate.
        getattr(self._target, name).emit(*args)
        return True

    def flush(self) -> None:
        """Deliver the current prefix before a GUI lifecycle/recording change.

        No processEvents or waiting for the producer: events posted after this
        call started lie beyond the captured sequence. Reentrant calls consume
        the same deque.
        """
        assert QThread.currentThread() == self.thread()
        with self._lock:
            through = self._last
        while self._emit_next(through):
            pass

    def close(self) -> None:
        with self._lock:
            self._events.clear()
            self._pending.clear()
            self._scheduled = False
