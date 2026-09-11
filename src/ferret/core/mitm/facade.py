"""Stable application API over mitmproxy's native addons."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import HTTPFlow, View, emoji
from ferret.core.mitm.compose import COMPOSE_METADATA_KEY, build_compose_flow
from ferret.core.mitm.detail import build_flow_detail
from ferret.core.mitm.export import FlowExporter
from ferret.core.mitm.gateway import GatewayRule
from ferret.core.mitm.intercept import (
    InterceptRule,
    RequestEdit,
    ResponseEdit,
    apply_request_edit,
    apply_response_edit,
    build_request_edit,
    fake_response,
)
from ferret.core.mitm.io import FlowFile
from ferret.core.mitm.modes import (
    upstream_mode_spec,
    upstream_targets_self,
    validate_local_spec,
    validate_mode_specs,
    wireguard_client_config,
)
from ferret.core.mitm.rewrite import RewriteRule
from ferret.core.mitm.runtime import MitmRuntime
from ferret.core.mitm.sse import SseEvent
from ferret.core.mitm.wsframe import WsClose, WsFrame, ws_close, ws_frames
from ferret.core.network import LOOPBACK_HOST, detect_lan_address

# 改个名，免得和下面同名的 MitmFacade.is_lan_exposed 属性看混。
# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from ferret.core.network import is_lan_exposed as host_is_lan_exposed
from ferret.core.settings import get_certs_dir, get_sessions_dir


# 同一句在下面出现两次，写成函数而不是常量：模块级求值赶在翻译器安装之前
# （`core/application.py` 顶层就 import 了主窗口），译文会永久冻结成英文。
def _not_running() -> str:
    return QCoreApplication.translate("MitmFacade", "mitmproxy 内核未运行")


# 「已标记」写进 `flow.marked` 的值。和原生 `flow.mark.toggle` 用的是同一个
# （`mitmproxy/addons/core.py`），所以存进 `.flow` 文件之后 mitmproxy console / web
# 那边也认得，渲染成一个实心圆点；换成自造的字符串只会在别处显示成兜底符号。
MARKER_DEFAULT = ":default:"


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

    # —— 抓包通道（local / wireguard / reverse；regular 恒在，见 core/mitm/modes.py）——

    @property
    def use_local(self) -> bool:
        return self.runtime.use_local

    @property
    def local_spec(self) -> str:
        return self.runtime.local_spec

    @property
    def use_wireguard(self) -> bool:
        return self.runtime.use_wireguard

    @property
    def use_reverse(self) -> bool:
        return self.runtime.use_reverse

    @property
    def reverse_target(self) -> str:
        return self.runtime.reverse_target

    @property
    def reverse_port(self) -> int:
        return self.runtime.reverse_port

    # —— 上游代理出口：不是第五条通道，是 mode 首槽位的替换（见 modes.py）——

    @property
    def use_upstream(self) -> bool:
        return self.runtime.use_upstream

    @property
    def upstream_target(self) -> str:
        return self.runtime.upstream_target

    @property
    def upstream_username(self) -> str:
        return self.runtime.upstream_username

    @property
    def upstream_password(self) -> str:
        return self.runtime.upstream_password

    def set_channels(
        self,
        *,
        use_local: bool | None = None,
        local_spec: str | None = None,
        use_wireguard: bool | None = None,
        use_reverse: bool | None = None,
        reverse_target: str | None = None,
        reverse_port: int | None = None,
        use_upstream: bool | None = None,
        upstream_target: str | None = None,
        upstream_username: str | None = None,
        upstream_password: str | None = None,
    ) -> None:
        """Switch the capture channels and the upstream egress; hot-applies when running."""
        self.runtime.apply_channels(
            use_local=use_local,
            local_spec=local_spec,
            use_wireguard=use_wireguard,
            use_reverse=use_reverse,
            reverse_target=reverse_target,
            reverse_port=reverse_port,
            use_upstream=use_upstream,
            upstream_target=upstream_target,
            upstream_username=upstream_username,
            upstream_password=upstream_password,
        )

    def engage_channels(self) -> None:
        """Open the capture session: pull the enabled channels into the mode list."""
        self.runtime.set_channels_engaged(True)

    def disengage_channels(self) -> None:
        """Close the capture session: back to regular-only, interception off."""
        self.runtime.set_channels_engaged(False)

    def validate_local_spec(self, local_spec: str) -> None:
        """Raise ``ValueError`` with a displayable message if the filter is invalid."""
        validate_local_spec(local_spec)

    def validate_upstream_target(self, target: str) -> None:
        """Raise ``ValueError`` with a displayable message if the upstream address is invalid.

        只过原生解析器这一道（scheme 必须 http(s)、host 段不许带凭证）；自环一类
        需要知道本机监听口的判断留在对话框，那是 UI 的上下文。
        """
        validate_mode_specs([upstream_mode_spec(target)])

    def upstream_targets_self(
        self, target: str, *, listen_host: str, listen_port: int
    ) -> bool:
        """Whether the upstream address points back at ferret's own listener.

        监听地址端口由调用方传：对话框里用户可能同时在改监听口，判据得用**待提交
        的**那一组，而不是 runtime 当前值。坏目标抛 ``ValueError``（同
        ``validate_upstream_target``）。
        """
        return upstream_targets_self(
            target, listen_host=listen_host, listen_port=listen_port
        )

    def channel_health(self) -> dict[str, bool | str]:
        """Per-channel liveness of the running kernel; ``{}`` when it is not running."""
        if not self.runtime.is_running:
            return {}
        return self.runtime.call(self.runtime.channel_health)

    def wireguard_client_config(self) -> str:
        """The client profile for the WireGuard tunnel, ready to import on a phone.

        配置文件由上游在隧道启动时写进 confdir；隧道从未开过时抛
        ``FileNotFoundError``，调用方转成「先开一次抓包」的提示。
        """
        return wireguard_client_config(
            get_certs_dir() / "wireguard.conf",
            self.lan_address(),
        )

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
    def rewrite_enabled(self) -> bool:
        """重写总开关。关掉后所有重写规则一律不生效，流量原样转发。"""
        return self.runtime.rewrite_enabled

    def set_rewrite_enabled(self, enabled: bool) -> None:
        """Flip the rewrite master switch; applied immediately when it runs."""
        self.runtime.apply_rewrite_rules(enabled=enabled)

    # —— 固定会话 ——

    @property
    def sticky_session_enabled(self) -> bool:
        """固定会话开关：跨连接复用客户端的 Cookie / Authorization。

        全局行为偏好（类比无痕模式）而不是逐条规则，所以没有规则列表 ——
        原生两个 addon 的过滤串恒为「全量」，见 `core/mitm/runtime.py`。
        初值来自落盘配置（`core/runtime.py::_build_mitm_runtime`）。
        """
        return self.runtime.sticky_session_enabled

    def set_sticky_session(self, enabled: bool) -> None:
        """Flip the sticky-session switch; applied immediately when it runs."""
        self.runtime.apply_sticky_session(enabled)

    # —— 无缓存·明文 ——

    @property
    def anticache_plaintext(self) -> bool:
        """无缓存·明文开关：删条件缓存头 + Accept-Encoding=identity。

        与固定会话同类的全局行为偏好：原生 anticache / anticomp 两个 addon 合成
        一个开关、同开同关，见 `core/mitm/runtime.py`。
        """
        return self.runtime.anticache_plaintext

    def set_anticache_plaintext(self, enabled: bool) -> None:
        """Flip the anticache/anticomp switch; applied immediately when it runs."""
        self.runtime.apply_anticache_plaintext(enabled)

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

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        """Set (or clear, with ``""``) the marker on a held flow.

        照原生 `flow.mark` 命令验一遍取值：`flow.marked` 本身是个自由字符串，写什么
        都能存进 `.flow` 文件，但只有 `emoji.emoji` 表里的短码在 mitmproxy console /
        web 那边渲染得出东西，别的一律落到兜底符号。界面只会送 `MARKER_DEFAULT` 或
        空串，验的是「以后别的调用方」。

        Raises:
            ValueError: 标记值不是空串也不是认得的 emoji 短码。
        """
        if marked and marked not in emoji.emoji:
            raise ValueError(
                QCoreApplication.translate("MitmFacade", "无法识别的标记值：%s")
                % marked
            )

        def assign(flow) -> None:
            flow.marked = marked

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
                    QCoreApplication.translate("MitmFacade", "这条流量已不在列表中")
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

    def flow_detail(self, flow_id: str) -> dict[str, Any]:
        """详情面板要的整个字典，**在 mitm 线程内**构建好再交出来。

        界面早先是自己拿着活 flow 现算（`FlowTableModel._build_row_data`），既踩了
        AGENTS.md §3「不在 Qt 线程读活 flow」的红线，又把 body 美化和证书解析压在
        界面线程上。现在 Qt 侧只拿纯数据。

        代价是构建期间 mitm 的 event loop 被这一次调用占住 —— body 美化本来就有
        1 MiB 上限，量级是几十毫秒，换到的是界面不再直读活 flow。
        """

        def build() -> dict[str, Any]:
            flow = self.view.get_by_id(flow_id)
            return build_flow_detail(flow) if isinstance(flow, HTTPFlow) else {}

        return self.runtime.call(build) if self.runtime.is_running else build()

    def request_edit(self, flow_id: str) -> RequestEdit:
        """一条流量的请求压成 :class:`RequestEdit`，给 compose 页灌表单。

        提取在 mitm 线程内完成（`build_request_edit` 全程只碰副本），交出来的
        是纯数据值对象。找不到时抛错而不是返回空：prefill 没有「空表单」这个
        兜底，措辞与 `replay_flow` 一致。

        Raises:
            ValueError: 这条流量已不在列表中。
        """

        def build() -> RequestEdit:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                # ValueError 是有意的：与 `replay_flow` 同一条「找不到」契约，
                # 调用方只捕一个异常族（TRY004 想要 TypeError，那是给参数用的）。
                raise ValueError(  # noqa: TRY004
                    QCoreApplication.translate("MitmFacade", "找不到指定的 Flow")
                )
            return build_request_edit(flow)

        return self.runtime.call(build) if self.runtime.is_running else build()

    # —— WebSocket ——

    def websocket_frames(self, flow_id: str) -> list[WsFrame]:
        """Every frame captured on that flow so far; 不是 WS 流量就是空表。

        刻意**不**走 `_snapshot()`：那个是给「界面要留着一整条 flow」的路径用的
        （`all_http_flows` / `intercepted_flows`），而这里要的只是帧。`flow.copy()`
        会把每条消息连内容一起深拷一遍 —— 上千帧的行情连接白拷一份，还是为了马上
        丢掉。`ws_frames` 在 mitm 线程里就地摘成值对象，跨线程该守的（不带 flow
        引用、`bytes` 不可变）一条不少，见 `wsframe.py` 的模块 docstring。

        内核没跑时直接读：会话页那条路是从文件读回来的 flow，本来就没有 mitm 线程。
        """

        def collect() -> list[WsFrame]:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                return []
            return ws_frames(flow.websocket)

        return self.runtime.call(collect) if self.runtime.is_running else collect()

    def websocket_close(self, flow_id: str) -> WsClose:
        """Why that connection ended; 还没关（或不是 WS）时字段全是 ``None``。"""

        def collect() -> WsClose:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                return WsClose()
            return ws_close(flow.websocket)

        return self.runtime.call(collect) if self.runtime.is_running else collect()

    # —— SSE ——

    def sse_events(self, flow_id: str) -> list[SseEvent]:
        """那条流迄今 tee 出的全部事件；不是事件流就是空表。

        数据在 `FerretSseAddon` 的存档里（恒存全量，显示上限归界面），所以内核
        没跑时**没有**退化路径：addon 跟 master 一代一换，内核停了存档就没了，
        返回空表（历史流量走 `parse_sse(body)` 兑底那条路）。
        """
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            return []
        return self.runtime.call(lambda: master.sse.events(flow_id))

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

    def visible_http_flows(self) -> list[HTTPFlow]:
        """**过滤后可见列表**的活引用，给流量表刷新行集用（`handle_refresh`）。

        与 `all_http_flows` 的两点分工，都不是可有可无的：
        * 迭代 ``self.view``（即 `View._view`，`set_filter` 之后只剩可见行），
          而那个走 `_store` 全量 —— 表格行集对应的就是可见列表，接全量会让
          显示过滤静默失效（过滤唯一的生效路径就是 refresh 重建行集）；
        * 刻意不 `_snapshot()`：表格靠活引用与桥接信号送来的**同一实例**做身份
          匹配（`handle_update` / `handle_remove` 反查行），换副本会让 refresh
          之后到达的更新全部落空。迭代本身在 mitm 线程内完成（`runtime.call`），
          Qt 线程只持有结果 —— 与信号路径交付活 flow 是同一种暴露。
        """
        visible = lambda: [f for f in self.view if isinstance(f, HTTPFlow)]
        return self.runtime.call(visible) if self.runtime.is_running else visible()

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

    def get_request_body(self, flow_id: str) -> bytes:
        """请求体的**解压后**字节；与 ``get_raw_*`` 的线上字节是两个口径。

        解压在 mitm 线程内完成（``get_content`` 摸的是活 message，同
        ``flow_detail`` 的先例），Qt 侧只拿现成 bytes。无体 / 挂起 → ``b""``。
        """
        return self._export(flow_id, FlowExporter.request_body, b"")

    def get_response_body(self, flow_id: str) -> bytes:
        """响应体的**解压后**字节；语义与 ``get_request_body`` 逐字相同。"""
        return self._export(flow_id, FlowExporter.response_body, b"")

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
                QCoreApplication.translate("MitmFacade", "找不到指定的 Flow")
            )
        self.replay_flows([flow])

    def replay_flows(self, flows: list[HTTPFlow]) -> None:
        if not flows:
            raise ValueError(
                QCoreApplication.translate("MitmFacade", "没有可重发的 Flow")
            )
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "mitmproxy 内核未运行，无法回放",
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
                    QCoreApplication.translate("MitmFacade", "无可回放的 Flow")
                )
            self.view.add(replay_flows)
            master.client_playback.start_replay(replay_flows)

        self.runtime.call(enqueue)

    def send_custom_request(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]] | None = None,
        content: bytes | str | None = None,
        *,
        record: bool = True,
    ) -> str:
        """编辑页「发送」：徒手造一条 flow 交给 `ClientPlayback` 发出，返回 flow id。

        与 `replay_flows` 同一条路（重写 / 网关 / 断点规则照常命中）。`record`
        决定是否留在流量列表：不留的那条由 `ComposeAddon` 在 View 收录后摘除。
        响应 / 错误落地后经 `MitmRuntime.compose_result` 信号回报编辑页。
        """
        if not method.strip():
            raise ValueError(QCoreApplication.translate("MitmFacade", "HTTP 方法为空"))
        if not url.strip():
            raise ValueError(QCoreApplication.translate("MitmFacade", "URL 为空"))
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "mitmproxy 内核未运行，无法发送",
                )
            )

        def enqueue() -> str:
            flow = build_compose_flow(method, url, headers, content)
            flow.metadata[COMPOSE_METADATA_KEY] = "1"
            flow.is_replay = "request"
            master.compose.register(flow.id, keep=record)
            # `start_replay` 自己会 backup / 清响应 / 入队；URL 不合法等构造错误
            # 在这一步之前就已经抛出，登记表不会被弄脏。
            master.client_playback.start_replay([flow])
            return flow.id

        return str(self.runtime.call(enqueue))

    def replay_file(self, path: Path | str) -> None:
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmFacade",
                    "mitmproxy 内核未运行，无法回放",
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
                    "mitmproxy 内核未运行，无法读取文件",
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
                master.sse.clear()
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
                for flow_id in flow_ids:
                    master.sse.forget(flow_id)
            current = [self.view.get_by_id(flow_id) for flow_id in flow_ids]
            self.view.remove([flow for flow in current if flow is not None])

        if self.runtime.is_running:
            self.runtime.call(remove)
        else:
            remove()
