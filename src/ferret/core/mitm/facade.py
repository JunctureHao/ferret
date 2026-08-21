"""Stable application API over mitmproxy's native addons."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ferret.core.mitm.bindings import HTTPFlow, View
from ferret.core.mitm.export import FlowExporter
from ferret.core.mitm.gateway import GatewayRule
from ferret.core.mitm.io import FlowFile
from ferret.core.mitm.rewrite import RewriteRule
from ferret.core.mitm.runtime import MitmRuntime
from ferret.core.network import LOOPBACK_HOST, detect_lan_address

# 改个名，免得和下面同名的 MitmFacade.is_lan_exposed 属性看混。
# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from ferret.core.network import is_lan_exposed as host_is_lan_exposed
from ferret.core.settings import get_sessions_dir


class MitmFacade:
    def __init__(self, runtime: MitmRuntime) -> None:
        self.runtime = runtime
        self._recording_path: Path | None = None

    @property
    def view(self) -> View:
        return self.runtime.view

    @property
    def listen_host(self) -> str:
        """绑定地址：交给 socket bind 的值，可能是 `0.0.0.0`。**不要**拿它去连。"""
        return self.runtime.listen_host

    @property
    def local_client_host(self) -> str:
        """本机接入地址，恒为环回。

        系统代理、本机客户端、工具栏展示的端点都用这个。绑定 `0.0.0.0` 时环回依旧
        可达（`INADDR_ANY` 覆盖所有网卡，含 lo），所以本机这条路径永远不需要跟着变；
        真把 `0.0.0.0:8080` 写进系统代理，抓包会整体失效。
        """
        return LOOPBACK_HOST

    @property
    def is_lan_exposed(self) -> bool:
        """当前绑定地址是否允许局域网设备连进来。"""
        return host_is_lan_exposed(self.runtime.listen_host)

    def lan_address(self) -> str | None:
        """本机在局域网里的 IPv4 地址，**仅供显示 / 复制**；拿不到返回 None。"""
        return detect_lan_address()

    @property
    def listen_port(self) -> int:
        return self.runtime.listen_port

    @property
    def is_running(self) -> bool:
        return self.runtime.is_running

    @property
    def gateway_rules(self) -> list[GatewayRule]:
        return list(self.runtime.gateway_rules)

    @property
    def gateway_enabled(self) -> bool:
        """网关总开关。关掉之后所有规则一律不判，挂起中的流量立刻放行。"""
        return self.runtime.gateway_enabled

    def set_gateway_rules(self, rules: list[GatewayRule]) -> None:
        """Replace the gateway rules; applied immediately when the kernel runs."""
        self.runtime.apply_gateway_rules(rules)

    def set_gateway_enabled(self, enabled: bool) -> None:
        """Flip the gateway master switch; applied immediately when it runs."""
        self.runtime.apply_gateway_rules(enabled=enabled)

    @property
    def rewrite_rules(self) -> list[RewriteRule]:
        return list(self.runtime.rewrite_rules)

    def set_rewrite_rules(self, rules: list[RewriteRule]) -> None:
        """Replace the rewrite rules; applied immediately when the kernel runs."""
        self.runtime.apply_rewrite_rules(rules)

    @property
    def block_global(self) -> bool:
        """是否拒绝来自公网的连接（原生 Block addon 的 `block_global`）。"""
        return self.runtime.block_global

    @property
    def block_private(self) -> bool:
        """是否拒绝来自局域网的连接（原生 Block addon 的 `block_private`）。"""
        return self.runtime.block_private

    def set_block_options(
        self, *, block_global: bool | None = None, block_private: bool | None = None
    ) -> None:
        """Update Block's source filters; applied immediately when the kernel runs."""
        self.runtime.apply_block_options(
            block_global=block_global, block_private=block_private
        )

    def reload_certificate_store(self) -> bool:
        """Pick up a regenerated CA without restarting the kernel."""
        return self.runtime.reload_certificate_store()

    def get_flow(self, flow_id: str) -> HTTPFlow | None:
        def find() -> HTTPFlow | None:
            flow = self.view.get_by_id(flow_id)
            return flow if isinstance(flow, HTTPFlow) else None

        return self.runtime.call(find) if self.runtime.is_running else find()

    def total_count(self) -> int:
        count = lambda: sum(
            isinstance(flow, HTTPFlow) for flow in self.view._store.values()
        )
        return int(self.runtime.call(count)) if self.runtime.is_running else count()

    def all_http_flows(self) -> list[HTTPFlow]:
        snapshot = lambda: [
            flow.copy()
            for flow in self.view._store.values()
            if isinstance(flow, HTTPFlow)
        ]
        return self.runtime.call(snapshot) if self.runtime.is_running else snapshot()

    def set_filter(self, flow_filter) -> None:
        if self.runtime.is_running:
            self.runtime.call(lambda: self.view.set_filter(flow_filter))
        else:
            self.view.set_filter(flow_filter)

    def save_flows(self, flows: list[HTTPFlow], path: str | Path) -> int:
        if self.runtime.is_running:
            flow_ids = [flow.id for flow in flows]

            def snapshot() -> list[HTTPFlow]:
                result = []
                for flow_id in flow_ids:
                    flow = self.view.get_by_id(flow_id)
                    if isinstance(flow, HTTPFlow):
                        result.append(flow.copy())
                return result

            flows = self.runtime.call(snapshot)
        return FlowFile.write(path, flows)

    def get_httpie_command(self, flow_id: str) -> str:
        return self._export(flow_id, FlowExporter.httpie_command, "")

    def get_raw_request(self, flow_id: str) -> bytes:
        return self._export(flow_id, FlowExporter.raw_request, b"")

    def get_raw_response(self, flow_id: str) -> bytes:
        return self._export(flow_id, FlowExporter.raw_response, b"")

    def get_raw_flow(self, flow_id: str) -> bytes:
        return self._export(flow_id, FlowExporter.raw, b"")

    def export_har(self, flows: list[HTTPFlow], path: str) -> None:
        FlowExporter.save_har(flows, path)

    def _export(self, flow_id: str, exporter, default):
        def export():
            flow = self.view.get_by_id(flow_id)
            return exporter(flow) if isinstance(flow, HTTPFlow) else default

        return self.runtime.call(export) if self.runtime.is_running else export()

    def start_capture_recording(self) -> Path:
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError("mitmproxy 内核未运行")
        started_at = datetime.now().astimezone()
        path = get_sessions_dir() / f"capture-{started_at:%Y%m%d-%H%M%S}.flow"
        self.runtime.call(
            lambda: master.options.update(
                save_stream_file=str(path), save_stream_filter="~http"
            )
        )
        self._recording_path = path
        return path

    def stop_capture_recording(self) -> Path | None:
        path = self._recording_path
        master = self.runtime.master
        if self.runtime.is_running and master is not None:
            self.runtime.call(lambda: master.options.update(save_stream_file=None))
        self._recording_path = None
        if path is not None and path.exists() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)
        return path

    def replay_flow(self, flow_id: str) -> None:
        flow = self.get_flow(flow_id)
        if flow is None:
            raise ValueError("找不到指定的 Flow")
        self.replay_flows([flow])

    def replay_flows(self, flows: list[HTTPFlow]) -> None:
        if not flows:
            raise ValueError("没有可重发的 Flow")
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError("mitmproxy 内核未运行，无法回放")

        def enqueue() -> None:
            replay_flows: list[HTTPFlow] = []
            for flow in flows:
                if master.client_playback.check(flow) is not None:
                    continue
                replay = flow.copy()
                replay.response = None
                replay.error = None
                replay.is_replay = "request"
                replay_flows.append(replay)
            if not replay_flows:
                raise ValueError("无可回放的 Flow")
            self.view.add(replay_flows)
            master.client_playback.start_replay(replay_flows)

        self.runtime.call(enqueue)

    def replay_file(self, path: Path | str) -> None:
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError("mitmproxy 内核未运行，无法回放")
        self.runtime.call(
            lambda: master.client_playback.load_file(str(path)), timeout=10.0
        )

    def load_flow_file(self, path: Path | str) -> int:
        """Load historical flows through the native ReadFile addon."""
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError("mitmproxy 内核未运行，无法读取文件")

        async def load() -> int:
            recording = master.options.save_stream_file
            if recording:
                master.options.update(save_stream_file=None)
            try:
                return await master.readfile.load_flows_from_path(str(path))
            finally:
                if recording:
                    master.options.update(save_stream_file=recording)

        return int(self.runtime.call(load, timeout=30.0))

    def clear_flows(self) -> None:
        def clear() -> None:
            # 清空之后挂起中的行就没了，界面上再也找不到它 —— 顺手放行，别留下一批
            # 看不见、又一直钉着连接的流量。
            master = self.runtime.master
            if master is not None:
                master.gateway.release_all()
            self.view.clear()

        if self.runtime.is_running:
            self.runtime.call(clear)
        else:
            clear()

    def remove_flows(self, flows: list[HTTPFlow]) -> None:
        flow_ids = [flow.id for flow in flows]

        def remove() -> None:
            # 必须先放行：`View.remove` 对 killable 的 flow 直接 kill()
            # （`addons/view.py:435`），而 kill() 会把 intercepted 清成 False，之后
            # resume() 开头那句 `if not intercepted: return` 就再也唤不醒它 ——
            # 挂起中的行被删掉，等于让那条连接永久挂死在 wait_for_resume() 上。
            master = self.runtime.master
            if master is not None:
                master.gateway.release(flow_ids)
            current = [self.view.get_by_id(flow_id) for flow_id in flow_ids]
            self.view.remove([flow for flow in current if flow is not None])

        if self.runtime.is_running:
            self.runtime.call(remove)
        else:
            remove()
