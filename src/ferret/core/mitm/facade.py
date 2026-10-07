"""Stable application API over mitmproxy's native addons."""

from __future__ import annotations

import os
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from PySide6.QtCore import QCoreApplication

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    FlowReadException,
    HTTPFlow,
    OptionsError,
    View,
    emoji,
    human,
)
from ferret.core.mitm.compose import (
    COMPOSE_METADATA_KEY,
    COMPOSE_RECORD_METADATA_KEY,
    build_compose_flow,
    compose_recording,
)
from ferret.core.mitm.detail import (
    build_flow_body,
    build_flow_detail,
    build_flow_messages,
    build_flow_overview_metadata,
    build_flow_summary,
)
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
from ferret.core.mitm.io import FlowFile, flow_import
from ferret.core.mitm.modes import (
    ensure_wireguard_conf,
    upstream_mode_spec,
    upstream_targets_self,
    validate_local_spec,
    validate_mode_specs,
    wireguard_client_config,
)
from ferret.core.mitm.rewrite import RewriteRule
from ferret.core.mitm.rows import FlowRow, flow_row
from ferret.core.mitm.runtime import MitmRuntime
from ferret.core.mitm.scripts import ScriptEntry, ScriptStatus
from ferret.core.mitm.sse import SseEvent
from ferret.core.mitm.wsframe import WsClose, WsFrame, ws_close, ws_frames
from ferret.core.network import LOOPBACK_HOST, detect_lan_address

# 改个名，免得和下面同名的 MitmFacade.is_lan_exposed 属性看混。
# ruff 默认 combine-as-imports = false，`as` 导入只能单独成句。
from ferret.core.network import is_lan_exposed as host_is_lan_exposed
from ferret.core.settings import get_certs_dir, get_mock_pool_file, get_sessions_dir


# 同一句在下面出现两次，写成函数而不是常量：模块级求值赶在翻译器安装之前
# （`core/application.py` 顶层就 import 了主窗口），译文会永久冻结成英文。
def _not_running() -> str:
    return QCoreApplication.translate("MitmFacade", "mitmproxy 内核未运行")


def _reserve_recording_path(started_at: datetime) -> Path:
    """唯一且防竞争地占住录制文件名，返回已创建的空占位文件（#74）。

    文件名只有秒精度：同一秒内 stop→start 会撞名，原生 Save 以 wb 重开就把
    上一段录制整个截掉了；时钟回拨同理。这里先用 ``O_CREAT|O_EXCL`` 把名字
    抢下来再下发 —— 占位是空文件，Save 随后的 wb 截断等于无操作；名字被占
    就加 ``-2``/``-3`` 后缀重试（同名越多后缀越大），极端密集才退到 uuid。

    占位后没能真正开录的空文件由调用方删除，录制期间无数据的空文件由
    ``stop_capture_recording`` 的空文件清理兜底。
    """
    directory = get_sessions_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"capture-{started_at:%Y%m%d-%H%M%S}"
    names = [f"{stem}.flow", *(f"{stem}-{n}.flow" for n in range(2, 100))]
    names.append(f"{stem}-{uuid4().hex[:8]}.flow")
    for name in names:
        candidate = directory / name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise RuntimeError("could not reserve a unique recording path")  # pragma: no cover


# 「已标记」写进 `flow.marked` 的值。和原生 `flow.mark.toggle` 用的是同一个
# （`mitmproxy/addons/core.py`），所以存进 `.flow` 文件之后 mitmproxy console / web
# 那边也认得，渲染成一个实心圆点；换成自造的字符串只会在别处显示成兜底符号。
MARKER_DEFAULT = ":default:"

