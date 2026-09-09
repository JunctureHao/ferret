"""Ferret's reusable mitmproxy addons."""

import mimetypes
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import Path

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    AddonHalt,
    HTTPFlow,
    Response,
    TlsConfig,
    connection,
    human,
    safe_join,
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
from ferret.core.mitm.rewrite import (
    REPLACE_RESPONSE_DEFAULT_STATUS,
    REWRITE_ANSWERED_KEY,
    CompiledRewrite,
    RewriteKind,
    RewriteRuleSet,
    read_replacement,
)
from ferret.core.settings import APP_NAME


class CertDownloadAddon:
    """访问  http://ferret-ca/ 直接下载 CA PEM 证书(无页面)"""

    HOST = "ferret-ca"

    def __init__(self, tls_config: TlsConfig) -> None:
        self._tls_config = tls_config

    def request(self, flow: HTTPFlow) -> None:
        if flow.request.pretty_host != self.HOST or flow.request.method != "GET":
            return
        certstore = self._tls_config.certstore
        if certstore is None:
            # configure 还没跑过（开始服务前理论上不会发生）
            flow.response = Response.make(503, b"Ferret CA is not ready yet.")
            return
        flow.response = Response.make(
            200,
            certstore.default_ca.to_pem(),
            {
                "Content-Type": "application/x-x509-ca-cert",
                "Content-Disposition": 'attachment; filename="ferret-ca.pem"',
            },
        )


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


