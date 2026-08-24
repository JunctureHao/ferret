"""Stable application API over mitmproxy's native addons."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import HTTPFlow, View
from ferret.core.mitm.export import FlowExporter
from ferret.core.mitm.gateway import GatewayRule
from ferret.core.mitm.intercept import (
    InterceptRule,
    RequestEdit,
    ResponseEdit,
    apply_request_edit,
    apply_response_edit,
    fake_response,
)
from ferret.core.mitm.io import FlowFile
from ferret.core.mitm.rewrite import RewriteRule
from ferret.core.mitm.runtime import MitmRuntime
from ferret.core.network import LOOPBACK_HOST, detect_lan_address

# 改个名，免得和下面同名的 MitmFacade.is_lan_exposed 属性看混。
# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from ferret.core.network import is_lan_exposed as host_is_lan_exposed
from ferret.core.settings import get_sessions_dir


# 同一句在下面出现两次，写成函数而不是常量：模块级求值赶在翻译器安装之前
# （`core/application.py` 顶层就 import 了主窗口），译文会永久冻结成英文。
def _not_running() -> str:
    return QCoreApplication.translate("MitmFacade", "The mitmproxy core is not running")


def _snapshot(flow: HTTPFlow) -> HTTPFlow:
    """跨线程只读副本：`copy()` 完把 id 补回去。

    原生 `Serializable.copy()` 会顺手给副本换一个新 uuid
    （`mitmproxy/coretypes/serializable.py`），而界面拿到快照之后唯一能回头找到这条
    真流量的凭据就是 `flow.id` —— id 一换，`release_flows` / `apply_request_edits`
    / `save_flows` 全都会 `get_by_id` 落空，报的还是「这条流量已不在列表中」。

    回放那条路刻意不用它：一次回放本来就是一条新流量，就该拿新 id。
    """
    clone = flow.copy()
    clone.id = flow.id
    return clone


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

    # —— 断点 ——

    @property
    def intercept_rules(self) -> list[InterceptRule]:
        return list(self.runtime.intercept_rules)

    @property
    def intercept_enabled(self) -> bool:
        """断点总开关。关掉后不再拦新流量，**已拦下的不会自动放行**。

        与网关的总开关刻意相反：网关的挂起是规则的副产物，断点拦下的这一条是用户
        正在编辑的对象，关个开关不该把它冲掉。
        """
        return self.runtime.intercept_enabled

    def set_intercept_rules(self, rules: list[InterceptRule]) -> None:
        """Replace the breakpoint rules; applied immediately when the kernel runs."""
        self.runtime.apply_intercept_rules(rules)

    def set_intercept_enabled(self, enabled: bool) -> None:
        """Flip the breakpoint master switch; applied immediately when it runs."""
        self.runtime.apply_intercept_rules(enabled=enabled)

    def intercepted_flows(self) -> list[HTTPFlow]:
        """Snapshots of every flow currently held at a breakpoint.

        扫 `flow.intercepted` 而不是读 `InterceptState` 那本账：网关的「挂起」策略
        拦下的流量同样是 `intercepted`，断点页要能看见并放行它们（`InterceptState.arm`
        对已被拦住的流量刻意退让，所以那本账里根本没有它们）。

        返回的是保留了原 id 的副本 —— Qt 线程只许读快照（AGENTS.md §3），但放行和
        写回都要靠这个 id 找回真流量，见 `_snapshot`。
        """
        snapshot = lambda: [
            _snapshot(flow)
            for flow in self.view._store.values()
            if isinstance(flow, HTTPFlow) and flow.intercepted
        ]
        return self.runtime.call(snapshot) if self.runtime.is_running else snapshot()

    def release_flows(self, flow_ids: list[str]) -> int:
        """Resume the named held flows; 返回真的放行了几条。"""
        return self._resume(flow_ids, kill=False)

    def drop_flows(self, flow_ids: list[str]) -> int:
        """Drop the named held flows: resume then kill, so the client gets nothing."""
        return self._resume(flow_ids, kill=True)

    def release_all_intercepted(self) -> int:
        """Resume every held flow, whoever is holding it."""
        if not self.runtime.is_running:
            return 0

        def release_all() -> int:
            master = self.runtime.master
            if master is None:
                return 0
            # 三项相加而不是只看兜底那一趟：两本账 `release` 完流量已经不是
            # `intercepted` 了，`_sweep` 再扫必然是 0，回 0 会让界面把一次成功的放行
            # 报成「没放到人」（`InterceptController._resume` 拿它当成功判据）。
            released = master.gateway.release_all()
            released += master.intercept_state.release_all()
            return released + self._sweep(list(self.view._store))

        return int(self.runtime.call(release_all))

    def _resume(self, flow_ids: list[str], *, kill: bool) -> int:
        """放行/丢弃的唯一实现：三种「谁攥着这条流量」的情形都要覆盖。"""
        if not flow_ids or not self.runtime.is_running:
            return 0

        def resume() -> int:
            master = self.runtime.master
            if master is None:
                return 0
            # 网关和断点各记一本账，都要问一遍（两边对不认识的 id 都是直接忽略）。
            # 先问网关：它的挂起可能先于断点发生，而 `InterceptState.arm` 见到已被
            # 拦住的流量会退让，那条流量只在网关那本账上。
            released = master.gateway.release(flow_ids, kill=kill)
            released += master.intercept_state.release(flow_ids, kill=kill)
            # 兜底再扫一遍：两本账都不认的 `intercepted` 只能是原生 addon 自己拦的，
            # 照样得放，否则界面上点了没反应、连接却一直钉着。
            # 三项相加：账上放掉的那些已经不 `intercepted` 了，只数兜底这趟必然是 0，
            # 而界面拿这个数当「放行成功了几条」用。
            return released + self._sweep(flow_ids, kill=kill)

        return int(self.runtime.call(resume))

    def _sweep(self, flow_ids: list[str], *, kill: bool = False) -> int:
        """Resume anything still ``intercepted``. **Only ever runs on the mitm loop.**"""
        resumed = 0
        for flow_id in flow_ids:
            flow = self.view.get_by_id(flow_id)
            if flow is None or not flow.intercepted:
                continue
            # 顺序不能反：`kill()` 会把 intercepted 清成 False，而 `resume()` 开头就是
            # `if not self.intercepted: return`（与 `InterceptState._release` 同一个坑）。
            flow.resume()
            if kill and flow.killable:
                flow.kill()
            resumed += 1
        return resumed

    def revert_flow(self, flow_id: str) -> None:
        """Undo every edit made to a held flow (native ``Flow.revert``)."""
        self._mutate(flow_id, lambda flow: flow.revert(), release=False)

    def set_flow_comment(self, flow_id: str, comment: str) -> None:
        """Set a comment on a held flow."""

        def assign(flow) -> None:
            flow.comment = comment

        self._mutate(flow_id, assign, release=False)

    def apply_request_edits(
        self, flow_id: str, edit: RequestEdit, *, release: bool = False
    ) -> None:
        """Write an edited request back onto a held flow."""
        self._mutate(
            flow_id, lambda flow: apply_request_edit(flow, edit), release=release
        )

    def apply_response_edits(
        self, flow_id: str, edit: ResponseEdit, *, release: bool = False
    ) -> None:
        """Write an edited response back onto a held flow."""
        self._mutate(
            flow_id, lambda flow: apply_response_edit(flow, edit), release=release
        )

    def fake_response(
        self, flow_id: str, edit: ResponseEdit, *, release: bool = True
    ) -> None:
        """Answer a request-phase breakpoint locally, without contacting the server.

        默认顺手放行：伪造响应的整个意义就是「这条别发出去了，就这么回」，改完挂着
        不放等于什么都没发生。
        """
        self._mutate(flow_id, lambda flow: fake_response(flow, edit), release=release)

    def _mutate(self, flow_id: str, mutate, *, release: bool) -> None:
        """改一条 flow 并让流量表重绘；改失败绝不放行。

        Raises:
            RuntimeError: 内核没在跑（改活 flow 只能在 mitm 线程上做）。
            ValueError: 找不到这条流量，或编辑内容不合法。
        """
        if not self.runtime.is_running:
            raise RuntimeError(_not_running())

        def run() -> None:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                # 找不到（None）和不是 HTTP 流量，对界面是同一件事：那行已经没了。
                # 所以是 ValueError 而不是 TypeError —— 这是运行期状态，不是调用错误。
                raise ValueError(  # noqa: TRY004
                    QCoreApplication.translate(
                        "MitmFacade", "That flow is no longer in the list"
                    )
                )
            mutate(flow)
            # 原生 `View.update` 会发 sig_view_update → UiBridgeAddon → Qt 信号，
            # 行的重绘就免费拿到了。放行放在最后：写回抛错时这条流量还攥在手里，
            # 用户能改完再放，不会「改坏了还顺手发出去」。
            self.view.update([flow])
            if release:
                master = self.runtime.master
                if master is not None:
                    master.gateway.release([flow_id])
                    master.intercept_state.release([flow_id])
                self._sweep([flow_id])

        self.runtime.call(run)

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
            _snapshot(flow)
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
                        result.append(_snapshot(flow))
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
            raise RuntimeError(_not_running())
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
            raise ValueError(
                QCoreApplication.translate("MitmFacade", "That flow could not be found")
            )
        self.replay_flows([flow])

    def replay_flows(self, flows: list[HTTPFlow]) -> None:
        if not flows:
            raise ValueError(
                QCoreApplication.translate("MitmFacade", "There is no flow to resend")
            )
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "The mitmproxy core is not running, so nothing can be replayed",
                )
            )

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
                raise ValueError(
                    QCoreApplication.translate(
                        "MitmFacade", "There is nothing left to replay"
                    )
                )
            self.view.add(replay_flows)
            master.client_playback.start_replay(replay_flows)

        self.runtime.call(enqueue)

    def replay_file(self, path: Path | str) -> None:
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "The mitmproxy core is not running, so nothing can be replayed",
                )
            )
        self.runtime.call(
            lambda: master.client_playback.load_file(str(path)), timeout=10.0
        )

    def load_flow_file(self, path: Path | str) -> int:
        """Load historical flows through the native ReadFile addon."""
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "The mitmproxy core is not running, so the file cannot be read",
                )
            )

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
                master.intercept_state.release_all()
                self._sweep(list(self.view._store))
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
                master.intercept_state.release(flow_ids)
                self._sweep(flow_ids)
            current = [self.view.get_by_id(flow_id) for flow_id in flow_ids]
            self.view.remove([flow for flow in current if flow is not None])

        if self.runtime.is_running:
            self.runtime.call(remove)
        else:
            remove()
