"""Native View with post-rewrite admission and bounded completed history."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from ferret.core.mitm.bindings import Flow, HTTPFlow, View, signals
from ferret.core.mitm.io import imported_flow_ids

FLOW_HISTORY_LIMIT = 10_000


class FerretView(View):
    name = "view"

    def __init__(self) -> None:
        super().__init__()
        self.sig_store_add = signals.SyncSignal(lambda flow: None)
        self._prune_timer: asyncio.TimerHandle | None = None

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
        self._prune()

    def _prune(self) -> None:
        if self._prune_timer is not None:
            self._prune_timer.cancel()
        self._prune_timer = None
        excess = len(self._store) - FLOW_HISTORY_LIMIT
        if excess <= 0:
            return
        expired = []
        for flow in self._store.values():
            # View.remove kills live flows: history eviction must never cancel
            # a request or strand a breakpoint. In-flight excess is temporary.
            if not flow.live and not flow.intercepted:
                expired.append(flow)
                if len(expired) >= excess:
                    break
        self.remove(expired)
        if len(self._store) > FLOW_HISTORY_LIMIT:
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