log = get_logger("mock")


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
        # mock 池的托管文件在应用启动时读回（docs/design.md#mock）：
        # 反序列化出来的是死对象，与 sessions 加载 .flow 同一安全等级，不需要等
        # 内核起来。文件缺省/损坏都从空池起步（read_valid_prefix 容忍尾部截断），
        # 下一次池变更会把好内容写回去。
        pool_path = get_mock_pool_file()
        self.runtime.mock_pool = (
            [
                flow
                for flow in FlowFile.read_valid_prefix(pool_path)
                if isinstance(flow, HTTPFlow) and flow.response is not None
            ]
            if pool_path.exists()
            else []
        )

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

    # —— 抓包通道（local / wireguard / reverse / socks5；regular 恒在）——

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

    @property
    def use_socks5(self) -> bool:
        return self.runtime.use_socks5

    @property
    def socks5_port(self) -> int:
        return self.runtime.socks5_port

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
        use_socks5: bool | None = None,
        socks5_port: int | None = None,
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
            use_socks5=use_socks5,
            socks5_port=socks5_port,
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

        用户勾上 WireGuard 的那刻就该能拿到码：文件不存在时现场生成（与内核
        ``_start`` 同格式，后到者复用），不再需要「先开始抓包一次」。文件损坏
        或目录不可写时抛已翻译的 ``OSError`` / ``ValueError`` 文案。
        """
        conf_path = get_certs_dir() / "wireguard.conf"
        try:
            ensure_wireguard_conf(conf_path)
        except OSError as exc:
            raise OSError(
                QCoreApplication.translate(
                    "MitmFacade", "无法写入 WireGuard 密钥文件：{}"
                ).format(exc)
            ) from exc
        try:
            return wireguard_client_config(conf_path, self.lan_address())
        except (ValueError, KeyError, TypeError) as exc:
            # 坏 JSON → JSONDecodeError(ValueError)；缺键 → KeyError；键不是字符串
            # → TypeError；密钥非法 → rs_wireguard.pubkey 的 ValueError。四种都是
            # 同一回事：这份文件没救了，删掉重新生成。
            raise ValueError(
                QCoreApplication.translate(
                    "MitmFacade", "WireGuard 密钥文件已损坏，删除 {} 后重试"
                ).format(conf_path)
            ) from exc

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

    # —— 用户脚本 ——

    @property
    def scripts(self) -> list[ScriptEntry]:
        """脚本清单（内存副本）。装载状态变更走 `runtime.script_status_changed`。"""
        return list(self.runtime.scripts)

    def set_scripts(self, entries: list[ScriptEntry]) -> None:
        """Replace the script entries; applied immediately when the kernel runs."""
        self.runtime.apply_scripts(entries)

    @property
    def scripts_enabled(self) -> bool:
        """脚本总开关。关掉后所有脚本一律不装载、不参与流量处理。"""
        return self.runtime.scripts_enabled

    def set_scripts_enabled(self, enabled: bool) -> None:
        """Flip the scripts master switch; applied immediately when it runs."""
        self.runtime.apply_scripts(enabled=enabled)

    def reload_script(self, path: str) -> None:
        """强制重载一条脚本；内核没跑是 no-op。"""
        self.runtime.reload_script(path)

    @property
    def script_statuses(self) -> dict[str, ScriptStatus]:
        """各条脚本的装载状态快照；内核没跑返回空表。"""
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            return {}
        return self.runtime.call(lambda: dict(master.scripts.statuses))

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

    # —— 协议层（docs/design.md#capture）——

    @property
    def http2_enabled(self) -> bool:
        """是否支持 HTTP/2（内存副本，原生默认开）。

        关掉是调试降级手段：TLS 连接一律走 HTTP/1.1，报文按行可读。与恒开的
        DisableH2C 垫片正交 —— 明文 h2c 升级本就（无条件）被剥离，与本开关无关。
        """
        return self.runtime.http2_enabled

    @property
    def http3_enabled(self) -> bool:
        """是否支持 QUIC / HTTP/3（内存副本，原生默认开）。

        关掉后原生不建 QUIC 层、并顺带抹隧道内 DNS HTTPS 记录里的 h3 ALPN，
        客户端回落 HTTP/2 (TCP)；HTTP 响应里的 alt-svc 头不摘，已缓存的客户端
        可能先试一次再回落 —— 预期行为。
        """
        return self.runtime.http3_enabled

    def set_protocol_options(
        self,
        *,
        http2: bool | None = None,
        http3: bool | None = None,
    ) -> None:
        """Update the protocol switches; applied immediately when the kernel runs.

        两个参数都是 **None = 不改动该项**。内核没跑只对齐内存副本，下发失败回滚
        后抛 ``ValueError`` / 原生异常（bool 无坏值路径，照 ``apply_anticache_plaintext``
        的错误模型），调用方自行决定是否落盘。
        """
        self.runtime.apply_protocol_options(http2=http2, http3=http3)

    # —— DNS 解析 ——

    @property
    def dns_name_servers(self) -> list[str]:
        """自定义 DNS 服务器（内存副本）。空列表 = 跟随系统 DNS。

        全局偏好（见 `core/mitm/runtime.py`），仅对隧道内 DNS 生效
        （WireGuard 通道）。初值来自落盘配置（`core/runtime.py`）。
        """
        return list(self.runtime.dns_name_servers)

    @property
    def dns_use_hosts_file(self) -> bool:
        """解析时是否查询操作系统 hosts 文件（原生默认开）。"""
        return self.runtime.dns_use_hosts_file

    def set_dns_options(
        self,
        *,
        name_servers: list[str] | None = None,
        use_hosts_file: bool | None = None,
    ) -> None:
        """Update the DNS options; applied immediately when the kernel runs.

        两个参数都是 **None = 不改动该项**，清空自定义 DNS 传 ``[]``。坏 IP 串抛
        ``ValueError`` 且不落盘（调用方负责只在校验通过后写 CONFIG）。
        """
        self.runtime.apply_dns_options(
            name_servers=name_servers, use_hosts_file=use_hosts_file
        )

    # —— 上游 TLS 信任 ——

    @property
    def ssl_insecure(self) -> bool:
        """不校验上游服务器证书（内存副本，原生默认关）。

        开启会顺带允许不安全重协商（原生把同一个值喂给 `legacy_server_connect`），
        文案里已写明 —— 老 Java / CBS 服务端有时正是靠这层才连得上。
        """
        return self.runtime.ssl_insecure

    @property
    def trusted_ca_files(self) -> list[str]:
        """额外信任的 CA 证书文件路径（内存副本）。空列表 = 只认公共根。

        刻意只给**用户填的路径**，不暴露合并产物：那份 PEM 是生成物，带内容指纹、
        随时会被换掉（见 `core/mitm/certificate.py::build_trusted_ca_bundle`），
        界面拿到也只会拿去显示一个随时失效的路径。
        """
        return list(self.runtime.ssl_trusted_ca_files)

    @property
    def add_upstream_certs_to_client_chain(self) -> bool:
        """向客户端拼接上游真实证书链（内存副本，原生默认关）。"""
        return self.runtime.add_upstream_certs_to_client_chain

    def set_upstream_tls_options(
        self,
        *,
        insecure: bool | None = None,
        trusted_ca_files: list[str] | None = None,
        add_upstream_certs: bool | None = None,
    ) -> None:
        """Update the upstream-TLS trust options; applied immediately when running.

        三个参数都是 **None = 不改动该项**，清空信任文件传 ``[]``。内核拒绝时抛
        ``ValueError``、写盘失败时抛 ``CertificateError``（`RuntimeError` 的子类），
        两种情形都**不落盘**（调用方负责只在这里没抛之后才写 CONFIG）。

        解不动的信任文件**不算坏值**、不抛异常：整批里能用的照常生效，全坏就退回
        公共根。要在界面上显示「N 个文件已失效」请另调
        `core.mitm.certificate.inspect_trusted_ca_files`（只读、不写产物）。
        """
        self.runtime.apply_ssl_options(
            insecure=insecure,
            trusted_ca_files=trusted_ca_files,
            add_upstream_certs=add_upstream_certs,
        )

    # —— mTLS 客户端证书 ——

    @property
    def client_certs_path(self) -> str:
        """向上游出示的客户端证书路径（内存副本）。空串 = 未启用。

        原样返回用户填的那个字符串（可能带 ``~``）：原生自己展开，我们不替它展开，
        免得界面回读到一个与配置对不上的路径。形态（单文件 / 按主机目录）不单独记
        —— 它由磁盘现状决定，要盘点请调 `core.mitm.certificate.inspect_client_certs`。
        """
        return self.runtime.client_certs_path

    def set_client_certs(self, path: str) -> None:
        """Point mitmproxy at a client certificate; applied immediately when running.

        空串 = 清除（下发原生默认 ``None``）。路径不存在、或单文件模式下内容不可用
        （没私钥 / 没证书 / 私钥加密 / 私钥与证书不配对）时抛 ``ValueError`` 且
        **不落盘** —— 调用方负责只在这里没抛之后才写 CONFIG。

        与上游信任那刀的口径不同：那边坏文件可以退回公共根，这边没有降级余地，
        坏值放过去就是一次「配了却不出示」的静默失败，所以前置拦下。目录模式只查
        存在性，里面的坏文件由 `core.mitm.certificate.inspect_client_certs` 显示，
        不拦保存。
        """
        self.runtime.apply_client_certs(path=path)

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

    def kill_flow(self, flow_id: str) -> None:
        """Kill a single flow: native ``flow.kill()`` semantics（TCP RST、不再转发）。

        已不可杀的 flow（已 kill / 已出错）静默跳过，不抛错 —— 右键菜单「点了没反应」
        好过弹一个 ControlException。

        挂起中的绝不能直接 ``kill()``：``kill()`` 会把 ``intercepted`` 清成 False，
        而 ``resume()`` 开头就是 ``if not self.intercepted: return`` —— 那条连接会
        永久钉死在 ``wait_for_resume()`` 上，之后连「全部放行」也救不回来（账上的
        ``release`` 也是先 ``resume()``，照样落空）。所以先走两本账的放行（它们内部
        是先 resume 再 kill），再兜底扫原生拦的，最后才补杀没被任何人攥着的活流。
        """
        if not self.runtime.is_running:
            raise RuntimeError(_not_running())

        def run() -> None:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                # 与 `_mutate` 同一条判据：找不到和不是 HTTP 流量，对界面是同一件事。
                raise ValueError(  # noqa: TRY004
                    QCoreApplication.translate("MitmFacade", "这条流量已不在列表中")
                )
            master = self.runtime.master
            if master is not None:
                master.gateway.release([flow_id], kill=True)
                master.intercept_state.release([flow_id], kill=True)
            self._sweep([flow_id], kill=True)
            if flow.killable:
                flow.kill()
            self.view.update([flow])

        self.runtime.call(run)

    def set_flow_comment(self, flow_id: str, comment: str) -> None:
        """Set a comment on a held flow."""

        def assign(flow) -> None:
            flow.comment = comment

        self._mutate(flow_id, assign, release=False)

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        """Set (or clear, with ``""``) the marker on a held flow.

        照原生 `flow.mark` 命令验一遍取值：`flow.marked` 本身是个自由字符串，写什么
        都能存进 `.flow` 文件，但只有 `emoji.emoji` 表里的短码在 mitmproxy console /
        web 那边渲染得出东西，别的一律落到兜底符号。界面送的是选择器里挑出来的
        短码（`apps/common/flow/marks.py`）或空串，验的是「以后别的调用方」。

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

    @property
    def proxyauth_enabled(self) -> bool:
        """连接本代理是否需要用户名密码（原生 ProxyAuth addon 的 `proxyauth`）。

        这是**意图值**：local / wireguard / reverse 接通期间内核侧会自动让路
        （`MitmRuntime._effective_proxyauth`），此处仍回用户配置的原值。
        """
        return self.runtime.proxyauth_enabled

    @property
    def proxyauth_username(self) -> str:
        """代理认证用户名；不得含冒号（原生按 `split(":")` 切两段）。"""
        return self.runtime.proxyauth_username

    @property
    def proxyauth_password(self) -> str:
        """代理认证密码；可为空，同样不得含冒号。"""
        return self.runtime.proxyauth_password

    def set_proxy_auth(
        self,
        *,
        enabled: bool | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Update the inbound proxy credential; applied immediately when the kernel runs."""
        self.runtime.apply_proxy_auth(
            enabled=enabled, username=username, password=password
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

    def flow_summary(self, flow_id: str) -> dict[str, Any]:
        """选中行时只取轻量字段，不解码 body 或遍历消息。"""

        def build() -> dict[str, Any]:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                return {}
            data = build_flow_summary(flow)
            master = self.runtime.master
            if data["message_kind"] == "sse" and master is not None:
                count = master.sse.event_count(flow_id)
                if count is not None:
                    data["message_count"] = count
            return data

        return self.runtime.call(build) if self.runtime.is_running else build()

    def flow_body(
        self, flow_id: str, side: Literal["Request", "Response"]
    ) -> dict[str, Any]:
        """打开一侧 Body 页时，才在 mitm 线程构建这一侧的派生数据。"""

        def build() -> dict[str, Any]:
            flow = self.view.get_by_id(flow_id)
            return build_flow_body(flow, side) if isinstance(flow, HTTPFlow) else {}

        return self.runtime.call(build) if self.runtime.is_running else build()

    def flow_overview_metadata(self, flow_id: str) -> dict[str, Any]:
        """概览需要的精确修改状态；其原生 backup 比较也在 mitm 线程完成。"""

        def build() -> dict[str, Any]:
            flow = self.view.get_by_id(flow_id)
            return (
                build_flow_overview_metadata(flow) if isinstance(flow, HTTPFlow) else {}
            )

        return self.runtime.call(build) if self.runtime.is_running else build()

    def flow_messages(self, flow_id: str) -> dict[str, Any]:
        """消息页一次取齐消息和结束状态，避免跨两次 call 读出不一致快照。"""

        def build() -> dict[str, Any]:
            flow = self.view.get_by_id(flow_id)
            if not isinstance(flow, HTTPFlow):
                return build_flow_messages(None)
            events = None
            master = self.runtime.master
            if master is not None and master.sse.event_count(flow_id) is not None:
                events = master.sse.events(flow_id)
            return build_flow_messages(flow, events)

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
        """那条流容量以内的最近事件；不是事件流就是空表。

        数据在 `FerretSseAddon` 的有界存档里，所以内核
        没跑时**没有**退化路径：addon 跟 master 一代一换，内核停了存档就没了，
        返回空表（历史流量走 `parse_sse(body)` 兑底那条路）。
        """
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            return []
        return self.runtime.call(lambda: master.sse.events(flow_id))

    def total_count(self, flow_ids: Collection[str] | None = None) -> int:
        count = lambda: sum(
            isinstance(flow, HTTPFlow)
            and compose_recording(flow) is not False
            and (flow_ids is None or flow.id in flow_ids)
            for flow in self.view._store.values()
        )
        return int(self.runtime.call(count)) if self.runtime.is_running else count()

    def all_http_flows(self, flow_ids: Collection[str] | None = None) -> list[HTTPFlow]:
        snapshot = lambda: [
            _snapshot(flow)
            for flow in self.view._store.values()
            if isinstance(flow, HTTPFlow) and (flow_ids is None or flow.id in flow_ids)
        ]
        return self.runtime.call(snapshot) if self.runtime.is_running else snapshot()

    def http_flow_ids(self) -> set[str]:
        collect = lambda: {
            flow.id for flow in self.view._store.values() if isinstance(flow, HTTPFlow)
        }
        return self.runtime.call(collect) if self.runtime.is_running else collect()

    def visible_flow_rows(
        self, flow_ids: Collection[str] | None = None
    ) -> list[FlowRow]:
        """**过滤后可见列表**的行快照，给流量表刷新行集用（`handle_refresh`）。

        与 `all_http_flows` 的两点分工，都不是可有可无的：
        * 迭代 ``self.view``（即 `View._view`，`set_filter` 之后只剩可见行），
          而那个走 `_store` 全量 —— 表格行集对应的就是可见列表，接全量会让
          显示过滤静默失效（过滤唯一的生效路径就是 refresh 重建行集）；
        * 在 mitm 线程内迭代**并当场折成 `FlowRow`**（#90）：跨线程交付的是
          不可变快照，Qt 侧的 refresh 行集与桥接信号送来的行是同一套数据形状，
          但都不再是活引用。
        """
        # 不记录的 Compose 在途时仍必须留在核心 View（提前移除会 kill），
        # 但刷新/切换过滤条件不能把它们重新带进表格。
        visible = lambda: [
            flow_row(f)
            for f in self.view
            if isinstance(f, HTTPFlow)
            and compose_recording(f) is not False
            and (flow_ids is None or f.id in flow_ids)
        ]
        return self.runtime.call(visible) if self.runtime.is_running else visible()

    def match_ids(self, matcher) -> set[str]:
        """在 mitm 线程内跑 matcher，返回命中的 flow.id 集（搜索高亮用）。

        与 `visible_flow_rows` 同一条「重活投 mitm 线程」的路：flowfilter 的
        `~b`/`~bq`/`~bs` 算子会去读 flow 的 body，只有在 mitm 线程读才安全
        （AGENTS.md §3 红线）。Qt 侧拿到 id 集后只做 `flow.id in ids` 的 O(1) 查表。

        高亮模式已清掉用户过滤，`self.view` 恰是「全部 ~http」——命中集与表格所见一致。
        """
        run = lambda: {
            f.id
            for f in self.view
            if isinstance(f, HTTPFlow)
            and compose_recording(f) is not False
            and matcher(f)
        }
        return self.runtime.call(run) if self.runtime.is_running else run()

    def set_filter(self, flow_filter) -> None:
        if self.runtime.is_running:
            self.runtime.call(lambda: self.view.set_filter(flow_filter))
        else:
            self.view.set_filter(flow_filter)

    def save_flows(self, flow_ids: Collection[str], path: str | Path) -> int:
        """按 id 批量写 `.flow` 文件；快照在 mitm 线程内解析（#90）。

        内核没在跑时从本 facade 的 View 取（死对象，直读安全）——只在测试/离线
        装配里出现。
        """

        def snapshot() -> list[HTTPFlow]:
            result = []
            for flow_id in flow_ids:
                flow = self.view.get_by_id(flow_id)
                if isinstance(flow, HTTPFlow):
                    result.append(_snapshot(flow))
            return result

        flows = self.runtime.call(snapshot) if self.runtime.is_running else snapshot()
        return FlowFile.write(path, flows)

    def get_httpie_command(self, flow_id: str) -> str:
        return self._export(flow_id, FlowExporter.httpie_command, "")

    def get_curl_command(self, flow_id: str) -> str:
        return self._export(flow_id, FlowExporter.curl_command, "")

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

    def export_har(self, flow_ids: Collection[str], path: str) -> None:
        """按 id 批量导出 HAR；快照与写盘都在 mitm 线程内完成（#90）。

        旧签名收 flow 对象、在**调用线程**上读字段导出——那是表格外的另一处
        跨线程读，一并收进 id 寻址。内核没在跑时直读本 facade 的 View（死对象）。
        """

        def snapshot() -> list[HTTPFlow]:
            result = []
            for flow_id in flow_ids:
                flow = self.view.get_by_id(flow_id)
                if isinstance(flow, HTTPFlow):
                    result.append(_snapshot(flow))
            return result

        flows = (
            self.runtime.call(snapshot) if self.runtime.is_running else snapshot()
        )
        FlowExporter.save_har(flows, path)

    def _export(self, flow_id: str, exporter, default):
        def export():
            flow = self.view.get_by_id(flow_id)
            return exporter(flow) if isinstance(flow, HTTPFlow) else default

        return self.runtime.call(export) if self.runtime.is_running else export()

    def start_capture_recording(self) -> Path:
        if not self.runtime.is_running:
            raise RuntimeError(_not_running())
        # 占位文件先落（唯一性在 Qt 侧定好）；幂等命中时在下方删掉。
        path = _reserve_recording_path(datetime.now().astimezone())
        current = self._recording_path

        def engage() -> str | None:
            # options 的读与写在同一个 call 闭包里完成：master.options 属内核
            # 线程，Qt 线程裸读会踩线程纪律，且读/写拆两次 call 可能读到别处
            # 改了一半的值。master=None 是内核恰好退出的竞态兜底。
            master = self.runtime.master
            if master is None:
                return None
            if current is not None and master.options.save_stream_file == str(current):
                # 同一内核已指向同一录制文件时按幂等处理：抓包中重挂系统代理会
                # 再次调进来（录制已与系统代理勾选解耦），原生 Save 换路径会先
                # 关旧流再开新文件，把一段会话拆成两个 capture 文件。内核换血后
                # options 是全新的（save_stream_file=None），落不到本分支，
                # 照常重开文件。
                return str(current)
            master.options.update(
                save_stream_file=str(path), save_stream_filter="~http"
            )
            return str(path)

        try:
            engaged = self.runtime.call(engage)
        except BaseException:
            # 下发失败时把占位空文件删掉，别在会话目录里留垃圾。
            path.unlink(missing_ok=True)
            raise
        if engaged is None:
            path.unlink(missing_ok=True)
            raise RuntimeError(_not_running())
        self._recording_path = Path(engaged)
        if engaged != str(path):
            # 幂等命中：占位空文件没用上，删掉。
            path.unlink(missing_ok=True)
        return self._recording_path

    def stop_capture_recording(self) -> Path | None:
        path = self._recording_path
        master = self.runtime.master
        if self.runtime.is_running and master is not None:

            def stop() -> None:
                # configure() is dispatched through addon safecall, which logs
                # and swallows I/O errors. Flush directly so a failed recording
                # close reaches the caller and retains the stream for retry.
                master.save.done()
                master.options.update(save_stream_file=None)

            self.runtime.call(stop)
        self._recording_path = None
        if path is not None and path.exists() and path.stat().st_size == 0:
            path.unlink(missing_ok=True)
        return path

    def replay_flow(self, flow_id: str) -> None:
        self.replay_flows([flow_id])

    def replay_flows(self, flow_ids: Collection[str]) -> None:
        """按 id 批量重发；解析与入队都在 mitm 线程内完成（#90）。

        id 解析不到（流量已被删/清）时静默跳过，与 ``check()`` 的在途跳过同一
        语义；全部落空才报错。
        """
        if not flow_ids:
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
            for flow_id in flow_ids:
                flow = self.view.get_by_id(flow_id)
                if not isinstance(flow, HTTPFlow):
                    continue
                if master.client_playback.check(flow) is not None:
                    continue
                replay = flow.copy()
                # 普通重发沿用抓包闸门，不继承原 Compose 的记录选择。
                replay.metadata.pop(COMPOSE_RECORD_METADATA_KEY, None)
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
        timeout: float | None = None,
    ) -> str:
        """编辑页「发送」：徒手造一条 flow 交给原生 ReplayHandler，返回 flow id。

        与 `replay_flows` 同一条原生路径（重写 / 网关生效，断点排除回放）。`record`
        决定是否进入流量列表，独立于抓包开关；不记录的请求在途时对表格隐藏，
        落地后由 `ComposeAddon` 从核心 View 摘除。
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
            flow.metadata[COMPOSE_RECORD_METADATA_KEY] = record
            flow.is_replay = "request"
            # 构造验证已完成，ComposeAddon 持有原生 handler 的可取消任务。
            master.compose.start(flow, master.options, keep=record, timeout=timeout)
            return flow.id

        return str(self.runtime.call(enqueue))

    def cancel_custom_request(self, flow_id: str) -> bool:
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            return False
        return bool(self.runtime.call(lambda: master.compose.cancel(flow_id)))

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

    def load_flow_file(
        self, path: Path | str, *, imported_ids: set[str] | None = None
    ) -> int:
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
            # Do not pause the shared Save stream: unrelated network tasks can
            # complete during ReadFile's awaits. Only this task's historical
            # lifecycle hooks are excluded by FerretSave.
            with flow_import(imported_ids):
                return await master.readfile.load_flows_from_path(str(path))

        return int(self.runtime.call(load, timeout=30.0))

    # —— mock 响应池（原生 ServerPlayback，docs/design.md#mock）——
    # 池的权威副本在 `runtime.mock_pool`，addon 的 flowmap 由本方法组负责同步
    # （增量变更 add_flows、删减/开关 load_flows 整表重建）。所有**变更**都要求
    # 内核在跑（副本必须在 mitm 线程上做）；删减/清空在内核没跑时也对池内死对象
    # 就地执行 —— 与 sessions 加载 .flow 同一安全等级。

    def _persist_mock_pool(self) -> None:
        """池写回托管 .flow 文件（mitm 线程或内核停止时的调用线程）。

        失败只记日志不连坐操作：内存与内核都已变更，落盘失败留给下一次池变更重试，
        报给界面反而要面对「操作到底成没成」的说不清。
        """
        try:
            FlowFile.write(get_mock_pool_file(), self.runtime.mock_pool)
        except OSError as exc:
            log.warning("mock 池落盘失败: %s", exc)

    def add_mock_flows(self, flow_ids: list[str]) -> int:
        """把流量列表选中的流做成副本加入 mock 池，返回实收条数。

        只有带响应的 HTTPFlow 有资格进池（原生 `next_flow` 会跳过无响应源流）；
        同一条流重复加入按已存在跳过。副本在 mitm 线程上做（桥接红线），并照
        `_snapshot` 的手法把 `copy()` 换掉的 id 补回 —— 池条目的身份就是来源
        流量行的 id，界面按它删除。池条目不进 View，不会有 id 撞车问题。
        """
        if not flow_ids:
            return 0
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(_not_running())
        runtime = self.runtime

        def add() -> int:
            seen = {flow.id for flow in runtime.mock_pool}
            added: list[HTTPFlow] = []
            for flow_id in flow_ids:
                flow = self.view.get_by_id(flow_id)
                if not isinstance(flow, HTTPFlow) or flow.response is None:
                    continue
                if flow.id in seen:
                    continue
                seen.add(flow.id)
                copy = flow.copy()
                copy.id = flow.id
                added.append(copy)
            if not added:
                return 0
            runtime.mock_pool.extend(added)
            if runtime.mock_enabled:
                master.server_playback.add_flows(added)
            self._persist_mock_pool()
            return len(added)

        return int(self.runtime.call(add, timeout=10.0))

    def add_mock_file(self, path: Path | str) -> int:
        """从 .flow 文件导入 mock 源（过滤/去重规则同 `add_mock_flows`）。"""
        master = self.runtime.master
        if not self.runtime.is_running or master is None:
            raise RuntimeError(_not_running())
        runtime = self.runtime

        def add() -> int:
            flows = FlowFile.read(path)
            seen = {flow.id for flow in runtime.mock_pool}
            added: list[HTTPFlow] = []
            for flow in flows:
                if not isinstance(flow, HTTPFlow) or flow.response is None:
                    continue
                if flow.id in seen:
                    continue
                seen.add(flow.id)
                added.append(flow)
            if not added:
                return 0
            runtime.mock_pool.extend(added)
            if runtime.mock_enabled:
                master.server_playback.add_flows(added)
            self._persist_mock_pool()
            return len(added)

        try:
            return int(self.runtime.call(add, timeout=30.0))
        except FlowReadException as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "MitmFacade", "无法读取 Flow 文件：{}"
                ).format(exc)
            ) from exc

    def remove_mock_flows(self, entry_ids: list[str]) -> int:
        """按池条目 id（= 来源流量 id）删除，返回删除数。

        素材池与运行队列分别删除，不能从素材池重建并复活已消费的条目。
        """
        runtime = self.runtime
        doomed = set(entry_ids)

        def remove() -> int:
            kept = [flow for flow in runtime.mock_pool if flow.id not in doomed]
            removed = len(runtime.mock_pool) - len(kept)
            if not removed:
                return 0
            runtime.mock_pool = kept
            master = runtime.master
            if runtime.is_running and master is not None and runtime.mock_enabled:
                master.server_playback.remove_entries(doomed)
            self._persist_mock_pool()
            return removed

        if runtime.is_running:
            return int(runtime.call(remove))
        return remove()

    def clear_mock(self) -> None:
        """清空 mock 池。总开关开着时这就是「关闭」的原生语义（flowmap 变空）。"""
        runtime = self.runtime

        def clear() -> None:
            runtime.mock_pool = []
            master = runtime.master
            if runtime.is_running and master is not None:
                master.server_playback.clear()
            self._persist_mock_pool()

        if runtime.is_running:
            runtime.call(clear)
        else:
            clear()

    def export_mock_pool(self, path: Path | str) -> int:
        """把整个池导出为 .flow 文件（用户手动留档的出口），返回条数。"""
        runtime = self.runtime

        def export() -> int:
            return FlowFile.write(path, runtime.mock_pool)

        try:
            if runtime.is_running:
                return int(runtime.call(export))
            return export()
        except OSError as exc:
            raise RuntimeError(
                QCoreApplication.translate("MitmFacade", "无法写入文件：{}").format(exc)
            ) from exc

    def set_mock_enabled(self, enabled: bool) -> None:
        """总开关。存内存副本 + 推给运行中的内核（开=整表装载，关=clear）。"""
        self.runtime.apply_mock_enabled(enabled)

    def set_mock_knobs(self, knobs: dict[str, Any]) -> None:
        """更新 mock 旋钮（键 = 原生 server_replay_* 选项名）。坏值抛 ValueError。"""
        try:
            self.runtime.apply_mock_knobs(knobs)
        except OptionsError as exc:
            raise ValueError(str(exc)) from exc

    def mock_snapshot(self) -> dict[str, Any]:
        """响应池的纯数据快照（方法/URL/状态/大小），Qt 侧不碰池内 flow。

        总在 mitm 线程上读（池内 flow 在内核跑着时是活对象）；内核没跑时池里是
        死对象，就地读（同 `get_flow` 的两路姿态）。
        """
        runtime = self.runtime

        def build() -> dict[str, Any]:
            entries = []
            for flow in runtime.mock_pool:
                response = flow.response
                content = (
                    response.get_content(strict=False) if response is not None else b""
                )
                entries.append(
                    {
                        "id": flow.id,
                        "method": flow.request.method,
                        "url": flow.request.pretty_url,
                        "status": response.status_code if response else 0,
                        # get_content(strict=False) 畸形编码回 None（AGENTS §2）。
                        "size": human.pretty_size(len(content or b"")),
                    }
                )
            return {
                "enabled": runtime.mock_enabled,
                "count": len(entries),
                "entries": entries,
            }

        if runtime.is_running:
            return dict(runtime.call(build))
        return build()

    def clear_flows(self, flow_ids: Collection[str] | None = None) -> None:
        def clear() -> None:
            if flow_ids is not None:
                self._remove_flow_ids(list(flow_ids))
                return
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

    def remove_flows(self, flow_ids: Collection[str]) -> None:
        def remove() -> None:
            self._remove_flow_ids(list(flow_ids))

        if self.runtime.is_running:
            self.runtime.call(remove)
        else:
            remove()

    def _marked_flow_ids(
        self, marked: bool, flow_ids: Collection[str] | None
    ) -> list[str]:
        """按「是否带标记」过滤 store 的 flow id。**必须在 mitm 线程上调用**
        （桥接红线：Qt 线程不碰 view），是 count / remove 两组方法的共享口径。"""
        return [
            f.id
            for f in self.view._store.values()
            if bool(f.marked) == marked and (flow_ids is None or f.id in flow_ids)
        ]

    def unmarked_flow_count(self, flow_ids: Collection[str] | None = None) -> int:
        """store 里未标记流量的条数。「删除未标记」确认框的计数：与
        `remove_unmarked_flows` 同一套过滤口径（不看类型、只看 marked），
        数字对不上就会删多。"""

        def count() -> int:
            return len(self._marked_flow_ids(False, flow_ids))

        if self.runtime.is_running:
            return int(self.runtime.call(count))
        return count()

    def remove_unmarked_flows(self, flow_ids: Collection[str] | None = None) -> int:
        """删除 store 里所有未标记流量（对齐原生 `view.clear_unmarked`，
        addons/view.py:369），返回删除数供界面播报。

        不能照抄原生直接删 store：挂起中的 flow 必须先放行，否则连接永久挂在
        wait_for_resume 上（同 `remove_flows` 注释），故复用同一条生命周期。
        枚举发生在 mitm 线程内（桥接红线：Qt 线程不碰 view），所以闭包现算。
        """

        def remove() -> int:
            doomed = self._marked_flow_ids(False, flow_ids)
            self._remove_flow_ids(doomed)
            return len(doomed)

        if self.runtime.is_running:
            return int(self.runtime.call(remove))
        return remove()

    def marked_flow_count(self, flow_ids: Collection[str] | None = None) -> int:
        """store 里已标记流量的条数。「清空已标记」确认框的计数，与
        `remove_marked_flows` 同一套过滤口径，数字对不上就会删多。"""

        def count() -> int:
            return len(self._marked_flow_ids(True, flow_ids))

        if self.runtime.is_running:
            return int(self.runtime.call(count))
        return count()

    def remove_marked_flows(self, flow_ids: Collection[str] | None = None) -> int:
        """删除 store 里所有已标记流量，返回删除数供界面播报。

        生命周期与 `remove_unmarked_flows` 完全一致：挂起中的 flow 必须先放行，
        否则连接永久挂在 wait_for_resume 上（同 `remove_flows` 注释）。
        """

        def remove() -> int:
            doomed = self._marked_flow_ids(True, flow_ids)
            self._remove_flow_ids(doomed)
            return len(doomed)

        if self.runtime.is_running:
            return int(self.runtime.call(remove))
        return remove()

    def _remove_flow_ids(self, flow_ids: list[str]) -> None:
        """放行 → sweep → sse.forget → view.remove（必须在 mitm 线程跑）。"""
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
