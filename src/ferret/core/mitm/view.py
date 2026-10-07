"""Native View with post-rewrite admission and bounded completed history."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from ferret.core.mitm.bindings import Flow, HTTPFlow, View, signals
from ferret.core.mitm.io import imported_flow_ids
from ferret.core.mitm.wsframe import WS_FRAME_LIMIT, WS_WINDOW_BYTES

FLOW_HISTORY_LIMIT = 10_000
FLOW_HISTORY_BYTES = 128 * 1024 * 1024


class FerretView(View):
    name = "view"

    def __init__(self) -> None:
        super().__init__()
        self.sig_store_add = signals.SyncSignal(lambda flow: None)
        self._prune_timer: asyncio.TimerHandle | None = None
        self._sizes: dict[str, int] = {}
        self._stored_bytes = 0
        self.additional_size: Callable[[Flow], int] = lambda flow: 0
        self.sig_store_remove.connect(self._forget_size)
        self.sig_store_refresh.connect(self._reset_sizes)

    def _size(self, flow: Flow) -> int:
        size = self.additional_size(flow)
        if isinstance(flow, HTTPFlow):
            size += sum(
                len(message.raw_content or b"")
                for message in (flow.request, flow.response)
                if message is not None
            )
            if flow.websocket is not None:
                size += sum(
                    len(message.content) for message in flow.websocket.messages
                )
        else:
            size += sum(
                len(message.content) for message in getattr(flow, "messages", ())
            )
        return size

    def _remember_size(self, flow: Flow) -> None:
        size = self._size(flow)
        self._stored_bytes += size - self._sizes.get(flow.id, 0)
        self._sizes[flow.id] = size

    def _forget_size(self, flow: Flow) -> None:
        self._stored_bytes -= self._sizes.pop(flow.id, 0)

    def _reset_sizes(self) -> None:
        self._sizes.clear()
        self._stored_bytes = 0
        for flow in self._store.values():
            self._remember_size(flow)

    def websocket_message(self, flow: HTTPFlow) -> None:
        self._trim_websocket(flow)
        if flow.id in self._store:
            self._remember_size(flow)
            self._prune()

    def _trim_websocket(self, flow: HTTPFlow) -> None:
        # 聊天型 / 行情型 WS 一条连接可以刷出无限帧，而原生 messages 列表没有
        # 任何上限。这里直接在原生列表上裁剪：只保留 UI 预览窗口那么多的最新帧
        # （条数与字节双预算，至少保留最新一条）。内核保留 = 界面可见 = 导出内容，
        # 三者一致；被裁掉的帧在任何副本（包括 .flow 导出）里都不再出现。
        data = flow.websocket
        if data is None or not data.messages:
            return
        messages = data.messages
        cut = max(0, len(messages) - WS_FRAME_LIMIT)
        used = sum(len(message.content) for message in messages)
        index = 0
        # 只从最旧一端裁，永远保住最新一帧。
        while used > WS_WINDOW_BYTES and len(messages) - index > 1:
            used -= len(messages[index].content)
            index += 1
        cut = max(cut, index)
        if cut:
            del messages[:cut]

    def update(self, flows: Sequence[Flow]) -> None:
        for flow in flows:
            if flow.id in self._store:
                self._remember_size(flow)
        super().update(flows)
        self._prune()

    def requestheaders(self, f: HTTPFlow) -> None:
        # Request rewriting runs in request(), after the complete body arrives.
        # Inserting here would expose a flow before the gateway sees its final
        # target, including targets explicitly excluded from capture.
        pass

    def request(self, f: HTTPFlow) -> None:
        self.add([f])

    def response(self, f: HTTPFlow) -> None:
        self.add([f])
        super().response(f)

    def error(self, f: HTTPFlow) -> None:
        self.add([f])
        super().error(f)

    def add(self, flows: Sequence[Flow]) -> None:
        for flow in flows:
            imported = imported_flow_ids()
            if imported is not None and isinstance(flow, HTTPFlow):
                imported.add(flow.id)
            if flow.id not in self._store:
                # The bridge only queues this notification. Queue admission
                # before native visibility, including currently filtered flows.
                self.sig_store_add.send(flow=flow)
            super().add([flow])
            self._remember_size(flow)
        self._prune()

    def _prune(self) -> None:
        if self._prune_timer is not None:
            self._prune_timer.cancel()
        self._prune_timer = None
        excess = len(self._store) - FLOW_HISTORY_LIMIT
        remaining_bytes = self._stored_bytes
        if excess <= 0 and remaining_bytes <= FLOW_HISTORY_BYTES:
            return
        expired = []
        for flow in self._store.values():
            # View.remove kills live flows: history eviction must never cancel
            # a request or strand a breakpoint. In-flight excess is temporary.
            if not flow.live and not flow.intercepted:
                expired.append(flow)
                remaining_bytes -= self._sizes.get(flow.id, 0)
                if len(expired) >= excess and remaining_bytes <= FLOW_HISTORY_BYTES:
                    break
        self.remove(expired)
        if (
            len(self._store) > FLOW_HISTORY_LIMIT
            or self._stored_bytes > FLOW_HISTORY_BYTES
        ):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            if self._prune_timer is None:
                self._prune_timer = loop.call_later(0.1, self._prune)

    def done(self) -> None:
        if self._prune_timer is not None:
            self._prune_timer.cancel()
            self._prune_timer = None
