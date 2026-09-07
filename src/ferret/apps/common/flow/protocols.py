from dataclasses import dataclass
from typing import Any, Protocol

from ferret.core.mitm import HTTPFlow, SseEvent, View, WsClose, WsFrame


class FlowViewController(Protocol):
    """Read-only Flow view controller protocol.

    Replay-capable controllers (CaptureController) additionally implement
    ``replay_flow``/``replay_flows``/``load_replay_file`` and
    ``set_flow_comment``/``set_flow_marked``, but those are NOT
    part of this protocol — they are
    gated at the UI layer by ``FlowViewCapabilities.can_replay`` /
    ``can_comment`` / ``can_mark`` so that
    read-only controllers (SessionViewController) don't need stubs.
    """

    @property
    def view(self) -> View | None: ...

    def total_count(self) -> int: ...
    def get_flow(self, flow_id: str) -> HTTPFlow | None: ...
    def flow_detail(self, flow_id: str) -> dict[str, Any]: ...

    # WS 帧走单独一趟而不是塞进 `flow_detail`：一条行情连接上千帧，选中就得连帧一起
    # 搬，而概览那九张卡片一帧都不用。消息页要看时再问。
    def websocket_frames(self, flow_id: str) -> list[WsFrame]: ...
    def websocket_close(self, flow_id: str) -> WsClose: ...
    # SSE 事件同形：addon 存档恒存全量（显示上限归界面），历史流量由消息页
    # 兑底解 body，所以只读的会话 controller 不提供它（`getattr` 探）。
    def sse_events(self, flow_id: str) -> list[SseEvent]: ...
    def get_raw_request(self, flow_id: str) -> bytes: ...
    def get_raw_response(self, flow_id: str) -> bytes: ...
    def get_raw_flow(self, flow_id: str) -> bytes: ...
    def get_httpie_command(self, flow_id: str) -> str: ...
    def export_har(self, flows: list[HTTPFlow], path: str) -> None: ...


@dataclass(frozen=True, slots=True)
class FlowViewCapabilities:
    can_delete: bool = False
    can_replay: bool = False
    can_save_selection: bool = True
    can_open_url: bool = True
    can_export: bool = True
    can_block: bool = False
    # 标记 / 备注要在**活** flow 上改（`MitmFacade._mutate` 只在 mitm 线程上跑，
    # 内核没在跑就抛）。会话页那批流量是从 `.flow` 文件回来的死对象，写回无处可去，
    # 所以这两项默认关，和 `can_replay` 同一个道理。
    can_comment: bool = False
    can_mark: bool = False
    # 「在 Compose 中编辑」走 `MitmFacade.request_edit` 在 mitm 线程上提取活 flow，
    # 会话页（死对象）一期不做，与上面同一套开关哲学。
    can_edit_compose: bool = False


CAPTURE_CAPABILITIES = FlowViewCapabilities(
    can_delete=True,
    can_replay=True,
    can_save_selection=True,
    can_open_url=True,
    can_export=True,
    can_block=True,
    can_comment=True,
    can_mark=True,
    can_edit_compose=True,
)

READONLY_CAPABILITIES = FlowViewCapabilities(
    can_delete=False,
    can_replay=False,
    can_save_selection=True,
    can_open_url=True,
    can_export=True,
    can_block=False,
    can_comment=False,
    can_mark=False,
)
