from dataclasses import dataclass
from typing import Protocol

from ferret.core.mitm import HTTPFlow, View


class FlowViewController(Protocol):
    """Read-only Flow view controller protocol.

    Replay-capable controllers (CaptureController) additionally implement
    ``replay_flow``/``replay_flows``/``load_replay_file``, but those are NOT
    part of this protocol — they are
    gated at the UI layer by ``FlowViewCapabilities.can_replay`` so that
    read-only controllers (SessionViewController) don't need stubs.
    """

    @property
    def view(self) -> View | None: ...

    def total_count(self) -> int: ...
    def get_flow(self, flow_id: str) -> HTTPFlow | None: ...
    def get_raw_request(self, flow_id: str) -> bytes: ...
    def get_raw_response(self, flow_id: str) -> bytes: ...
    def get_raw_flow(self, flow_id: str) -> bytes: ...
    def get_httpie_command(self, flow_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class FlowViewCapabilities:
    can_delete: bool = False
    can_replay: bool = False
    can_save_selection: bool = True
    can_open_url: bool = True
    can_export: bool = True


CAPTURE_CAPABILITIES = FlowViewCapabilities(
    can_delete=True,
    can_replay=True,
    can_save_selection=True,
    can_open_url=True,
    can_export=True,
)

READONLY_CAPABILITIES = FlowViewCapabilities(
    can_delete=False,
    can_replay=False,
    can_save_selection=True,
    can_open_url=True,
    can_export=True,
)
