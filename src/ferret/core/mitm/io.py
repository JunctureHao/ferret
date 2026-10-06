"""Read and write mitmproxy flow files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from io import BufferedIOBase, BufferedReader
from pathlib import Path
from typing import Any, BinaryIO, cast

from ferret.core.mitm.bindings import (
    DNSFlow,
    Flow,
    FlowReadException,
    HTTPFlow,
    ReadFile,
    Save,
    TCPFlow,
    UDPFlow,
    io,
)

_FLOW_IMPORT: ContextVar[tuple[asyncio.Task[Any] | None, set[str]] | None] = ContextVar(
    "flow_import", default=None
)


def imported_flow_ids() -> set[str] | None:
    """Return the collector only in the task dispatching imported lifecycle hooks.

    Scripts can spawn replay tasks while processing an imported flow. ContextVars
    propagate into those tasks, but their new traffic must still be recorded.
    """
    state = _FLOW_IMPORT.get()
    if state is not None and state[0] is asyncio.current_task():
        return state[1]
    return None


@contextmanager
def flow_import(ids: set[str] | None = None) -> Iterator[None]:
    token = _FLOW_IMPORT.set(
        (asyncio.current_task(), ids if ids is not None else set())
    )
    try:
        yield
    finally:
        _FLOW_IMPORT.reset(token)


class FerretSave(Save):
    """Keep native recording active while excluding historical import hooks."""

    name = "save"

    def request(self, flow: HTTPFlow) -> None:
        if imported_flow_ids() is None:
            super().request(flow)

    def tcp_start(self, flow: TCPFlow) -> None:
        if imported_flow_ids() is None:
            super().tcp_start(flow)

    def udp_start(self, flow: UDPFlow) -> None:
        if imported_flow_ids() is None:
            super().udp_start(flow)

    def dns_request(self, flow: DNSFlow) -> None:
        if imported_flow_ids() is None:
            super().dns_request(flow)

    def save_flow(self, flow: Flow) -> None:
        # Native response/error, WebSocket, TCP, UDP and DNS completion hooks all
        # converge here. No historical flow reaches the writer or active set.
        if imported_flow_ids() is None:
            super().save_flow(flow)

    def done(self) -> None:
        if self.stream is None:
            return
        # Keep native serialization, but acknowledge each successful record.
        # If a later write fails, retry must not duplicate earlier records.
        for flow in tuple(self.active_flows):
            self.stream.add(flow)
            self.active_flows.discard(flow)
        # Retain the stream and path if close fails, so the caller can retry.
        self.stream.fo.close()
        self.stream = None
        self.current_path = None


class _FilePrefix(BufferedReader):
    """Expose only bytes present when an import starts, even if Save appends."""

    def __init__(self, source: BinaryIO) -> None:
        position = source.tell()
        self._remaining = source.seek(0, os.SEEK_END) - position
        source.seek(position)
        # Native ReadFile opens a buffered binary file; BinaryIO's typing omits
        # readinto even though both that file and BytesIO implement it.
        super().__init__(cast(BufferedIOBase, source))

    def read(self, size: int | None = -1) -> bytes:
        size = (
            self._remaining if size is None or size < 0 else min(size, self._remaining)
        )
        data = super().read(size)
        self._remaining -= len(data)
        return data

    def peek(self, size: int = 0) -> bytes:
        return super().peek(size)[: self._remaining]


class FerretReadFile(ReadFile):
    name = "readfile"

    async def load_flows(self, fo: BinaryIO) -> int:
        with _FilePrefix(fo) as source:
            return await super().load_flows(source)


class FlowFile:
    @staticmethod
    def count_http(path: str | Path) -> int:
        """Count HTTP records without retaining their bodies or all flow objects."""
        ids: set[str] = set()
        with Path(path).open("rb") as file:
            try:
                for flow in io.FlowReader(file).stream():
                    if isinstance(flow, HTTPFlow):
                        ids.add(flow.id)
            except FlowReadException:
                pass
        return len(ids)

    @staticmethod
    def write(path: str | Path, flows: Iterable[Flow]) -> int:
        count = 0
        with Path(path).open("wb") as file:
            writer = io.FlowWriter(file)
            for flow in flows:
                writer.add(flow)
                count += 1
        return count

    @staticmethod
    def read(path: str | Path) -> list[Flow]:
        with Path(path).open("rb") as file:
            return list(io.FlowReader(file).stream())

    @staticmethod
    def read_valid_prefix(path: str | Path) -> list[Flow]:
        """Read all complete flows from a possibly truncated file.

        Returns the flows that were fully written before any truncation or
        corruption at the file tail. A truncated final entry is ignored.
        """
        flows: list[Flow] = []
        with Path(path).open("rb") as file:
            reader = io.FlowReader(file)
            try:
                flows.extend(reader.stream())
            except FlowReadException:
                pass
        return flows