class FerretRewriteAddon:
    """自研统一重写引擎（plans/rewrite-ui.md §5）：八个类型一个 addon。

    原生 MapRemote / MapLocal / ModifyHeaders / ModifyBody 四件退役的动机与
    语义契约见 `core/mitm/rewrite.py` 的模块 docstring。这里只管执行：

    - 请求期类型全部落在 `request` 钩子、响应期类型全部落在 `response` 钩子，
      **按列表行序逐条作用、不短路** —— 行序＝执行序，跨类型也有意义。请求期
      钩子还保证原生断点（`Intercept` 也在 `request` 拦）拦到的是替换后的报文。
    - 每条规则独立 try/except：一条规则执行炸了记日志跳过，绝不打断钩子链，
      更不能连坐整批（编译期的校验已在 `RewriteRuleSet` 构造时完成）。
    - 文件映射的目录候选算法逐行对齐原生 `MapLocal.file_candidates`，路径拼接
      走 `bindings.safe_join`（werkzeug 等价守卫）—— URL 后缀是不可信输入。
    - ``@文件`` 每请求现读（`:data:`~ferret.core.mitm.rewrite.FILE_REPLACEMENT_PREFIX``），
      比原生「spec 解析时定格」更利于 mock 迭代，是刻意差异。
    """

    def __init__(self) -> None:
        self._log = get_logger("mitmproxy")
        self._rules = RewriteRuleSet()
        self._enabled = True

    def set_rules(self, rules: RewriteRuleSet, *, enabled: bool = True) -> None:
        """Swap in a pre-compiled snapshot. 只在 mitm 线程上调用（网关规则模式）。"""
        self._rules = rules
        self._enabled = enabled

    # —— 钩子 ——

    def request(self, flow: HTTPFlow) -> None:
        if not self._enabled or flow.error or not flow.live:
            return
        for entry in self._rules.entries():
            # 每条规则都对**当前** URL 重新匹配：重定向规则链式生效
            # （a→b 之后 b→c 照样命中），对齐原生 MapRemote 的逐条重读语义。
            url = flow.request.pretty_url
            if not entry.matches(url):
                continue
            kind = entry.rule.kind
            try:
                if kind == RewriteKind.MODIFY_REQUEST_HEADER:
                    self._modify_header(flow.request.headers, entry)
                elif kind == RewriteKind.MODIFY_REQUEST_BODY:
                    self._modify_body(flow.request, entry)
                elif kind == RewriteKind.MAP_REMOTE:
                    self._map_remote(flow, entry)
                elif kind == RewriteKind.MAP_LOCAL:
                    self._map_local(flow, entry, url)
                elif kind == RewriteKind.REPLACE_REQUEST:
                    self._replace_request(flow, entry)
                # 前面某条映射规则已经作答的流量不再整条替换。
                elif kind == RewriteKind.REPLACE_RESPONSE and flow.response is None:
                    self._replace_response(flow, entry)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("重写规则执行失败，本条已跳过: %s", exc)

    def response(self, flow: HTTPFlow) -> None:
        if not self._enabled or flow.response is None or not flow.live:
            return
        url = flow.request.pretty_url
        for entry in self._rules.entries():
            if not entry.matches(url):
                continue
            kind = entry.rule.kind
            try:
                if kind == RewriteKind.MODIFY_RESPONSE_HEADER:
                    self._modify_header(flow.response.headers, entry)
                elif kind == RewriteKind.MODIFY_RESPONSE_BODY:
                    self._modify_body(flow.response, entry)
            except Exception as exc:  # noqa: BLE001
                self._log.warning("重写规则执行失败，本条已跳过: %s", exc)

    # —— 执行分支 ——

    def _modify_header(self, headers, entry: CompiledRewrite) -> None:
        """先删同名头、非空再按新值加回（§5 契约；mitmproxy Headers 大小写不敏感）。

        对齐原生 `ModifyHeaders.run`：`pop(subject, None)` 删不掉不炸；`add` 直接收
        bytes —— `@文件` 读出的内容不必是 utf-8 文本也能当头值用。
        """
        headers.pop(entry.header_name, None)
        replacement = entry.rule.replacement
        if not replacement:
            return
        headers.add(entry.header_name, read_replacement(replacement))

    def _modify_body(self, message, entry: CompiledRewrite) -> None:
        """对 **utf-8 可解码**的体做正则替换，重新编码；二进制体跳过。"""
        content = message.get_content(strict=False)
        if content is None:
            return
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            # 二进制体跳过：debug 日志落一条，不静默（plans/rewrite-ui.md §13）。
            self._log.debug(
                "体正则规则跳过二进制体: %s",
                entry.rule.target or "(整体替换)",
            )
            return
        try:
            replacement = read_replacement(entry.rule.replacement).decode("utf-8")
        except UnicodeDecodeError:
            self._log.warning("体替换内容不是 utf-8 文本，本条已跳过")
            return
        # 编译期保证 entry.body 非 None（仅体类型会走到这里）。
        assert entry.body is not None
        # 直接写 content：mitmproxy 自动重算 Content-Length；charset 保持原头不动。
        message.content = entry.body.sub(replacement, text).encode("utf-8")

    def _map_remote(self, flow: HTTPFlow, entry: CompiledRewrite) -> None:
        """``re.sub(subject, template, pretty_url)`` → ``request.url``。

        scheme / host / port / Host 头随 url setter 自动更新 —— 这也是不自造
        重定向的理由之一（`RewriteRule.template` 的注释）。
        """
        flow.request.url = entry.url.sub(entry.rule.template, flow.request.pretty_url)

    def _map_local(self, flow: HTTPFlow, entry: CompiledRewrite, url: str) -> None:
        """文件映射：现读本地文件直接作答，请求不出网（对齐原生 MapLocal）。"""
        if flow.response is not None:
            return
        root = Path(entry.rule.replacement.strip()).expanduser()
        candidates = [root] if root.is_file() else self._local_candidates(root, entry, url)
        local_file = next((c for c in candidates if c.is_file()), None)
        if local_file is None:
            if candidates:
                # 对齐原生：目录候选一个都不在盘上时回 404，而不是放行去撞服务器。
                self._log.info(
                    "文件映射候选均不存在: %s",
                    ", ".join(str(c) for c in candidates),
                )
                flow.response = Response.make(404)
                flow.metadata[REWRITE_ANSWERED_KEY] = "1"
            return
        try:
            contents = local_file.read_bytes()
        except OSError as exc:
            self._log.warning("文件映射读取失败: %s", exc)
            return
        headers = {}
        mimetype = mimetypes.guess_type(str(local_file))[0]
        if mimetype:
            headers["Content-Type"] = mimetype
        flow.response = Response.make(200, contents, headers)
        flow.metadata[REWRITE_ANSWERED_KEY] = "1"

    @staticmethod
    def _local_candidates(
        root: Path, entry: CompiledRewrite, url: str
    ) -> list[Path]:
        """目录映射的候选文件，逐行对齐原生 `MapLocal.file_candidates`。"""
        match = entry.url.search(url)
        assert match is not None  # 调用方已用同一 pattern 筛过
        if match.groups():
            suffix = match.group(1)
        else:
            suffix = entry.url.split(url, maxsplit=1)[1]
            suffix = suffix.split("?")[0].strip("/")
        if not suffix:
            return [root / "index.html"]
        decoded = urllib.parse.unquote(suffix)
        candidates = [decoded, f"{decoded}/index.html"]
        escaped = "".join(c if c.isalnum() or c in "-_.=(),/" else "_" for c in decoded)
        if escaped != decoded:
            candidates.extend([escaped, f"{escaped}/index.html"])
        joined = []
        for item in candidates:
            # URL 后缀是不可信输入，目录穿越守卫一道都不能省。
            safe = safe_join(root.as_posix(), *Path(item).parts)
            if safe is not None:
                joined.append(Path(safe))
        return joined

    def _replace_request(self, flow: HTTPFlow, entry: CompiledRewrite) -> None:
        """逐项覆盖 method / path / 头表 / 体；留空的栏保持原样。"""
        rule = entry.rule
        if rule.method.strip():
            flow.request.method = rule.method.strip()
        if rule.path.strip():
            flow.request.path = rule.path.strip()
        for name, value in rule.headers:
            flow.request.headers[name] = value
        if rule.replacement:
            # 写 content 而不是 text：体是字节，Content-Length 由 mitmproxy 重算。
            flow.request.content = read_replacement(rule.replacement)

    def _replace_response(self, flow: HTTPFlow, entry: CompiledRewrite) -> None:
        """整条作答：请求不出网，响应按状态码 / 头表 / 体拼装。"""
        rule = entry.rule
        status = (
            rule.status_code
            if rule.status_code is not None
            else REPLACE_RESPONSE_DEFAULT_STATUS
        )
        body = read_replacement(rule.replacement) if rule.replacement else b""
        headers = {name: value for name, value in rule.headers}
        flow.response = Response.make(status, body, headers)
        flow.metadata[REWRITE_ANSWERED_KEY] = "1"


__all__ = [
    "SUSPEND_LIMIT",
    "FerretRewriteAddon",
    "FerretTlsConfig",
    "GatewayL4Addon",
    "GatewayL7Addon",
    "GatewayState",
    "LogAddon",
]
