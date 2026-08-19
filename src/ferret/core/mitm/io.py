"""Read and write mitmproxy flow files."""

from collections.abc import Iterable
from pathlib import Path

from ferret.core.mitm.bindings import Flow, FlowReadException, io


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
