"""Ferret's reusable mitmproxy addons."""

from collections.abc import Callable, Iterable

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    AddonHalt,
    HTTPFlow,
    Response,
    TlsConfig,
    connection,
    human,
    server_hooks,
    status_codes,
    tlsconfig_module,
)
from ferret.core.mitm.gateway import (
    GATEWAY_METADATA_KEY,
    GATEWAY_STATUS_CLOSE,
    GatewayDecision,
    GatewayPolicy,
    GatewayRuleSet,
)
from ferret.core.settings import APP_NAME


class FerretTlsConfig(TlsConfig):
    """Use Ferret's name for generated certificate files."""

    def configure(self, updated):
        original = tlsconfig_module.CONF_BASENAME
        tlsconfig_module.CONF_BASENAME = APP_NAME
        try:
            super().configure(updated)
        finally:
            tlsconfig_module.CONF_BASENAME = original


class LogAddon:
    """Log the proxy connection and HTTP lifecycle."""

    def __init__(self) -> None:
        self._log = get_logger("mitmproxy")

    def client_connected(self, client: connection.Client) -> None:
        address = f"{client.peername[0]}:{client.peername[1]}"
        self._log.info("[%s] client connect", address)

    def server_connected(self, data: server_hooks.ServerConnectionHookData):
        client = data.client
        server = data.server
        client_address = f"{client.peername[0]}:{client.peername[1]}"
        server_address = (
            f"{server.address[0]}:{server.address[1]}" if server.address else "unknown"
        )
        ip_port = (
            f"{server.peername[0]}:{server.peername[1]}"
            if server.peername
            else "unknown"
        )
        self._log.info(
            "[%s] server connect %s (%s)",
            client_address,
            server_address,
            ip_port,
        )

    def request(self, flow: HTTPFlow) -> None:
        request = flow.request
        if request is None:
            return
        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        self._log.info(
            "%s %s %s %s",
            client_address,
            request.method,
            request.pretty_url,
            request.http_version,
            extra={"raw": True},
        )

    def response(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None:
            return
        status = response.status_code
        reason = response.reason or status_codes.RESPONSES.get(status, "")
        friendly_size = human.pretty_size(
            len(response.content) if response.content else 0
        )
        self._log.info(
            "      << %s %s %s %s",
            response.http_version,
            status,
            reason,
            friendly_size,
            extra={"raw": True},
        )

    def error(self, flow: HTTPFlow) -> None:
        if flow.error is not None:
            self._log.info("      << %s", flow.error.msg, extra={"raw": True})

    def http_connect_error(self, flow: HTTPFlow) -> None:
        request = flow.request
        if request is None:
            return
        conn = flow.client_conn
        client_address = f"{conn.peername[0]}:{conn.peername[1]}"
        self._log.info(
            "%s %s %s %s",
            client_address,
            request.method,
            request.pretty_url,
            request.http_version,
            extra={"raw": True},
        )
        message = flow.error.msg if flow.error else "connection failed"
        self._log.info("      << %s", message, extra={"raw": True})


# 挂起上限。一条挂起会把客户端 socket、`handle_connection` 任务和这次钩子派发一起
# 钉住：`handle_hook`（`proxy/mode_servers.py:69-75`）在整个 `await wait_for_resume()`
# 期间都 `disarm()` 了看门狗，`tcp_timeout`（默认 600s）**不会**兜底；挂起期间连
# 客户端断开都处理不了（连接事件排在这次钩子后面），所以泄漏是永久的，只能靠显式
# 放行收回。到顶就不再挂起：宁可漏掉一条策略，也不能让内核被拖死。
SUSPEND_LIMIT = 128


class GatewayState:
    """The gateway's mutable state: rule snapshot, master switch, suspended flows.

    只在 mitm 线程上读写 —— 钩子本来就跑在 mitm 线程，规则下发经 `MitmRuntime.call`
    也会被 marshal 到同一个 loop 上，所以这里不需要锁。
    """

    def __init__(self) -> None:
        self._log = get_logger("mitmproxy")
        self._rules = GatewayRuleSet()
        self._enabled = True
        self._suspended: dict[str, HTTPFlow] = {}
        # 挂起（出）发生在 `request`，而 `View` 没有 `request` 钩子 —— 不主动通知，
        # 界面上那一行不会重绘。由 runtime 接成 Qt 信号。
        self.on_suspend_changed: Callable[[HTTPFlow], None] | None = None

    @property
    def suspended_count(self) -> int:
        return len(self._suspended)

    def set_rules(self, rules: GatewayRuleSet, *, enabled: bool = True) -> None:
        """Swap in a pre-compiled snapshot; 挂起中的流量一律放行。

        规则一变，旧判定就不再算数：挂起是永久的，不放行就再也没人放行了。
        """
        self._rules = rules
        self._enabled = enabled
        self.release_all()

    def decide(
        self, host: str, port: int, method: str | None = None
    ) -> GatewayDecision | None:
        """Resolve the policy for one piece of traffic (``None`` 表示正常抓取)."""
        if not self._enabled:
            return None
        return self._rules.decide(host, port, method)

    def suspend(self, flow: HTTPFlow, policy: GatewayPolicy) -> bool:
        """Hold a flow open until it is released; ``False`` 表示到顶了没挂起。"""
        if flow.id in self._suspended:
            return True
        if len(self._suspended) >= SUSPEND_LIMIT:
            self._log.warning(
                "挂起数已达上限 %d，本条流量照常放行: %s",
                SUSPEND_LIMIT,
                flow.request.pretty_url if flow.request else flow.id,
            )
            return False
        # 只能同步 intercept()，绝不能在钩子里 await：`handle_hook` 在
        # `handle_lifecycle(hook)` 之后本来就会 `await wait_for_resume()`，addon
        # 自己 await 只会饿死链上后面的 addon。同步标记完钩子链照常跑完，
        # `View` 能正常更新行，真正的挂起发生在 addon 链之外。
        flow.intercept()
        self._suspended[flow.id] = flow
        flow.metadata[GATEWAY_METADATA_KEY] = str(policy)
        self._notify(flow)
        return True

    def release(self, flow_ids: Iterable[str], *, kill: bool = False) -> int:
        """Let the named suspended flows go; 不在挂起表里的 id 直接忽略。"""
        released = 0
        for flow_id in flow_ids:
            flow = self._suspended.pop(flow_id, None)
            if flow is None:
                continue
            self._release(flow, kill=kill)
            released += 1
        return released

    def release_all(self, *, kill: bool = False) -> int:
        """Let every suspended flow go; ``kill`` 时顺手断连（停机路径用）。"""
        pending = list(self._suspended.values())
        self._suspended.clear()
        for flow in pending:
            self._release(flow, kill=kill)
        return len(pending)

    def _release(self, flow: HTTPFlow, *, kill: bool) -> None:
        flow.metadata.pop(GATEWAY_METADATA_KEY, None)
        # 顺序不能反：`kill()` 会把 `intercepted` 清成 False，而 `resume()` 开头就是
        # `if not self.intercepted: return`（`flow.py:226-273`）—— 先 kill 再 resume
        # 会让这条 flow 永久挂死在 `wait_for_resume()` 上。`kill()` 还要先看
        # `killable`，否则抛 `ControlException`。
        flow.resume()
        if kill and flow.killable:
            flow.kill()
        self._notify(flow)

    def _notify(self, flow: HTTPFlow) -> None:
        callback = self.on_suspend_changed
        if callback is not None:
            callback(flow)


class GatewayL4Addon:
    """Enforce the connection-level 屏蔽 policy.

    仅允许 / 绕行两条不在这里：它们是原生 `allow_hosts` / `ignore_hosts`
    （`addons/next_layer.py`），由 `gateway_option_updates` 下发到选项上。
    """

    def __init__(self, state: GatewayState) -> None:
        self._state = state

    def server_connect(self, data: server_hooks.ServerConnectionHookData) -> None:
        server = data.server
        address = server.address
        if address is None or server.error:
            return
        decision = self._state.decide(address[0], address[1])
        if decision is not None and decision.policy == GatewayPolicy.BLOCK:
            # `proxy/server.py:196-204` 在钩子返回后读 `connection.error`，非空就不连、
            # 改走 ServerConnectErrorHook —— 和原生 Block addon 拒来源是同一招。
            server.error = "blocked by gateway"


class GatewayL7Addon:
    """Enforce the flow-level gateway policies.

    挂在 `View` **正前方**，于是：跑在 `MapRemote` 之后，判定看到的是**重写之后**的
    目标主机（网关管的是「流量到不了服务器」，主机当然按真实目的地算）；抛
    `AddonHalt` 就能让 `View` / `Save` / `LogAddon` / `UiBridgeAddon` 一个都收不到。

    L4 策略在这里**再判一次**，不是重复执行：原生 `ignore_hosts` / `allow_hosts`
    只在 `next_layer` 生效，而显式代理下的明文 HTTP 压根走不到那里（mitmproxy 自己的
    帮助文本写着 "In regular mode, only SSL traffic is ignored"），只能在这里兜底；
    被原生那一层处理掉的连接又压根不产生 flow。L4 屏蔽同理 —— 连接复用时
    `server_connect` 不会重放，新加的屏蔽规则否则得等连接池换血才生效。
    """

    def __init__(self, state: GatewayState) -> None:
        self._state = state

    def requestheaders(self, flow: HTTPFlow) -> None:
        self._gate(flow)

    def request(self, flow: HTTPFlow) -> None:
        decision = self._gate(flow)
        if decision is None or flow.response or flow.error or not flow.live:
            return
        policy = decision.policy
        if policy == GatewayPolicy.BLOCK:
            self._kill(flow, policy)
        elif policy == GatewayPolicy.BLOCK_OUT:
            self._block_out(flow, decision.status_code)
        elif policy == GatewayPolicy.SUSPEND_OUT:
            self._state.suspend(flow, policy)

    def response(self, flow: HTTPFlow) -> None:
        decision = self._gate(flow)
        if decision is None or flow.error or not flow.live:
            return
        policy = decision.policy
        if policy == GatewayPolicy.BLOCK_IN:
            self._kill(flow, policy)
        elif policy == GatewayPolicy.SUSPEND_IN:
            self._state.suspend(flow, policy)

    def error(self, flow: HTTPFlow) -> None:
        self._gate(flow)

    def done(self) -> None:
        """Master 要停了：把挂起的全放掉，别留着任务钉住 loop 的收尾。"""
        self._state.release_all(kill=True)

    def _gate(self, flow: HTTPFlow) -> GatewayDecision | None:
        """Resolve this flow's policy, truncating the chain for 绕行 / 仅允许.

        Raises:
            AddonHalt: 这条流量不该被抓 —— `addonmanager.trigger_event:285-294`
                捕获后直接 return，链上后面的 addon 一个都不会跑。
                不能改用 `View.set_filter`：`View.add()` 无条件写 `_store`，filter
                只管可见列表，计数 / HAR 导出 / 录制照样会漏。每个钩子都是一次独立
                派发，所以四个钩子都要判、都要抛。
        """
        request = flow.request
        if request is None:
            return None
        decision = self._state.decide(request.host, request.port, request.method)
        if decision is not None and decision.policy == GatewayPolicy.BYPASS:
            raise AddonHalt
        return decision

    def _block_out(self, flow: HTTPFlow, status_code: int) -> None:
        if status_code == GATEWAY_STATUS_CLOSE:
            self._kill(flow, GatewayPolicy.BLOCK_OUT)
            return
        flow.metadata[GATEWAY_METADATA_KEY] = str(GatewayPolicy.BLOCK_OUT)
        # 不走原生 block_list：`BlockList` 在 addon 链里位于网关**之前**，而
        # `AddonHalt` 只截断当前这一次派发，下一个钩子照样从链首重来 —— 高优先级的
        # 绕行规则否决不掉它。自己回响应才能让优先级说了算。
        flow.response = Response.make(status_code)

    def _kill(self, flow: HTTPFlow, policy: GatewayPolicy) -> None:
        flow.metadata[GATEWAY_METADATA_KEY] = str(policy)
        if flow.killable:
            flow.kill()


__all__ = [
    "SUSPEND_LIMIT",
    "FerretTlsConfig",
    "GatewayL4Addon",
    "GatewayL7Addon",
    "GatewayState",
    "LogAddon",
]
