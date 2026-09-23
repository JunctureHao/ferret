"""Application-scoped mitmproxy runtime and event-loop bridge."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import socket
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QCoreApplication, QObject, QThread, Signal

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    HTTPFlow,
    LocalRedirectorInstance,
    Options,
    OptionsError,
    View,
    net_tls,
    parse_filter,
)
from ferret.core.mitm.certificate import (
    CertificateError,
    build_trusted_ca_bundle,
    client_certs_error,
)
from ferret.core.mitm.cut import DEFAULT_BODY_CUT_SIZE, clamp_body_cut_size
from ferret.core.mitm.gateway import (
    GatewayRule,
    GatewayRuleSet,
    gateway_option_updates,
)
from ferret.core.mitm.intercept import InterceptRule, intercept_option_updates
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.modes import (
    capture_mode_specs,
    ensure_wireguard_conf,
    validate_mode_specs,
)
from ferret.core.mitm.rewrite import RewriteRule, RewriteRuleSet
from ferret.core.mitm.scripts import ScriptEntry
from ferret.core.mitm.wsframe import latest_frame, ws_close
from ferret.core.network import ANY_HOST, LOOPBACK_HOST, normalize_listen_host
from ferret.core.settings import get_certs_dir

log = get_logger("mitmproxy")

# 固定会话（plans/sticky-session.md）：原生 StickyCookie / StickyAuth 两个 addon
# 的选项名。过滤串恒为「全量」、刻意不暴露匹配条件 —— 一旦可配就退化成规则表，
# 而它是「浏览器行为偏好」式的全局开关。开关状态不在这里：它只是 bool，直接
# 存 MitmRuntime.sticky_session_enabled（配置落盘在 core/settings.py）。
STICKY_SESSION_OPTIONS: tuple[str, ...] = ("stickycookie", "stickyauth")
STICKY_SESSION_FILTER = "~http"


def sticky_session_option_updates(enabled: bool) -> dict[str, str | None]:
    """Translate the switch into the ``options.update`` kwargs for both addons.

    恒写全量：关掉也要把两个选项写回 ``None`` —— 原生 configure 见到空值会清掉
    自家过滤器，留着旧串内核就会继续补头。
    """
    value = STICKY_SESSION_FILTER if enabled else None
    return {"stickycookie": value, "stickyauth": value}


# 无缓存·明文：原生 AntiCache / AntiComp 两个 addon 的布尔 option（默认 False）。
# 与固定会话同一个「浏览器行为偏好」定位：全局一个合成开关，两个 option 同开同关，
# 不拆成两个独立开关。开关状态存 MitmRuntime.anticache_plaintext（落盘在
# core/settings.py）。
ANTICACHE_OPTIONS: tuple[str, ...] = ("anticache", "anticomp")


def anticache_option_updates(enabled: bool) -> dict[str, bool]:
    """Translate the switch into the ``options.update`` kwargs for both addons.

    bool 选项与 sticky 的过滤串不同：关掉写 ``False``（出厂默认）而不是 ``None``。
    """
    return {"anticache": enabled, "anticomp": enabled}


# DNS 解析（.plans/dns-options.md）：原生 DnsResolver addon 的两个选项。仅对隧道内
# DNS 生效（WireGuard 通道的 10.0.0.53）；不产生 DNSFlow 就没有代码路径碰到它，
# 全局偏好、不随通道回滚（不进 _CHANNEL_INTENTS）。
def dns_option_updates(
    name_servers: list[str], use_hosts_file: bool
) -> dict[str, list[str] | bool]:
    """Translate the DNS config into the ``options.update`` kwargs."""
    return {
        "dns_name_servers": list(name_servers),
        "dns_use_hosts_file": use_hosts_file,
    }


def _validate_dns_name_servers(items: list[str]) -> list[str]:
    """Reject non-IP entries, drop blank ones; returns a new stripped list.

    原生 ``dns_name_servers`` 注册时无校验器，``_Option.set`` 只查类型不查值
    （optmanager.py），坏 IP 串过 ``options.update`` 静默放行 —— 这里是唯一闸门，
    界面提交与内核种子两处共用。错误文案会上面（对话框内联展示），按 AGENTS.md
    用 ``"runtime"`` context 包整句，变量交给 ``.format()``。
    """
    checked: list[str] = []
    for item in items:
        text = item.strip()
        if not text:
            continue
        try:
            ipaddress.ip_address(text)
        except ValueError:
            raise ValueError(
                QCoreApplication.translate("runtime", "“{}”不是合法的 IP 地址").format(
                    text
                )
            ) from None
        checked.append(text)
    return checked


# 上游 TLS 信任（.plans/upstream-tls.md）：原生 tlsconfig 的三个选项。与 dns_* 相反，
# 它们都写在 `Options.__init__` 里、构造期就存在（tests/core/mitm/test_upstream_tls.py
# 钉着这条差异），但仍走 `_apply_*` 播种 —— 合并产物要现算，且必须支持运行中热更。
def ssl_option_updates(
    insecure: bool,
    trusted_bundle: str | None,
    add_upstream_certs: bool,
) -> dict[str, bool | str | None]:
    """Translate the upstream-TLS config into the ``options.update`` kwargs.

    两处非写不可的细节：

    * 没有合并产物时必须回 ``None`` 而**不是** ``""``。空串会让
      ``load_verify_locations("", None)`` 抛 ``SSL.Error``，被 `net/tls.py` 重包成
      ``RuntimeError`` 砸在握手路径上 —— 那不是 ``OptionsError``，`options.update`
      外面那层 try/except 拦不住，只能在这里不让它产生。
    * ``add_upstream_certs`` 为真时一并显式带上 ``upstream_cert=True``。原生
      `addons/core.py::Core.configure` 会在 upstream_cert 关着时抛 ``OptionsError``；
      它出厂就是 True 且 ferret 从不暴露，这行是防上游哪天翻默认值。
    """
    updates: dict[str, bool | str | None] = {
        "ssl_insecure": insecure,
        "ssl_verify_upstream_trusted_ca": trusted_bundle or None,
        "add_upstream_certs_to_client_chain": add_upstream_certs,
    }
    if add_upstream_certs:
        updates["upstream_cert"] = True
    return updates


def client_certs_option_updates(path: str) -> dict[str, str | None]:
    """Translate the mTLS client-certificate path into ``options.update`` kwargs.

    两处非写不可的细节：

    * 空串必须归一成 ``None``（= 原生出厂值）。原生 `addons/core.py` 与
      `tlsconfig.py` 两处都是 truthy 判断，``""`` 恰好也无害 —— 但那是巧合，
      不把功能的「关」态押在别人的实现细节上。
    * 发**用户给的原样路径**，不发展开 ``~`` 之后的。原生那两处自己 expanduser，
      我们展开了反而让 CONFIG 与 options 存着两个不同的字符串，界面回读对不上。
    """
    return {"client_certs": path.strip() or None}


def clear_proxy_server_context_cache() -> None:
    """Drop mitmproxy's cached upstream TLS contexts.

    `create_proxy_server_context` 是模块级 ``@lru_cache(256)``，键里只有
    ``client_cert`` 的**路径字符串**：证书续期时用户在原路径上换掉文件内容，路径
    没变 → 命中旧 context → 继续出示那张过期证书。而且缓存挂在模块上，跨
    `MitmRuntime.restart` 存活，「停止抓包 → 换证书 → 重新开始」也清不掉。

    所以下发 client_certs 的两条路径（热更、种子）都**无条件**调它。代价很小：
    冷建一次 context 实测 5~12 ms，命中缓存 4 µs 级，而重建只发生在下一次握手。
    """
    net_tls.create_proxy_server_context.cache_clear()


# 抓包通道的**意图值**字段名（见 `MitmRuntime.__init__` 的逐条注释）：落盘偏好，
# 与不落盘的接通位 `channels_engaged` 分开。`apply_channels` 的三条回滚路径共用
# 这张表打包/恢复，新增一个意图值只改这里。
# 别把 proxyauth_* 三项「统一」进来：upstream_username/password 在表内，是因为它们
# 由 apply_channels 写入、失败要连同 mode 一起回滚；proxyauth 走自己的
# apply_proxy_auth 热更（与 block_global 同类），apply_channels 只读不写它，没有
# 任何属于它的状态需要回滚。详见 _effective_proxyauth。
_CHANNEL_INTENTS: tuple[str, ...] = (
    "use_local",
    "local_spec",
    "use_wireguard",
    "use_reverse",
    "reverse_target",
    "reverse_port",
    "use_socks5",
    "socks5_port",
    "use_upstream",
    "upstream_target",
    "upstream_username",
    "upstream_password",
)


class MitmRuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class UiBridgeAddon:
    """Forward the native View signals and the websocket hooks across the boundary.

    View 的信号只是转发；websocket 那三个钩子是这里**自己**实现的 addon 钩子 ——
    `View` 一个 websocket 钩子都没有（它只有 `requestheaders` / `error` / `response` /
    `tcp_*` / `udp_*` / `update`），所以帧到达这件事没有任何原生信号可借。这与本类
    已经带着 `flow_suspended` / `flow_intercepted` 两个 ferret 自有信号是同一类问题：
    缺钩子的事件只能自己补一次 emit。
    """

    def __init__(
        self,
        view: View,
        bridge: MitmRuntime,
        master: FerretMaster,
        generation: int,
    ) -> None:
        self._view = view
        self._bridge = bridge
        self._master = master
        self._generation = generation
        self._connected = True
        self._on_add = lambda flow: bridge.flow_added.emit(flow)
        self._on_update = lambda flow: bridge.flow_updated.emit(flow)
        self._on_remove = lambda flow, index: bridge.flow_removed.emit(flow, index)
        self._on_refresh = lambda: bridge.view_refreshed.emit()
        view.sig_view_add.connect(self._on_add)
        view.sig_view_update.connect(self._on_update)
        view.sig_view_remove.connect(self._on_remove)
        view.sig_view_refresh.connect(self._on_refresh)

    def running(self) -> None:
        if not self._master.proxyserver.listen_addrs():
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "代理端口监听失败")
            )
        self._bridge._master_running.emit(self._generation)

    def websocket_start(self, flow: HTTPFlow) -> None:
        """101 握手成功、可以收发帧了。"""
        if not self._connected:
            return
        self._bridge.websocket_started.emit(flow.id)

    def websocket_message(self, flow: HTTPFlow) -> None:
        """一帧到达（约定：最新一帧在 ``flow.websocket.messages[-1]``）。

        发的是 :class:`WsFrame` 值对象而不是 flow：钩子跑在 mitm 线程上，发 flow 过去
        等于让 Qt 侧读活 flow（AGENTS.md §3 红线），而且 `WebSocketMessage.content`
        此刻还是可改的 —— 当场取值才对得上「这一帧当时是什么」。
        """
        if not self._connected:
            return
        frame = latest_frame(flow.websocket)
        if frame is not None:
            self._bridge.websocket_frame.emit(flow.id, frame)

    def websocket_end(self, flow: HTTPFlow) -> None:
        """连接关了。关闭码 / 原因 / 谁关的 / 何时关，一并作为值对象发出去。"""
        if not self._connected:
            return
        self._bridge.websocket_closed.emit(flow.id, ws_close(flow.websocket))

    def done(self) -> None:
        self.disconnect()

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self._view.sig_view_add.disconnect(self._on_add)
        self._view.sig_view_update.disconnect(self._on_update)
        self._view.sig_view_remove.disconnect(self._on_remove)
        self._view.sig_view_refresh.disconnect(self._on_refresh)


class _MitmThread(QThread):
    failed = Signal(int, str)

    def __init__(self, runtime: MitmRuntime, generation: int) -> None:
        super().__init__(runtime)
        self.runtime = runtime
        self.generation = generation
        self.loop: asyncio.AbstractEventLoop | None = None
        self.master: FerretMaster | None = None
        self.stop_requested = False

    def run(self) -> None:
        try:
            asyncio.run(self._run_master())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.generation, str(exc))
            try:
                log.error("mitmproxy runtime failed: %s", exc)
            except RuntimeError:
                pass

    async def _run_master(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._ensure_port_available()
        # 上一个循环可能没跑成 disarm（stop 超时、崩溃），把守护进程的旧 spec
        # 与单例占位清干净再起，否则 _start 的 "more than one redirector" 护栏
        # 会让 local 通道静默失效。
        await self.loop.run_in_executor(None, MitmRuntime._disarm_local_redirector)
        # WireGuard 密钥在 Options 构造前预置：内核 _start 只在文件不存在时写，
        # 先到者定密钥，后到者复用。消灭「内核正在 _start 写文件，同一瞬间用户
        # 点二维码」的窗口（facade 侧也走 open("x")，谁赢都用同一份）。
        if self.runtime.use_wireguard:
            ensure_wireguard_conf(get_certs_dir() / "wireguard.conf")
        options = Options(
            listen_host=self.runtime.listen_host,
            listen_port=self.runtime.listen_port,
            confdir=str(get_certs_dir()),
            mode=self.runtime._mode_specs(),
        )
        master = FerretMaster(options, event_loop=self.loop, view=self.runtime.view)
        master.addons.add(
            UiBridgeAddon(
                self.runtime.view,
                self.runtime,
                master,
                self.generation,
            )
        )
        self._apply_gateway_rules(master)
        self._apply_block_options(master)
        self._apply_rewrite_rules(master)
        self._apply_scripts(master)
        self._apply_intercept_rules(master)
        self._apply_sticky_session(master)
        self._apply_anticache_plaintext(master)
        self._apply_upstream_auth(master)
        self._apply_proxyauth(master)
        self._apply_dns_options(master)
        self._apply_ssl_options(master)
        self._apply_client_certs(master)
        self._apply_body_cut(master)
        # 编辑页发送结果的回报桥：与 gateway.on_suspend_changed 同一个接法，
        # 回调只做一次 Signal.emit，由 Qt 队列连接跨线程。
        master.compose.on_result = self.runtime.compose_result.emit
        # SSE tee 的信号桥同理（master 装配时 runtime 还不存在，只能在这里补）。
        master.sse.bridge = self.runtime
        self.master = master
        self.runtime._master_created.emit(self.generation, master)
        if self.stop_requested:
            master.shutdown()
        try:
            await master.run()
        finally:
            self.master = None
            self.loop = None

    def _apply_gateway_rules(self, master: FerretMaster) -> None:
        """Seed both gateway planes before serving traffic (on the mitm loop).

        挂起变更回调也在这里接：`GatewayState` 只在 mitm 线程上被读写，回调本身
        只做一次 `Signal.emit`（和 `UiBridgeAddon` 转发 View 信号是同一条路子）。
        """
        master.gateway.on_suspend_changed = self.runtime._on_flow_suspended
        try:
            self.runtime._push_gateway(master, self.runtime._gateway_payload())
        except (ValueError, OptionsError) as exc:
            log.warning("网关规则无法应用，已忽略: %s", exc)

    def _apply_block_options(self, master: FerretMaster) -> None:
        """Seed Block's source filters before serving traffic (on the mitm loop).

        `block_global` / `block_private` 由 Block.load 注册，构造 Options 时还不存在，
        所以只能等 Master 建好之后再写。
        """
        try:
            master.options.update(
                block_global=self.runtime.block_global,
                block_private=self.runtime._effective_block_private(),
            )
        except (ValueError, OptionsError) as exc:
            log.warning("来源过滤开关无法应用，已忽略: %s", exc)

    def _apply_rewrite_rules(self, master: FerretMaster) -> None:
        """Seed the rewrite snapshot before serving traffic (on the mitm loop).

        自研件不吃选项（原生四件退役后 ``rewrite_option_updates`` 一并拆除），
        快照在这里编译（编译不碰任何线程绑定资源）并注入 addon —— 编译失败记
        日志忽略：规则是整批下发的，一条坏规则不该拖死内核启动。
        """
        try:
            snapshot = RewriteRuleSet(self.runtime.rewrite_rules)
        except ValueError as exc:
            log.warning("重写规则无法应用，已忽略: %s", exc)
            return
        master.rewrite.set_rules(snapshot, enabled=self.runtime.rewrite_enabled)

    def _apply_scripts(self, master: FerretMaster) -> None:
        """Seed the script batch before serving traffic (on the mitm loop).

        状态回调也在这里接：FerretScriptAddon 只在 mitm 线程上被读写，回调本身
        只做一次 `Signal.emit`（与 `GatewayState.on_suspend_changed` 同一条路子）。
        set_scripts 绝不抛 —— 坏脚本只落成 ERROR 状态，不能拖死内核启动
        （与 `_apply_rewrite_rules` 的播种姿态一致）。
        """
        master.scripts.on_status = self.runtime.script_status_changed.emit
        master.scripts.set_scripts(self.runtime.scripts)

    def _apply_intercept_rules(self, master: FerretMaster) -> None:
        """Seed the breakpoint option before serving traffic (on the mitm loop).

        拦截变更回调也在这里接，理由与 `_apply_gateway_rules` 里的挂起回调逐字相同：
        `InterceptState` 只在 mitm 线程上被读写，回调本身只做一次 `Signal.emit`。
        ``intercept`` 由 Intercept.load 注册，构造 Options 时还不存在，同样只能等
        Master 建好之后再写。
        """
        master.intercept_state.on_intercept_changed = self.runtime._on_flow_intercepted
        try:
            master.options.update(
                **intercept_option_updates(
                    self.runtime.intercept_rules,
                    enabled=self.runtime.intercept_enabled,
                )
            )
        except (ValueError, OptionsError) as exc:
            log.warning("断点规则无法应用，已忽略: %s", exc)

    def _apply_sticky_session(self, master: FerretMaster) -> None:
        """Seed the sticky-session options before serving traffic (on the mitm loop).

        ``stickycookie`` / ``stickyauth`` 由两个原生 addon 的 ``load`` 注册，构造
        Options 时还不存在，只能等 Master 建好之后再写 —— 和 `_apply_rewrite_rules`
        完全同一个约束。
        """
        try:
            master.options.update(
                **sticky_session_option_updates(self.runtime.sticky_session_enabled)
            )
        except (ValueError, OptionsError) as exc:
            log.warning("固定会话开关无法应用，已忽略: %s", exc)

    def _apply_anticache_plaintext(self, master: FerretMaster) -> None:
        """Seed the anticache/anticomp options before serving traffic (on the mitm loop).

        ``anticache`` / ``anticomp`` 由两个原生 addon 的 ``load`` 注册，构造
        Options 时还不存在，只能等 Master 建好之后再写 —— 与 `_apply_sticky_session`
        完全同一个约束。
        """
        try:
            master.options.update(
                **anticache_option_updates(self.runtime.anticache_plaintext)
            )
        except (ValueError, OptionsError) as exc:
            log.warning("无缓存·明文开关无法应用，已忽略: %s", exc)

    def _apply_upstream_auth(self, master: FerretMaster) -> None:
        """Seed the upstream proxy credential before serving traffic (on the mitm loop).

        ``upstream_auth`` 由原生 ``UpstreamAuth.load`` 注册，构造 Options 时还不
        存在，只能等 Master 建好之后再写 —— 与 `_apply_sticky_session` 完全同一个
        约束。mode 里的 upstream 槽位走 Options 构造参数（那是出厂就有的选项），
        凭证走这里，两边合起来才是一条完整的上游出口。
        """
        try:
            master.options.update(upstream_auth=self.runtime._upstream_auth())
        except (ValueError, OptionsError) as exc:
            log.warning("上游代理凭证无法应用，已忽略: %s", exc)

    def _apply_dns_options(self, master: FerretMaster) -> None:
        """Seed the DNS resolver options before serving traffic (on the mitm loop).

        ``dns_*`` 由 ``DnsResolver.load`` 注册，构造 Options 时还不存在，只能等
        Master 建好再写 —— 与 sticky / anticache / upstream_auth 同款约束。关键
        差异在坏值守卫必须在这里自建：该选项注册时无校验器，原生 ``_Option.set``
        只查类型不查值，坏 IP 串过 ``options.update`` 静默放行不抛 ``OptionsError``
        （历史落盘坏值的唯一拦截点）。校验失败回退 ``[]``（= 系统 DNS），不炸启动。
        """
        try:
            master.options.update(
                **dns_option_updates(
                    _validate_dns_name_servers(self.runtime.dns_name_servers),
                    self.runtime.dns_use_hosts_file,
                )
            )
        except ValueError as exc:
            # 提示性日志失败不值得连坐启动：全量测试里可能有残留的 mitmproxy
            # 日志 handler 指着已关闭的事件循环（同 core/runtime.py 的 local_spec
            # 兜底与 _MitmThread.run 的姿态）；回退语义不依赖日志是否送达。
            try:
                log.warning("DNS 服务器配置无法应用，已回退系统 DNS: %s", exc)
            except RuntimeError:
                pass
        except OptionsError as exc:
            # 对该选项近乎死代码（无校验器），防上游未来加校验器时分叉。
            try:
                log.warning("DNS 选项无法应用，已忽略: %s", exc)
            except RuntimeError:
                pass

    def _apply_ssl_options(self, master: FerretMaster) -> None:
        """Seed the upstream-TLS trust options before serving traffic (mitm loop).

        与 `_apply_dns_options` 的取舍完全一致：坏值不炸启动。用户那几个信任文件
        随时可能被删 / 挪走 / 换成截图，`build_trusted_ca_bundle` 把解不动的挑出来
        当返回值而不是抛异常，这里只记 warning —— 全坏时它回 ``None``，落到
        `ssl_option_updates` 就是不下发 ca_pemfile，即原生公共根行为。

        写盘失败（证书目录只读之类）抛 ``CertificateError``，同样只降级不连坐：
        没有产物就按公共根跑，用户在界面上会看到卡片仍显示旧状态而抓不到自签站点，
        日志里有这一条可查。
        """
        runtime = self.runtime
        bundle: str | None = None
        try:
            bundle, bad = build_trusted_ca_bundle(runtime.ssl_trusted_ca_files)
        except CertificateError as exc:
            bad = list(runtime.ssl_trusted_ca_files)
            self._log_warning("上游信任库无法生成，已回退公共根证书: %s", exc)
        if bad:
            self._log_warning(
                "上游信任库中 %d 个文件无法解析，已跳过: %s", len(bad), ", ".join(bad)
            )
        try:
            master.options.update(
                **ssl_option_updates(
                    runtime.ssl_insecure,
                    bundle,
                    runtime.add_upstream_certs_to_client_chain,
                )
            )
        except (ValueError, OptionsError) as exc:
            # OptionsError 这支不是死代码：拼接链在 upstream_cert 关着时会被原生
            # Core.configure 拒掉（ssl_option_updates 已显式带上 True 防着它）。
            self._log_warning("上游 TLS 选项无法应用，已忽略: %s", exc)

    def _apply_client_certs(self, master: FerretMaster) -> None:
        """Seed the mTLS client-cert path before serving traffic (on the mitm loop).

        坏值只记 warning 并**整项跳过**（= 保持原生 None），不炸启动：用户配的那个
        路径随时可能被删 / 挪走 / 换成加密私钥，而「今天没法出示客户端证书」远不如
        「应用起不来」严重 —— 与 `_apply_dns_options` / `_apply_ssl_options` 同一取舍。
        界面 showEvent 会重新盘点并把卡片打成失效态，用户在那儿看得见。

        末尾**无条件**清缓存：`clear_proxy_server_context_cache` 的 docstring 写了
        为什么内核重启不足以让它失效。
        """
        runtime = self.runtime
        path = runtime.client_certs_path
        reason = client_certs_error(path)
        if reason:
            self._log_warning("客户端证书无法应用，已跳过: %s", reason)
        else:
            try:
                master.options.update(**client_certs_option_updates(path))
            except (ValueError, OptionsError) as exc:
                self._log_warning("客户端证书选项无法应用，已忽略: %s", exc)
        clear_proxy_server_context_cache()

    def _apply_body_cut(self, master: FerretMaster) -> None:
        """Seed the body-cut snapshot before serving traffic (on the mitm loop).

        阈值不落原生 options（12.x 已无对应选项，见 cut.py 模块注释），所以这里
        没有 options.update、也不会抛 —— 直接换 addon 的内存快照，与
        `_apply_rewrite_rules` 的播种姿态一致。
        """
        master.cut.set_options(
            enabled=self.runtime.body_cut_enabled,
            max_size=self.runtime.body_cut_size,
        )

    @staticmethod
    def _log_warning(message: str, *args: object) -> None:
        """提示性日志失败不值得连坐启动（同 `_apply_dns_options` 的兜底姿态）。

        全量测试里可能有残留的 mitmproxy 日志 handler 指着已关闭的事件循环，
        而上面几处的降级语义都不依赖日志是否送达。
        """
        try:
            log.warning(message, *args)
        except RuntimeError:
            pass

    def _apply_proxyauth(self, master: FerretMaster) -> None:
        """Seed the inbound proxy credential before serving traffic (on the mitm loop).

        ``proxyauth`` 由原生 ``ProxyAuth.load`` 注册，构造 Options 时还不存在，
        只能等 Master 建好之后再写 —— 与 `_apply_sticky_session` 完全同一个约束。

        **刻意不等 `channels_engaged`**：ferret 的内核常驻监听，「开始抓包」只接
        通道 + 挂系统代理 + 开写入闸门，监听口在那之前就已经对局域网开着。防蹭
        保护的正是这个监听口，所以开机就得种下去 —— 与 block_global 常驻同理。
        """
        try:
            master.options.update(proxyauth=self.runtime._effective_proxyauth())
        except (ValueError, OptionsError) as exc:
            log.warning("代理认证凭证无法应用，已忽略: %s", exc)

    def _ensure_port_available(self) -> None:
        if self.runtime.listen_port == 0:
            return
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((self.runtime.listen_host, self.runtime.listen_port))
        except OSError as exc:
            # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "端口 {} 已被占用").format(
                    self.runtime.listen_port
                )
            ) from exc

    def request_shutdown(self) -> None:
        self.stop_requested = True
        loop = self.loop
        master = self.master
        if loop is None or master is None:
            return
        try:
            loop.call_soon_threadsafe(master.shutdown)
        except RuntimeError:
            pass


class MitmRuntime(QObject):
    """Own one Master for the whole application lifetime."""

    state_changed = Signal(object)
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    flow_suspended = Signal(object)
    flow_intercepted = Signal(object)
    # flow_id + 值对象。用 str 而不是整条 flow：见 `UiBridgeAddon.websocket_message`。
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)
    # SSE 三件事，与 WS 同形：flow_id + SseEvent 值对象（core/mitm/sse.py）。
    sse_started = Signal(str)
    sse_event = Signal(str, object)
    sse_ended = Signal(str)
    # 编辑页手工发送的落地结果：ComposeResult 值对象（见 core/mitm/compose.py）。
    compose_result = Signal(object)
    # 脚本装载状态变更：path + ScriptStatus 值对象（core/mitm/scripts.py），
    # 由 FerretScriptAddon.on_status 回调经本信号跨线程送达（与 compose_result 同形）。
    script_status_changed = Signal(str, object)

    _master_created = Signal(int, object)
    _master_running = Signal(int)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        listen_host: str = LOOPBACK_HOST,
        listen_port: int = 8080,
        block_global: bool = True,
        block_private: bool = False,
        use_local: bool = False,
        local_spec: str = "",
        use_wireguard: bool = False,
        use_reverse: bool = False,
        reverse_target: str = "",
        reverse_port: int = 8081,
        use_socks5: bool = False,
        socks5_port: int = 1080,
        use_upstream: bool = False,
        upstream_target: str = "",
        upstream_username: str = "",
        upstream_password: str = "",
        proxyauth_enabled: bool = False,
        proxyauth_username: str = "",
        proxyauth_password: str = "",
        sticky_session_enabled: bool = False,
        anticache_plaintext: bool = False,
        dns_name_servers: list[str] | None = None,
        dns_use_hosts_file: bool = True,
        ssl_insecure: bool = False,
        ssl_trusted_ca_files: list[str] | None = None,
        add_upstream_certs_to_client_chain: bool = False,
        client_certs_path: str = "",
        body_cut_enabled: bool = False,
        body_cut_size: int = DEFAULT_BODY_CUT_SIZE,
    ) -> None:
        super().__init__(parent)
        self.listen_host = normalize_listen_host(listen_host)
        self.listen_port = listen_port
        # 原生 Block addon 的两个来源过滤开关（mitmproxy/addons/block.py）。
        # 默认沿用 mitmproxy 出厂姿态：拒公网、放局域网；环回永远放行且不可配。
        self.block_global = block_global
        self.block_private = block_private
        # 抓包通道（见 core/mitm/modes.py）：local = 本地重定向、wireguard = VPN
        # 隧道、reverse = 反向代理（把 ferret 架在目标服务前面）。类默认**全关** ——
        # 直接构造 MitmRuntime 的场景（测试、兜底组合根）不该一启动就弹 UAC 或
        # 拉起陌生监听口；真实应用的默认全开由 CONFIG 种子决定（见
        # core/runtime.py::_build_mitm_runtime）。
        self.use_local = use_local
        self.local_spec = local_spec
        self.use_wireguard = use_wireguard
        # reverse 三意图值（plans/reverse-mode.md §2/§3）：目标与端口落盘，激活
        # 与否跟随 `use_reverse` + `channels_engaged` 两个开关。reverse 与
        # regular 共用 self.listen_host（spec 里的 @ 地址），spec 端口由调用方
        # 保证错开（对话框前置校验 + 内核查重兜底）。
        self.use_reverse = use_reverse
        self.reverse_target = reverse_target.strip()
        self.reverse_port = reverse_port
        # SOCKS5 入站两意图值（.plans/0-socks5-channel.md）：独立端口的 SOCKS5 代理，
        # 给只认 SOCKS5 的客户端接入。监听地址跟随全局 listen_host（D2），spec 必带
        # ``@``（与 reverse 同一动机：主动要独立端口，不带会回退全局 listen_port 与
        # regular 撞车被内核查重拒）。
        self.use_socks5 = use_socks5
        self.socks5_port = socks5_port
        # 上游代理四意图值：它**不是第五条通道**，而是把 mode 列表第一个槽位从
        # regular 换成 upstream（见 core/mitm/modes.py::upstream_mode_spec）——
        # 监听地址端口一字不动，只把系统代理这条通道的出口改成「先交给上游代理」。
        # 目标进 spec，凭证**不能**进（原生 server_spec 的 host 段是 `[^:/]+`），
        # 另走 `upstream_auth` 选项，见 `_upstream_auth` / `_apply_upstream_auth`。
        # 用户名密码拆两项存：原生格式 "user:pass" 按首个冒号切，密码含冒号没问题，
        # 用户名含冒号则无法表达，所以由 UI 分开收而不是让用户自己拼。
        self.use_upstream = use_upstream
        self.upstream_target = upstream_target.strip()
        self.upstream_username = upstream_username
        self.upstream_password = upstream_password
        # 代理认证三意图值（.plans/proxyauth.md）：方向与 upstream_auth 正好相反
        # —— upstream_auth 管「ferret → 上游代理」的出站凭证，这里管「客户端 →
        # ferret」的入站挑战。两者互不相干，可以叠着用（手机认证到 ferret，
        # ferret 再认证到企业代理）。
        # 拆两项存的理由同 upstream_*，但约束更严：原生 SingleUser 与客户端侧
        # parse_http_basic_auth 都按 split(":") 要求恰好两段，**用户名和密码都
        # 不能含冒号**（upstream 那边只有用户名不能）。
        # 不进 _CHANNEL_INTENTS，理由见那张表的注释。
        self.proxyauth_enabled = proxyauth_enabled
        self.proxyauth_username = proxyauth_username
        self.proxyauth_password = proxyauth_password
        # 「抓包会话」是否接通：False 时 _mode_specs 只回 regular（应用启动态），
        # True 才把启用的通道拼进 mode 列表。与上面三个**意图值**分开 —— 停止
        # 会话只动这一位，用户的通道偏好原样保留，下次点开始照旧拼装。
        self.channels_engaged = False
        self.view = View()
        self.view.set_filter(parse_filter("~http"))
        self._state = MitmRuntimeState.STOPPED
        self._thread: _MitmThread | None = None
        self._master: FerretMaster | None = None
        self._last_error = ""
        self._generation = 0
        self.gateway_rules: list[GatewayRule] = []
        self.gateway_enabled = True
        self.rewrite_rules: list[RewriteRule] = []
        # 重写总开关：关掉后自研件对所有流量一律不判（网关规则模式，见
        # apply_rewrite_rules）。刻意**不落盘**（plans/rewrite-ui.md §8：
        # settings.py 零改动）—— 每次启动都是开，「临时下发空规则」的语义由
        # 这个内存位承担，不碰各行规则的 enabled 落盘值。
        self.rewrite_enabled = True
        # 用户脚本清单（plans/scripts.md）：由控制器层在启动时从 CONFIG 播种，
        # 构造器不读 CONFIG（与 rewrite/gateway 规则同一姿态）。
        self.scripts: list[ScriptEntry] = []
        self.intercept_rules: list[InterceptRule] = []
        # 断点默认**关**：拦截会把客户端连接一直钉住等人处理，一启动就生效等于用户
        # 还没打开界面、流量就先卡住了。开关由界面显式打开（见 core/settings.py 的
        # intercept_enabled）。
        self.intercept_enabled = False
        # 固定会话默认**关**：开启会改写实时抓取所见的请求头（代理侧补
        # Cookie / Authorization），与「抓包应如实转发原件」冲突。类默认关、真实
        # 应用由 CONFIG 种子决定（core/runtime.py::_build_mitm_runtime），开关
        # 在设置页（apps/settings）。
        self.sticky_session_enabled = sticky_session_enabled
        # 无缓存·明文默认**关**：开启会改写请求头（删条件缓存头、改
        # Accept-Encoding=identity），抓到的就不是客户端原件。种子与开关位置同
        # 固定会话（core/runtime.py::_build_mitm_runtime、apps/preferences）。
        self.anticache_plaintext = anticache_plaintext
        # DNS 解析两意图值（.plans/dns-options.md）：全局偏好、不随通道回滚，
        # 故不进 _CHANNEL_INTENTS。类默认对齐原生出厂（[] = 系统 DNS、
        # True = 查 hosts），真实值由 CONFIG 种子决定（core/runtime.py）。
        self.dns_name_servers = list(dns_name_servers or [])
        self.dns_use_hosts_file = dns_use_hosts_file
        # 上游 TLS 信任三意图值（.plans/upstream-tls.md）：四条通道共用同一条
        # `tls_start_server`（QUIC 路另有分支但读同两个值），语义天然一致 ——
        # 不按通道分别下发，也不存在 block_private 那种让路需求，故不进
        # _CHANNEL_INTENTS。类默认一律取原生出厂（False / [] / False），真实值
        # 由 CONFIG 种子决定（core/runtime.py）。
        # 存的是**用户给的文件路径**，合并产物（公共根 + 用户根）每次现算、永不
        # 落盘，见 core/mitm/certificate.py::build_trusted_ca_bundle。
        self.ssl_insecure = ssl_insecure
        self.ssl_trusted_ca_files = list(ssl_trusted_ca_files or [])
        self.add_upstream_certs_to_client_chain = add_upstream_certs_to_client_chain
        # mTLS 客户端证书（.plans/mtls-client-certs.md）：与上面三项同属「四条通道
        # 共用一条 tls_start_server」的全局偏好，同样不进 _CHANNEL_INTENTS。存**用户
        # 给的原样路径**（可以带 ~）：原生 addons/core.py 与 tlsconfig.py 两处都自己
        # expanduser，我们展开了反而让这份内存副本与 options 对不上。
        self.client_certs_path = client_certs_path
        # 大正文边收边截两意图值（.plans/1-cut-flow-size.md）：全局偏好、不随通道
        # 回滚，故不进 _CHANNEL_INTENTS。默认**关**：截断改变存储语义（`~b` 只搜
        # 前缀、导出缺完整正文），不该在用户没开之前替他决定。阈值是 addon 的内
        # 存快照（不落原生 options，12.x 已无对应选项），下发走网关规则模式。
        self.body_cut_enabled = body_cut_enabled
        self.body_cut_size = clamp_body_cut_size(body_cut_size)

        self._master_created.connect(self._on_master_created)
        self._master_running.connect(self._on_master_running)

    @property
    def state(self) -> MitmRuntimeState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state == MitmRuntimeState.RUNNING and self._master is not None

    @property
    def master(self) -> FerretMaster | None:
        return self._master

    @property
    def last_error(self) -> str:
        return self._last_error

    def _mode_specs(self) -> list[str]:
        """完整 ``mode`` 选项：首槽位恒在（常驻底盘），通道按「启用 × 已接通」拼接。

        首槽位默认 regular，上游代理启用且已接通时整条换成 upstream —— 上游与四条
        通道同过 `channels_engaged` 这道闸门，未接通时（应用启动态）回 regular。
        """
        engaged = self.channels_engaged
        return capture_mode_specs(
            use_local=engaged and self.use_local,
            local_spec=self.local_spec,
            use_wireguard=engaged and self.use_wireguard,
            use_reverse=engaged and self.use_reverse,
            reverse_target=self.reverse_target,
            reverse_port=self.reverse_port,
            use_socks5=engaged and self.use_socks5,
            socks5_port=self.socks5_port,
            use_upstream=engaged and self.use_upstream,
            upstream_target=self.upstream_target,
            listen_host=self.listen_host,
        )

    def set_channels_engaged(self, engaged: bool) -> None:
        """Open or close the capture session on the kernel.

        True 把启用的通道热更进 mode 列表（local 的提权守护进程由上游常驻复用，
        重开不弹 UAC）；False 回到 regular-only，OS 级截流全部解除 —— 内核继续
        空转，compose / 详情页不受影响。只动接通位，不碰 use_local/use_wireguard
        这些意图值。

        Raises:
            ValueError: 内核拒绝（坏 spec / 重复监听地址等），内存副本已回滚。
        """
        previous = self.channels_engaged
        self.channels_engaged = engaged
        specs = self._mode_specs()
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                # proxyauth 必须与 mode 同一次 update 推下去，理由与
                # apply_channels 里那条注释同源：分两次推会漏出「local 已接通、
                # 认证还在挑战」的半拉子中间态，那一瞬里被截流的应用全吃 401。
                lambda: master.options.update(
                    mode=specs,
                    block_global=self.block_global,
                    block_private=self._effective_block_private(),
                    proxyauth=self._effective_proxyauth(),
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.channels_engaged = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            self.channels_engaged = previous
            raise

    def _effective_block_private(self) -> bool:
        """下发 Block 选项时实际采用的 ``block_private`` 值。

        WireGuard 客户端全部来自 10.0.0.1/32（上游 ``WireGuardServerInstance`` 固定
        分配的隧道网段），reverse 绑非环回时局域网客户端会被原生 ``Block`` 当「局
        域网来源」全杀 —— 这两条都必须在接通期内让路；其它情况按用户配置原值下发。
        原生 Block 对 LocalMode 连接已有豁免（block.py:35），环回也恒放行，所以
        这里不需要为 local 另写条件。

        判据与 ``apps/capture/views.py::_sync_exposure`` 的 UI 让路条件同式（计划
        §4/§6）——「绑定非环回」在 ferret 语境恒等于 ``listen_host == ANY_HOST``
        （``LISTEN_HOSTS`` 只两个合法值，``OptionsValidator.correct`` 把野值纠回
        环回）。engaged 闸门保证未接通时（应用启动态）不发生让路、保留配置原值。
        用户配置的原值保留在 ``self.block_private``，通道撤下后自动恢复。
        """
        reverse_yield = self.use_reverse and self.listen_host == ANY_HOST
        # socks5 绑 ANY_HOST 时局域网来源同被原生 Block 误杀，与 reverse 同式让路
        # （.plans/0-socks5-channel.md §2.3）。
        socks5_yield = self.use_socks5 and self.listen_host == ANY_HOST
        return self.block_private and not (
            self.channels_engaged
            and (self.use_wireguard or reverse_yield or socks5_yield)
        )

    def _upstream_auth(self) -> str | None:
        """下发给原生 ``upstream_auth`` 选项的值；``None`` = 不发认证头。

        上游**没有生效**或用户名为空都回 ``None``，三条都是必须的：

        - **不能回空串**。``parse_upstream_auth`` 的正则是 ``.+:``，实测
          ``upstream_auth=""`` 抛 ``OptionsError: Invalid upstream auth
          specification:``，会把整个 ``options.update`` 连 mode 一起打回。
        - **上游关闭时必须回 None**，哪怕凭证还留在配置里。``UpstreamAuth``
          的模式闸门放行 upstream **和 reverse**（``upstream_auth.py:40-58``），
          选项非空时 reverse 通道的请求会被补上 ``Authorization`` 头 —— 即把企业
          代理的密码发给 reverse 目标。原生分不开这两者，唯一的开关就在这里。
          两者同时开启的残余风险由对话框警告兜（测试钉住了这个语义）。
        - **判据必须与首槽位是否真的换成了 upstream 一致**，所以空目标同样回
          None：``capture_mode_specs`` 见到空目标会保持 ``regular``（空 spec 推进
          内核只会让系统代理整条死掉），此时上游并未生效，凭证却还发得出去 ——
          配上 reverse 就是把企业代理密码白送给反代目标。对话框拦得住「勾了没填」，
          但手改配置文件、或直接调 ``apply_channels(use_upstream=True)`` 不带目标
          都绕得过去，闸门得设在这里。
        - **不看 ``channels_engaged``**：它与 mode 不同步是有意的。开机
          `_apply_upstream_auth` 在未接通时就把凭证种好，`set_channels_engaged`
          才只推 mode 而不必重推凭证；未接通时 mode 里既没有 upstream 也没有
          reverse，没有任何一条钩子能拿这份凭证做事。
        """
        if not self.use_upstream or not self.upstream_target.strip():
            return None
        if not self.upstream_username:
            return None
        return f"{self.upstream_username}:{self.upstream_password}"

    def _effective_proxyauth(self) -> str | None:
        """下发给原生 ``proxyauth`` 选项的值；``None`` = 不挑战，任何人可用。

        与 `_effective_block_private` 同一个「让路」姿态：用户配置的意图值原样留在
        ``self.proxyauth_*``，只有下发值受通道状态影响，通道撤下后自动恢复。

        四道闸门，逐条都是必须的：

        - **没启用 / 用户名为空回 None**。原生没有「关闭」spec，关就是置 ``None``
          （``addons/proxyauth.py:48-63``）；空密码合法（``"alice:"`` 实测可用），
          空用户名不是 —— ``":"`` 虽能过解析，但等于谁都能用空用户名进来。
        - **任一侧含冒号回 None**。``SingleUser`` 用 ``split(":")`` 要求恰好两段
          （``:192-197``），三段直接 ``OptionsError``，会把整个 ``options.update``
          连 mode 一起打回。对话框已前置拒绝，这里是手改配置文件的兜底 —— 镜像
          ``_upstream_auth`` 把闸门设在 runtime 的论证。这条路会**静默**关掉认证，
          用户以为开着其实没开，所以必须留一行日志。
        - **local / wireguard / reverse 接通期间让路**。这三条通道与 proxyauth 互斥：
          ``is_http_proxy`` 只认 RegularMode / UpstreamMode（``:143-151``），其余模式
          不是「跳过」而是按 HTTP 服务器语义回 401 —— 被截流的应用、隧道内流量、
          反代客户端全部打挂；reverse 还额外串台（客户端发给源站的真实
          ``Authorization`` 头会被当代理凭证消费掉）。原生 ``configure`` 没有任何
          mode 检查（``:48-63``），不会替我们拦，闸门只能设在这里。代价是让路期间
          regular 的保护也一并撤下（原生选项全局单值，分不开）—— UI 侧置灰并给出
          提示，让用户知情。
        - **让路判据是「接通位 + 通道意图值」而不是「真实接通」**。``use_reverse``
          为 True 但 ``reverse_target`` 为空之类的坏意图，会在 ``apply_channels`` 的
          spec 校验阶段被拒、通道根本接不通，而那时 ``channels_engaged`` 仍是 False，
          走不到让路分支 —— 即坏意图不会误撤 proxyauth。

        与 ``_effective_block_private`` 的让路名单差一个 **local**：原生 Block 对
        LocalMode 连接有豁免（``block.py:35``），ProxyAuth 没有，LocalMode 照样落
        401 分支。socks5 也不在让路名单里：SOCKS5 原生支持用户名/密码子协商
        （method 0x02），能认证就不该撤防（.plans/0-socks5-channel.md §1 D3）。
        """
        if not self.proxyauth_enabled or not self.proxyauth_username:
            return None
        if ":" in self.proxyauth_username or ":" in self.proxyauth_password:
            # 只可能来自手改的配置文件；静默失效比报错更坑，留一行日志。
            log.warning("代理认证的用户名或密码含冒号，原生无法解析，已停用代理认证")
            return None
        if self.channels_engaged and (
            self.use_local or self.use_wireguard or self.use_reverse
        ):
            return None
        return f"{self.proxyauth_username}:{self.proxyauth_password}"

    def _channel_intents(self) -> tuple[Any, ...]:
        """Snapshot every channel intent value, for `apply_channels` rollback.

        `apply_channels` 有三条回滚路径（spec 校验失败、内核 `OptionsError`、
        兜底 `Exception`），逐条展开同一个元组的写法在字段变多后必然漏掉某一条
        —— 收进这对方法，新增意图值只需要改 `_CHANNEL_INTENTS` 一处。
        """
        return tuple(getattr(self, name) for name in _CHANNEL_INTENTS)

    def _restore_intents(self, saved: tuple[Any, ...]) -> None:
        """Roll the channel intents back to a `_channel_intents` snapshot."""
        for name, value in zip(_CHANNEL_INTENTS, saved, strict=True):
            setattr(self, name, value)

    def apply_channels(
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
        """Switch the capture channels (and the upstream egress) on a running kernel.

        与 `apply_rewrite_rules` 同构：spec 在提交任何东西之前先过一遍原生解析器，
        坏值不会走到「内核已受理一半」。热更走 ``options.update(mode=...)`` ——
        原生 proxyserver 监听 mode 变更，diff 出增删并热启停实例；local 的提权
        守护进程被上游刻意常驻（`LocalRedirectorInstance._stop` 只清截流配置），
        所以关了再开不会再次弹 UAC。

        Raises:
            ValueError: spec 不合法，或内核拒绝（超时/启动失败等，此时内存副本回滚）。
        """
        previous = self._channel_intents()
        if use_local is not None:
            self.use_local = use_local
        if local_spec is not None:
            self.local_spec = local_spec.strip()
        if use_wireguard is not None:
            self.use_wireguard = use_wireguard
        if use_reverse is not None:
            self.use_reverse = use_reverse
        if reverse_target is not None:
            self.reverse_target = reverse_target.strip()
        if reverse_port is not None:
            self.reverse_port = reverse_port
        if use_socks5 is not None:
            self.use_socks5 = use_socks5
        if socks5_port is not None:
            self.socks5_port = socks5_port
        if use_upstream is not None:
            self.use_upstream = use_upstream
        if upstream_target is not None:
            self.upstream_target = upstream_target.strip()
        if upstream_username is not None:
            self.upstream_username = upstream_username
        if upstream_password is not None:
            self.upstream_password = upstream_password
        specs = self._mode_specs()
        try:
            validate_mode_specs(specs)
        except ValueError:
            self._restore_intents(previous)
            raise

        # block_private 要跟着让路（见 _effective_block_private）；这一步无先决
        # 条件 —— 内核没跑时先把内存副本对齐，下次启动的 _apply_block_options 才
        # 能读到正确值。
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                # mode 与 upstream_auth 必须同一次 update 推下去：分两次推会出现
                # 「槽位已换成 upstream、凭证还是上一轮的」这种半拉子中间态，
                # 期间的流量会拿着错凭证去撞上游的 407。proxyauth 同理：它对
                # local / wireguard / reverse 要让路（_effective_proxyauth），分两
                # 次推就会漏出「通道已接通、认证还在挑战」的一瞬，那一瞬里被
                # 截流的应用全吃 401。
                lambda: master.options.update(
                    mode=specs,
                    upstream_auth=self._upstream_auth(),
                    block_global=self.block_global,
                    block_private=self._effective_block_private(),
                    proxyauth=self._effective_proxyauth(),
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self._restore_intents(previous)
            raise ValueError(str(exc)) from exc
        except Exception:
            self._restore_intents(previous)
            raise

    def start(self) -> None:
        if self._thread is not None or self._state in (
            MitmRuntimeState.STARTING,
            MitmRuntimeState.RUNNING,
            MitmRuntimeState.STOPPING,
        ):
            return
        self._last_error = ""
        self._set_state(MitmRuntimeState.STARTING)
        self._generation += 1
        generation = self._generation
        thread = _MitmThread(self, generation)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(
            lambda t=thread, g=generation: self._on_thread_finished(g, t)
        )
        self._thread = thread
        thread.start()

    def stop(self, timeout_ms: int = 5000) -> bool:
        thread = self._thread
        if thread is None:
            self._master = None
            self._set_state(MitmRuntimeState.STOPPED)
            return True
        # local 的提权守护进程是**进程外**服务，内核线程死了它还活着。正常停机
        # 路径由 Servers.update 的 stop 任务清截流配置；但事件循环关闭时挂起的
        # 任务会被静默丢弃——抓包中改端口/地址触发的 restart 必然如此。守护进程
        # 随后继续按旧 spec 截流，而新内核的 mode 列表里没有 local，再没有谁会
        # 去清它（界面症状：关了本地重定向还能抓到本机流量）。所以趁事件循环
        # 还活着在它上面同步拔掉截流，不依赖任何异步任务。内核从未跑成（端口
        # 被占、立即停止）时 call 没有循环可投，此刻本进程也没接过守护进程，
        # 跳过即可。
        try:
            self.call(self._disarm_local_redirector)
        except (RuntimeError, TimeoutError):
            pass
        self._set_state(MitmRuntimeState.STOPPING)
        thread.request_shutdown()
        stopped = thread.wait(timeout_ms)
        if not stopped:
            log.error("mitmproxy runtime did not stop within %d ms", timeout_ms)
            return False
        if self._thread is thread:
            self._thread = None
        self._master = None
        self._set_state(MitmRuntimeState.STOPPED)
        return True

    @staticmethod
    def _disarm_local_redirector() -> None:
        """Runs on the mitm loop: clear the local redirector daemon's intercept spec.

        除清 spec 外还要清掉 `_instance` 占位——这是上游 `_stop` 的簿记，丢了它
        新 Master 的 `_start` 会被 "Cannot spawn more than one local redirector"
        护栏拒掉，local 通道在重启后静默死亡。`_server` 为 None 说明本进程从未
        接过守护进程，直接跳过。
        """
        if LocalRedirectorInstance._server is None:
            return
        LocalRedirectorInstance._server.set_intercept("")
        LocalRedirectorInstance._instance = None

    def restart(
        self, *, listen_host: str | None = None, listen_port: int | None = None
    ) -> None:
        if listen_host is not None:
            self.listen_host = normalize_listen_host(listen_host)
        if listen_port is not None:
            self.listen_port = listen_port
        if not self.stop():
            raise RuntimeError(
                QCoreApplication.translate(
                    "MitmRuntime",
                    "mitmproxy 内核停止超时，无法重启",
                )
            )
        self.start()

    def apply_gateway_rules(
        self,
        rules: list[GatewayRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store gateway rules and push both planes to the Master when one runs.

        与 `apply_rewrite_rules` 同构，但要多守一条：两个平面的载荷（编译好的规则
        快照 + 原生 `allow_hosts` / `ignore_hosts`）在提交**任何**东西之前就全部构造
        并校验完。坏规则不会留下「一个平面换了、另一个还是老的」的状态 —— 两个平面
        对同一条流量给出不同判定，是这套设计最不能出的错。
        """
        previous = (self.gateway_rules, self.gateway_enabled)
        self.gateway_rules = self.gateway_rules if rules is None else list(rules)
        if enabled is not None:
            self.gateway_enabled = enabled
        try:
            payload = self._gateway_payload()
        except ValueError:
            self.gateway_rules, self.gateway_enabled = previous
            raise
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: self._push_gateway(master, payload))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.gateway_rules, self.gateway_enabled = previous
            raise ValueError(str(exc)) from exc

    def _gateway_payload(self) -> tuple[GatewayRuleSet, dict[str, list[str]], bool]:
        """Compile both planes' payloads from the stored rules; commits nothing.

        Raises:
            ValueError: 任何一条启用的规则不合法（编译在这里发生，运行期钩子不编译）。
        """
        enabled = self.gateway_enabled
        return (
            GatewayRuleSet(self.gateway_rules),
            gateway_option_updates(self.gateway_rules, enabled=enabled),
            enabled,
        )

    @staticmethod
    def _push_gateway(
        master: FerretMaster,
        payload: tuple[GatewayRuleSet, dict[str, list[str]], bool],
    ) -> None:
        """Commit both planes. Only ever runs on the mitm loop."""
        ruleset, updates, enabled = payload
        # 先写原生选项（唯一还可能抛的一步），再换钩子平面的快照。
        master.options.update(**updates)
        # set_rules 顺手放行挂起中的流量：规则一变旧判定就不算数了，而挂起是永久的，
        # 不在这里放就再也没人放了。
        master.gateway.set_rules(ruleset, enabled=enabled)

    def release_suspended(self) -> int:
        """Let every suspended flow go; 返回放行条数（内核没跑就是 0）。

        规则变更与总开关都由 `_push_gateway` 顺带放行，这里是给「不改规则也要放行」
        的路径用的（清空/删除流量行，见 `MitmFacade`）。
        """
        master = self._master
        if not self.is_running or master is None:
            return 0
        return int(self.call(lambda: master.gateway.release_all()))

    def apply_rewrite_rules(
        self,
        rules: list[RewriteRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store rewrite rules and push them to the Master when one runs.

        与 `apply_gateway_rules` 同构（网关规则模式）：规则快照在提交**任何**
        东西之前就编译完（坏规则在这里抛，内存副本回滚），内核没跑就只存副本
        （下次启动 `_apply_rewrite_rules` 补推），在跑则经 ``self.call`` 把预编译
        快照换进自研 addon。总开关关掉时下发的 enabled=False 等价于空规则列表，
        但各行规则的 enabled 落盘值原样保留。

        Raises:
            ValueError: 任何一条启用且填完的规则不合法（整批回滚，绝不留半套）。
        """
        previous = (self.rewrite_rules, self.rewrite_enabled)
        candidate = self.rewrite_rules if rules is None else list(rules)
        wanted = self.rewrite_enabled if enabled is None else enabled
        try:
            snapshot = RewriteRuleSet(candidate)
        except ValueError:
            self.rewrite_rules, self.rewrite_enabled = previous
            raise
        self.rewrite_rules, self.rewrite_enabled = candidate, wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.rewrite.set_rules(snapshot, enabled=wanted))
        except Exception:
            # call 只会抛 RuntimeError / TimeoutError 这类运行期故障；与网关
            # 同一条纪律：下发失败内存副本回滚，界面显示的状态必须内核真收到了。
            self.rewrite_rules, self.rewrite_enabled = previous
            raise

    def apply_scripts(self, entries: list[ScriptEntry]) -> None:
        """Store script entries and push them to the Master when one runs.

        与 `apply_rewrite_rules` 逐行同构：整批先过 `validate()`（任何一条不合法
        整批回滚抛 ValueError），存内存副本 `self.scripts`，在跑则经 ``self.call``
        把清单推给自研 addon。`FerretScriptAddon.set_scripts` 本身绝不抛（坏脚本
        只落成状态），但 `self.call` 会抛运行期故障，下发失败同样回滚。

        Raises:
            ValueError: 任何一条脚本条目不合法（整批回滚）。
        """
        previous = self.scripts
        candidate = list(entries)
        try:
            for entry in candidate:
                entry.validate()
        except ValueError:
            self.scripts = previous
            raise
        self.scripts = candidate
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.scripts.set_scripts(self.scripts))
        except Exception:
            self.scripts = previous
            raise

    def reload_script(self, path: str) -> None:
        """强制重载一条脚本；内核没跑是 no-op（下次启动装载的就是新内容）。"""
        master = self._master
        if not self.is_running or master is None:
            return
        self.call(lambda: master.scripts.reload(path))

    def apply_intercept_rules(
        self,
        rules: list[InterceptRule] | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """Store breakpoint rules and push them to the Master when one is running.

        与 `apply_rewrite_rules` 同构：表达式在提交任何东西之前就编译并过一遍原生
        解析器。总开关关掉时下发的是 ``intercept=None``，所以「删光规则」和「关掉
        开关」走的是同一条清空路径。

        规则变更**不**顺带放行已拦下的流量（和网关 `set_rules` 刻意相反）：网关的
        挂起是规则的副产物，规则一改旧判定就不算数了；断点拦下的这条流量用户正在
        编辑器里改，改到一半去动规则列表不该把它冲掉。放行始终是显式动作。
        """
        previous = (self.intercept_rules, self.intercept_enabled)
        candidate = self.intercept_rules if rules is None else list(rules)
        wanted = self.intercept_enabled if enabled is None else enabled
        updates = intercept_option_updates(candidate, enabled=wanted)
        self.intercept_rules, self.intercept_enabled = candidate, wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.options.update(**updates))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.intercept_rules, self.intercept_enabled = previous
            raise ValueError(str(exc)) from exc

    def apply_sticky_session(self, enabled: bool | None = None) -> None:
        """Store the sticky-session switch and push it to a running Master.

        与 `apply_block_options` 同构：内核没跑就只对齐内存副本（下次启动的
        `_apply_sticky_session` 会读到它），下发失败回滚，绝不留下「界面显示已
        生效、内核其实没收到」的状态。关掉只下发 ``None`` —— 原生 addon 的 jar /
        hosts 缓存刻意不倒，重开开关立即复用（只在代理内存、不落盘）。
        """
        wanted = self.sticky_session_enabled if enabled is None else enabled
        previous = self.sticky_session_enabled
        self.sticky_session_enabled = wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(**sticky_session_option_updates(wanted))
            )
        except OptionsError as exc:
            self.sticky_session_enabled = previous
            raise ValueError(str(exc)) from exc

    def apply_anticache_plaintext(self, enabled: bool | None = None) -> None:
        """Store the anticache/anticomp switch and push it to a running Master.

        与 `apply_block_options` 同构（两个都是 bool 选项，错误模型照它而不是
        sticky 的过滤串）：内核没跑只对齐内存副本（下次启动
        `_apply_anticache_plaintext` 会读到），下发失败回滚，绝不留下「界面显示
        已生效、内核其实没收到」的状态。
        """
        wanted = self.anticache_plaintext if enabled is None else enabled
        previous = self.anticache_plaintext
        self.anticache_plaintext = wanted
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(lambda: master.options.update(**anticache_option_updates(wanted)))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.anticache_plaintext = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            # bool 选项传错类型 optmanager 抛 TypeError（未知键抛 KeyError），不经
            # OptionsError —— 编程错误原样抛出，但内存副本必须先回滚。
            self.anticache_plaintext = previous
            raise

    def apply_dns_options(
        self,
        *,
        name_servers: list[str] | None = None,
        use_hosts_file: bool | None = None,
    ) -> None:
        """Store the DNS options and push them to a running Master.

        与 `apply_anticache_plaintext` 同构：内核没跑只对齐内存副本（下次启动
        `_apply_dns_options` 会读到），下发失败回滚，绝不留下「界面显示已生效、
        内核其实没收到」的状态。两个参数都是 **None = 不改动该项** —— 清空自定义
        DNS（回系统）必须显式传 ``[]``，与 bool 开关的 None 语义刻意区分。坏 IP
        串在动内存副本**之前**就被 `_validate_dns_name_servers` 拒掉（原生该选项
        无校验器，静默放行坏值的路径不存在）。
        """
        wanted_ns = self.dns_name_servers if name_servers is None else name_servers
        wanted_hosts = (
            self.dns_use_hosts_file if use_hosts_file is None else use_hosts_file
        )
        checked = _validate_dns_name_servers(wanted_ns)
        previous = (self.dns_name_servers, self.dns_use_hosts_file)
        self.dns_name_servers = checked
        self.dns_use_hosts_file = wanted_hosts
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(
                    **dns_option_updates(checked, wanted_hosts)
                )
            )
        except OptionsError as exc:
            self.dns_name_servers, self.dns_use_hosts_file = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            self.dns_name_servers, self.dns_use_hosts_file = previous
            raise

    def apply_ssl_options(
        self,
        *,
        insecure: bool | None = None,
        trusted_ca_files: list[str] | None = None,
        add_upstream_certs: bool | None = None,
    ) -> None:
        """Store the upstream-TLS trust options and push them to a running Master.

        与 `apply_dns_options` 同构：三个参数都是 **None = 不改动该项**（清空信任
        文件必须显式传 ``[]``），内核没跑只对齐内存副本，下发失败回滚内存副本后
        抛 `ValueError`，绝不留下「界面显示已生效、内核其实没收到」的状态。

        与 DNS 不同的一点是**没有坏值前置闸门**：信任文件解不动是常态（用户删了、
        挪了、给了张截图），`build_trusted_ca_bundle` 把它们当返回值挑出来而不是
        抛异常 —— 一把坏证书不该让整次保存失败。界面自己调 `inspect_trusted_ca_files`
        复算并显示「N 个文件已失效」。真正会抛的只有写盘失败（`CertificateError`，
        `RuntimeError` 的子类），那一支在动内存副本**之前**抛出去，配置不落盘。
        """
        wanted_insecure = self.ssl_insecure if insecure is None else insecure
        wanted_files = (
            self.ssl_trusted_ca_files if trusted_ca_files is None else trusted_ca_files
        )
        wanted_splice = (
            self.add_upstream_certs_to_client_chain
            if add_upstream_certs is None
            else add_upstream_certs
        )
        previous = (
            self.ssl_insecure,
            self.ssl_trusted_ca_files,
            self.add_upstream_certs_to_client_chain,
        )
        master = self._master
        running = self.is_running and master is not None
        # 合并产物只在真要下发时才生成：内核没跑就写一份没人读的 PEM，纯属给证书
        # 目录添垃圾（下次启动 `_apply_ssl_options` 会照**那时**的文件重算）。
        bundle = build_trusted_ca_bundle(wanted_files)[0] if running else None
        self.ssl_insecure = wanted_insecure
        self.ssl_trusted_ca_files = list(wanted_files)
        self.add_upstream_certs_to_client_chain = wanted_splice
        if not running or master is None:
            return
        try:
            self.call(
                lambda: master.options.update(
                    **ssl_option_updates(wanted_insecure, bundle, wanted_splice)
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            (
                self.ssl_insecure,
                self.ssl_trusted_ca_files,
                self.add_upstream_certs_to_client_chain,
            ) = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            (
                self.ssl_insecure,
                self.ssl_trusted_ca_files,
                self.add_upstream_certs_to_client_chain,
            ) = previous
            raise

    def apply_client_certs(self, *, path: str | None = None) -> None:
        """Store the mTLS client-certificate path and push it to a running Master.

        与 `apply_ssl_options` 同构：``None`` = 不改动该项（清除必须显式传 ``""``），
        内核没跑只对齐内存副本，下发失败回滚内存副本后抛 `ValueError`。

        与上游信任那刀不同的是**有前置闸门**：信任文件解不动可以回退公共根，而客户端
        证书解不动没有任何降级余地 —— 不拦下来，用户会得到一次「配了却不出示」的静默
        失败（私钥与证书不配对时 OpenSSL 连报错都不给，见 certificate.py）。闸门在动
        内存副本**之前**判，坏值不落盘、不进内核。

        目录模式只查存在性：里面的坏文件由界面盘点显示，不拦保存（判据见
        `client_certs_error`）。
        """
        wanted = self.client_certs_path if path is None else path
        reason = client_certs_error(wanted)
        if reason:
            raise ValueError(reason)
        previous = self.client_certs_path
        master = self._master
        running = self.is_running and master is not None
        self.client_certs_path = wanted
        if not running or master is None:
            return
        try:
            # 先 update 后清缓存：反过来会给失败路径清掉本来好好的 context，无害
            # 但白重建一遍。
            self.call(
                lambda: master.options.update(**client_certs_option_updates(wanted))
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.client_certs_path = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            self.client_certs_path = previous
            raise
        # 清除（``""``）这条路径同样要清：关掉功能之后不该还有残留 context 在出示。
        clear_proxy_server_context_cache()

    def apply_body_cut(
        self,
        enabled: bool | None = None,
        size: int | None = None,
    ) -> None:
        """Store the body-cut switch/threshold and push them to a running Master.

        与 `apply_sticky_session` 同构：`None` = 不改动该项，内核没跑只对齐内存
        副本（下次启动 `_apply_body_cut` 补推），下发失败回滚内存副本。阈值是
        addon 的内存快照（不落原生 options），所以下发不会抛 `OptionsError`，
        只有 `self.call` 本身的运行期故障（RuntimeError / TimeoutError）需要回滚。
        阈值先过 `clamp_body_cut_size`：配置是用户可手改的纯文本，旋钮只是第一
        道闸，这里收底。
        """
        previous = (self.body_cut_enabled, self.body_cut_size)
        self.body_cut_enabled = self.body_cut_enabled if enabled is None else enabled
        self.body_cut_size = (
            self.body_cut_size if size is None else clamp_body_cut_size(size)
        )
        master = self._master
        if not self.is_running or master is None:
            return
        try:
            self.call(
                lambda: master.cut.set_options(
                    enabled=self.body_cut_enabled,
                    max_size=self.body_cut_size,
                )
            )
        except Exception:
            # 与 `apply_rewrite_rules` 同一条纪律：下发失败内存副本回滚，界面
            # 显示的状态必须内核真收到了。
            self.body_cut_enabled, self.body_cut_size = previous
            raise

    def release_intercepted(self) -> int:
        """Let every breakpoint-held flow go; 返回放行条数（内核没跑就是 0）。

        只管断点这本账。网关挂起的那批走 `release_suspended` —— 两边各记一本，
        谁拦的谁放（见 `InterceptState.arm` 对 `flow.intercepted` 的退让）。
        """
        master = self._master
        if not self.is_running or master is None:
            return 0
        return int(self.call(lambda: master.intercept_state.release_all()))

    def channel_health(self) -> dict[str, bool | str]:
        """Report per-channel liveness of the running kernel.

        ``options.update(mode=...)`` 只保证 spec 语法合法并触发热启停，实例**启动**
        失败（UAC 拒绝、端口被占等）由原生 proxyserver 记日志吞掉，不会同步抛回。
        这里逐实例读 ``is_running`` / ``last_exception``，给界面一个可靠的「通道
        真的起来了吗」。键是 ``local`` / ``wireguard`` / ``reverse`` / ``socks5``，
        只在对应通道启用时出现；regular 由端口占用与 UiBridgeAddon.running 的全局
        探测兜底，不单列。
        """
        master = self._master
        if not self.is_running or master is None:
            return {}
        health: dict[str, bool | str] = {}
        for server in master.proxyserver.servers:
            spec = server.mode.full_spec
            if spec.startswith("local"):
                key = "local"
            elif spec.startswith("wireguard"):
                key = "wireguard"
            elif spec.startswith("reverse"):
                key = "reverse"
            elif spec.startswith("socks5"):
                key = "socks5"
            else:
                continue
            health[key] = server.is_running
            if not server.is_running and server.last_exception is not None:
                health[key] = str(server.last_exception)
        return health

    def apply_block_options(
        self, *, block_global: bool | None = None, block_private: bool | None = None
    ) -> None:
        """Store Block's source filters and push them to a running Master.

        与 `apply_block_rules` 同构：下发失败就把内存副本回滚，绝不留下「界面显示
        已生效、内核其实没收到」的状态。
        """
        previous = (self.block_global, self.block_private)
        if block_global is not None:
            self.block_global = block_global
        if block_private is not None:
            self.block_private = block_private
        master = self._master
        if not self.is_running or master is None:
            return
        wanted = (self.block_global, self._effective_block_private())
        try:
            self.call(
                lambda: master.options.update(
                    block_global=wanted[0], block_private=wanted[1]
                )
            )
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            self.block_global, self.block_private = previous
            raise ValueError(str(exc)) from exc
        except Exception:
            # 这两个是 bool 选项，传错类型 optmanager 抛的是 TypeError（未知键则是
            # KeyError），都不经 OptionsError。那属于编程错误，照原样抛出去 ——
            # 但内存副本必须先回滚，否则界面会显示一个内核根本没收到的状态。
            self.block_global, self.block_private = previous
            raise

    def apply_proxy_auth(
        self,
        *,
        enabled: bool | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Store the inbound proxy credential and push it to a running Master.

        与 `apply_block_options` 同构：内核没跑只对齐内存副本（下次启动的
        `_apply_proxyauth` 会读到），下发失败回滚，绝不留下「界面显示已生效、
        内核其实没收到」的状态。

        三项一次推是有意的：下发值是 `_effective_proxyauth()` 现算的单个字符串，
        拆开推等于中途真的把「新用户名 + 旧密码」这种组合下发下去过。
        """
        previous = (
            self.proxyauth_enabled,
            self.proxyauth_username,
            self.proxyauth_password,
        )
        if enabled is not None:
            self.proxyauth_enabled = enabled
        if username is not None:
            self.proxyauth_username = username
        if password is not None:
            self.proxyauth_password = password
        master = self._master
        if not self.is_running or master is None:
            return
        wanted = self._effective_proxyauth()
        try:
            self.call(lambda: master.options.update(proxyauth=wanted))
        except OptionsError as exc:
            # 让 apps/ 只需要认识内建异常，不必 import mitmproxy 的异常类型。
            # 正常路径到不了这里（冒号已被闸门挡掉），留着是因为 spec 的唯一
            # 真相源是原生 configure，不该假设它只认冒号这一种坏值。
            self._restore_proxyauth(previous)
            raise ValueError(str(exc)) from exc
        except Exception:
            self._restore_proxyauth(previous)
            raise

    def _restore_proxyauth(self, previous: tuple[bool, str, str]) -> None:
        """Roll the three proxyauth intents back after a failed push."""
        (
            self.proxyauth_enabled,
            self.proxyauth_username,
            self.proxyauth_password,
        ) = previous

    def reload_certificate_store(self) -> bool:
        """Rebuild the live CertStore through mitmproxy's own TlsConfig hook.

        `optmanager.update_known` 对传入的每个键都发 `changed`（即使值没变），所以
        重写一次 `confdir` 就会触发原生 `TlsConfig.configure({"confdir"})` →
        `CertStore.from_store`：重新生成后的新 CA 立刻对后续连接生效，无需重启内核。
        返回 False 表示内核没在跑，下次启动时自然会读到新证书。
        """
        master = self._master
        if not self.is_running or master is None:
            return False
        self.call(lambda: master.options.update(confdir=str(get_certs_dir())))
        return True

    def call(self, callback: Callable[[], Any], *, timeout: float = 5.0) -> Any:
        thread = self._thread
        master = self._master
        loop = thread.loop if thread is not None else None
        if not self.is_running or master is None or loop is None:
            raise RuntimeError(
                QCoreApplication.translate("MitmRuntime", "mitmproxy 内核未运行")
            )
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            raise RuntimeError("不能在 mitmproxy event loop 中同步调用")

        async def invoke() -> Any:
            result = callback()
            if inspect.isawaitable(result):
                return await result
            return result

        future = asyncio.run_coroutine_threadsafe(invoke(), loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                QCoreApplication.translate("MitmRuntime", "mitmproxy 任务执行超时")
            ) from exc

    def _on_flow_suspended(self, flow: Any) -> None:
        """Republish a suspend/release from the mitm thread as a Qt signal.

        挂起（出）发生在 `request`，而 `View` 只有 `requestheaders` / `response` /
        `error` 几个钩子 —— 不自己发一次，流量表那一行不会重绘、「挂起中」标记就
        永远不上屏。跨线程 emit 走 Qt 的队列连接，和 `UiBridgeAddon` 同一条路子。
        """
        self.flow_suspended.emit(flow)

    def _on_flow_intercepted(self, flow: Any) -> None:
        """Republish a breakpoint hold/release from the mitm thread as a Qt signal.

        与 `_on_flow_suspended` 同一个理由：请求期拦截发生在 `request` 钩子，而
        `View` 只在 `requestheaders` / `response` / `error` 上更新行，不自己补发
        一次「已拦截」就永远不上屏。
        """
        self.flow_intercepted.emit(flow)

    def _set_state(self, state: MitmRuntimeState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)

    def _on_master_created(self, generation: int, master: FerretMaster) -> None:
        if generation != self._generation or self._state != MitmRuntimeState.STARTING:
            return
        self._master = master

    def _on_master_running(self, generation: int) -> None:
        if (
            generation != self._generation
            or self._state != MitmRuntimeState.STARTING
            or self._master is None
        ):
            return
        self._set_state(MitmRuntimeState.RUNNING)
        try:
            self.apply_gateway_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("网关规则下发失败: %s", exc)
        try:
            self.apply_block_options()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("来源过滤开关下发失败: %s", exc)
        try:
            self.apply_rewrite_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("重写规则下发失败: %s", exc)
        try:
            self.apply_intercept_rules()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("断点规则下发失败: %s", exc)
        try:
            self.apply_sticky_session()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("固定会话开关下发失败: %s", exc)
        try:
            self.apply_anticache_plaintext()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            log.warning("无缓存·明文开关下发失败: %s", exc)
        self.ready.emit(self.view)

    def _on_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._last_error = message
        self._master = None
        self._set_state(MitmRuntimeState.FAILED)
        self.failed.emit(message)

    def _on_thread_finished(self, generation: int, thread: _MitmThread) -> None:
        if generation != self._generation or self._thread is not thread:
            return
        self._thread = None
        self._master = None
        if self._state not in (MitmRuntimeState.FAILED, MitmRuntimeState.STOPPED):
            self._set_state(MitmRuntimeState.STOPPED)
        self.stopped.emit()
