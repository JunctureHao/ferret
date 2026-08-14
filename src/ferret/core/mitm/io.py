"""Read and write mitmproxy flow files."""

from collections.abc import Iterable
from pathlib import Path

from ferret.core.mitm.bindings import Flow, io


class FlowFile:
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
