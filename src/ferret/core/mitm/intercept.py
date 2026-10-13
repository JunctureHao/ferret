"""Breakpoint (intercept) rule model, state and flow write-back.

断点本身是原生的：拦不拦由 `mitmproxy/addons/intercept.py` 的 ``Intercept`` addon
决定，它读 ``intercept``（一条 flowfilter 表达式）和 ``intercept_active``（开关），
命中就 ``flow.intercept()``。挂起/放行/丢弃/撤销也全是原生的 ``Flow.intercept`` /
``resume`` / ``kill`` / ``backup`` / ``revert``。

Ferret 自研的只有三件事：

1. 把用户那张规则列表编译成**一条** flowfilter 表达式（:func:`intercept_expression`）；
2. 给原生 addon 补一个拦截上限（:class:`InterceptState` + :class:`FerretIntercept`）——
   原生一个上限都没有，而每条拦截都钉住一个客户端连接；
3. 把界面编辑好的报文写回 flow（:func:`apply_request_edit` / :func:`apply_response_edit`），
   这一步必须在 mitm 线程上跑。

只用 QtCore 的 `QCoreApplication.translate`、不碰控件：校验与写回失败的消息会被
`apps/intercept` 原样显示出来，所以要过翻译目录（`from_dict` 那两条不译 —— 它们被
`rules_from_raw` 吞掉，从不上界面）。

**为什么不像网关那样自己判定**：网关必须自己编译正则，因为它的两个平面
（连接级 ``ignore_hosts`` 和 flow 级钩子）要逐字节同义，而原生 ``~d`` 不带端口、
和连接级的 ``host:port`` 对不上（见 `gateway.py` 的模块 docstring）。断点只有
flow 这一个平面，没有第二方需要对齐，原生 flowfilter 就是最合适的表达方式。
"""

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from PySide6.QtCore import QCoreApplication

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import (
    Flow,
    Headers,
    HTTPFlow,
    Intercept,
    Response,
    encoding,
    http_url,
    parse_filter,
    status_codes,
)
from ferret.core.mitm.edit_body import response_body_allowed
from ferret.core.mitm.filters import quote_regex
from ferret.core.mitm.held_edit import advance_edit_epoch

# 下发时恒写全量：规则删光也要把 ``intercept`` 写回 None，否则内核继续用上一条表达式。
# ``intercept_active`` **不在**这里 —— 原生 `Intercept.configure` 处理 "intercept"
# 变更时会自己把它设成 `bool(ctx.options.intercept)`，同一次 update 里再传一份只会
# 被覆盖掉（还会让「谁说了算」变得不可读）。开关一律通过 ``intercept=None`` 表达。
INTERCEPT_OPTIONS: tuple[str, ...] = ("intercept",)

# 拦截上限。理由和 `addons.py::SUSPEND_LIMIT` 逐字相同：一条拦截会把客户端 socket、
# `handle_connection` 任务和这次钩子派发一起钉住，`handle_hook` 在 `wait_for_resume()`
# 期间 `disarm()` 了看门狗，`tcp_timeout` 不会兜底 —— 泄漏是永久的，只能显式放行。
# 原生 `Intercept` 一个上限都没有（它面向 mitmproxy 自己的 TUI，人肉逐条放行），
# 所以这道闸门只能由 ferret 自己加。到顶就不再拦，宁可漏一条也不能把内核拖死。
INTERCEPT_LIMIT = 128


class InterceptPhase(StrEnum):
    """断点规则选在哪个阶段拦，也用来标注队列里一条流量停在哪。

    用途一（规则选阶段）：原生 `Intercept` 的 `request` 和 `response` 两个钩子
    共用同一个过滤器（`mitmproxy/addons/intercept.py` 的 `should_intercept`），
    阶段由表达式里的原生选择器决定 —— 不加阶段选择器（``BOTH``，默认）就是
    请求期、响应期各拦一次；带 ``~q``（``REQUEST``）只拦请求期，带 ``~s``
    （``RESPONSE``）只拦响应期。

    用途二（标注停在哪）：队列里一条已被拦下的流量此刻停在哪，只用
    ``REQUEST`` / ``RESPONSE``，判据是 ``flow.response is None`` —— 和原生
    ``~q``（Match request with no response）/ ``~s`` 同一套语义。
    """

    BOTH = "both"
    REQUEST = "request"
    RESPONSE = "response"


class InterceptField(StrEnum):
    """Which part of the traffic a rule matches against."""

    URL = "url"
    HOST = "host"
    METHOD = "method"


# 对应的原生选择器（`mitmproxy/flowfilter.py`）：
#   ~u → FUrl，  匹配 request.pretty_url（含 scheme / 端口 / 查询串）
#   ~d → FDomain，匹配 request.host 或 pretty_host，**都不含端口**
#   ~m → FMethod，匹配 request.data.method（bytes）
_FIELD_SELECTORS: dict[InterceptField, str] = {
    InterceptField.URL: "~u",
    InterceptField.HOST: "~d",
    InterceptField.METHOD: "~m",
}

# 规则阶段的对应选择器（BOTH 不带，两个钩子都拦）：
#   ~q → request with no response（请求期）
#   ~s → response present（响应期）
_PHASE_SELECTORS: dict[InterceptPhase, str] = {
    InterceptPhase.REQUEST: "~q",
    InterceptPhase.RESPONSE: "~s",
}

# 需要忽略大小写的字段。原生这三个选择器一个都没带 re.IGNORECASE，而主机名按 DNS
# 本就大小写无关、HTTP 方法习惯全大写却没人保证 —— 用户写 `get` 匹配不上 `GET`
# 属于纯粹的坑。URL 不在其列：路径是**区分**大小写的，而且要和重写页的
# `re.escape`（同样不忽略大小写）保持同一套语义。
_CASE_INSENSITIVE_FIELDS: frozenset[InterceptField] = frozenset(
    {InterceptField.HOST, InterceptField.METHOD}
)


class InterceptLogic(StrEnum):
    """How a rule value is turned into a regular expression."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


@dataclass(frozen=True, slots=True)
class InterceptRule:
    """A single user-authored breakpoint rule."""

    field: InterceptField = InterceptField.URL
    logic: InterceptLogic = InterceptLogic.CONTAINS
    value: str = ""
    enabled: bool = True
    phase: InterceptPhase = InterceptPhase.BOTH

    @property
    def pattern(self) -> str:
        r"""The regular expression source this rule matches with.

        和网关的 `GatewayRule.pattern` 有一处**故意的**不同：主机的 EQUALS 这里
        不补 ``(?::\d+)?``。网关要补是因为连接级拿到的主机名全是 ``host:port``
        形式；这里走的是原生 ``~d``，它匹配 ``request.host`` / ``pretty_host``，
        两个都不带端口 —— 补上反而永远匹配不到。
        """
        value = self.value.strip()
        if not value:
            raise ValueError(
                QCoreApplication.translate("InterceptRule", "匹配值不能为空")
            )
        if self.logic == InterceptLogic.REGEX:
            return value
        literal = re.escape(value)
        pattern = f"^{literal}$" if self.logic == InterceptLogic.EQUALS else literal
        if self.field in _CASE_INSENSITIVE_FIELDS:
            # 内联标志必须写在最前面（Python 3.11 起放中间是 error），这里恒在开头。
            return f"(?i){pattern}"
        return pattern

    @property
    def expression(self) -> str:
        """This rule as a **parenthesized** flowfilter sub-expression.

        阶段选择器不在这一层追加：flowfilter 的括号只包得住**一个** term，
        ``(~u "x" ~q)`` 直接是语法错误（实测）。带阶段的规则先出字段选择器，
        到 `intercept_expression` 按 phase 分组后，再用显式 ``&`` 挂 ``~q`` / ``~s``
        —— 并列（隐式 AND）的优先级比 ``|`` 低，挂选择器会把整串段都攥住，
        细节见那一头的 docstring。

        括号则是留给多条规则用 ``|`` 连起来的时候。flowfilter 的文法是
        ``OneOrMore(infixNotation(...))``，并列即隐式 AND，而 ``|`` 只是 infixNotation
        内部的一个中缀算子 —— 实测 ``~m "GET" ~u "x" | ~d "y"`` 解析出来是 **FAnd**
        （等于「既要 ~m GET，又要 (~u x 或 ~d y)」），和想表达的「或」毫不相干，
        而且**不报错**。眼下每条规则只剩一个选择器，省掉括号也还能解析对，但哪天
        规则多带一项就会静默变味 —— 这对括号是那时候的保险，不摘。
        """
        selector = _FIELD_SELECTORS[self.field]
        return f"({selector} {quote_regex(self.pattern)})"

    def validate(self) -> None:
        """Reject rules the native parser would not accept.

        Raises:
            ValueError: 匹配值为空，或正则不合法。

        必须由 ferret 自己先过一遍 `parse_filter`：`Intercept.configure` 里的
        `flowfilter.parse` 失败会包成 ``OptionsError`` 重抛，那会把**整批**规则
        连坐回滚（`optmanager.rollback`），错误信息也只剩一句原生英文。
        """
        expression = self.expression
        try:
            parse_filter(expression)
        except ValueError as exc:
            # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
            raise ValueError(
                QCoreApplication.translate("InterceptRule", "无效的匹配值：{}").format(
                    exc
                )
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": str(self.field),
            "logic": str(self.logic),
            "value": self.value,
            "enabled": self.enabled,
            "phase": str(self.phase),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "InterceptRule":
        """Rebuild a rule from persisted data; raises on anything unusable.

        老数据没有 ``"phase"`` 键时默认 ``BOTH``，与旧行为（两边都拦）一致，
        不需要迁移代码。
        """
        if not isinstance(raw, dict):
            raise TypeError("规则必须是对象")
        try:
            field = InterceptField(str(raw.get("field", InterceptField.URL)))
            logic = InterceptLogic(str(raw.get("logic", InterceptLogic.CONTAINS)))
            phase = InterceptPhase(str(raw.get("phase", InterceptPhase.BOTH)))
        except ValueError as exc:
            raise ValueError(f"未知的规则字段：{exc}") from exc
        return cls(
            field=field,
            logic=logic,
            value=str(raw.get("value", "")),
            enabled=bool(raw.get("enabled", True)),
            phase=phase,
        )


def _is_active(rule: InterceptRule) -> bool:
    return rule.enabled and bool(rule.value.strip())


def intercept_expression(rules: Iterable[InterceptRule]) -> str | None:
    """Compile the rules into one flowfilter expression; ``None`` 表示一条都不拦。

    按 phase 分组拼：同阶段的规则用 `` | `` 连成一段，带阶段的段用显式 ``&``
    挂 ``~q`` / ``~s``，各段之间再用 `` | `` 连接。**不能**用并列（隐式 AND）挂
    选择器：flowfilter 里并列的优先级比 ``|`` 低，``A | B ~q`` 解析成
    ``FAnd(FOr(A, B), ~q)`` —— 段还没 OR 起来就被选择器整个攥住，多个段时语义
    必错；``&`` 的优先级与 ``|`` 同层且更高，``A | B & ~q`` 才是
    ``FOr(A, FAnd(B, ~q))``。括号包不住两个 term（见 `InterceptRule.expression`），
    多条规则的段先括起来再交 ``&``。

    Raises:
        ValueError: 任何一条启用的规则不合法（编译在这里发生，运行期钩子不编译）。
    """
    groups: dict[InterceptPhase, list[str]] = {}
    for rule in rules:
        if not _is_active(rule):
            continue
        rule.validate()  # 单条先过一遍原生解析器，坏规则不会留到运行期
        groups.setdefault(rule.phase, []).append(rule.expression)
    if not groups:
        return None
    segments: list[str] = []
    combined = False
    for phase in InterceptPhase:
        terms = groups.get(phase)
        if not terms:
            continue
        segment = terms[0] if len(terms) == 1 else f"({' | '.join(terms)})"
        combined = combined or len(terms) > 1 or bool(segments)
        selector = _PHASE_SELECTORS.get(phase)
        segments.append(segment if selector is None else f"{segment} & {selector}")
    expression = " | ".join(segments)
    if combined:
        # 单条已经在 validate 里过了；多条拼起来还要再过一次，确认 `|` 连接本身
        # 没把语义拼歪（这一步同时兜住「括号丢了」这类拼串错误）。
        try:
            parse_filter(expression)
        except ValueError as exc:
            # 文案先取出来再 format：lupdate 不看 f-string 内部，写成
            # f"...{exc}" 就永远提取不到（见 AGENTS.md §7）。
            raise ValueError(
                QCoreApplication.translate(
                    "InterceptRule",
                    "断点规则无法合并：{}",
                ).format(exc)
            ) from exc
    return expression


def intercept_option_updates(
    rules: Iterable[InterceptRule], *, enabled: bool = True
) -> dict[str, Any]:
    """Translate rules into the ``options.update`` kwargs for the native addon.

    总开关关掉就下发 ``intercept=None``：原生 `configure` 见到空值会把
    ``intercept_active`` 一并清成 False，比自己去写那个开关更可靠。
    """
    expression = intercept_expression(rules) if enabled else None
    return {"intercept": expression}


def intercept_rules_from_config(raw: Any) -> list[InterceptRule]:
    """Read rules back from persisted config, dropping entries we cannot parse."""
    if not isinstance(raw, list):
        return []
    rules: list[InterceptRule] = []
    for item in raw:
        try:
            rules.append(InterceptRule.from_dict(item))
        except (TypeError, ValueError):
            continue
    return rules


def intercept_rules_to_config(rules: Iterable[InterceptRule]) -> list[dict[str, Any]]:
    """Serialize rules for persistence."""
    return [rule.to_dict() for rule in rules]


class InterceptState:
    """The breakpoint plane's mutable state: which flows are held, and how many.

    只在 mitm 线程上读写 —— 钩子本来就跑在 mitm 线程，放行/丢弃经
    `MitmRuntime.call` 也会被 marshal 到同一个 loop 上，所以这里不需要锁
    （和 `GatewayState` 同一个约定）。
    """

    def __init__(self) -> None:
        self._log = get_logger("mitmproxy")
        self._held: dict[str, Flow] = {}
        # 拦截发生在 `request` / `response` 钩子里，而 `View` 只在 `requestheaders`
        # / `response` / `error` 上更新行 —— 请求期拦截不主动通知，流量表那一行就
        # 不会重绘、「已拦截」标记永远不上屏。由 runtime 接成 Qt 信号。
        self.on_intercept_changed: Callable[[Flow], None] | None = None

    @property
    def held_count(self) -> int:
        return len(self._held)

    def held_ids(self) -> tuple[str, ...]:
        return tuple(self._held)

    def arm(self, flow: Flow) -> bool:
        """Hold a flow at a breakpoint; ``False`` 表示没拦（已被拦住或到顶了）。"""
        if flow.id in self._held:
            return True
        if flow.intercepted:
            # 网关的「挂起」策略已经拦住它了。两边各记一本账，放行时就会出现
            # 「一边放了、另一边还以为攥着」的错帐；这条流量交给网关那边放行，
            # 断点页照样能看见它（队列是扫 `flow.intercepted` 得出的，不是扫这本账）。
            return False
        if len(self._held) >= INTERCEPT_LIMIT:
            self._log.warning(
                "断点拦截数已达上限 %d，本条流量照常放行: %s",
                INTERCEPT_LIMIT,
                _describe(flow),
            )
            return False
        # 只能同步 intercept()，绝不能在钩子里 await：`handle_hook` 在
        # `handle_lifecycle(hook)` 之后本来就会 `await wait_for_resume()`，addon
        # 自己 await 只会饿死链上后面的 addon（和 `GatewayState.suspend` 同理）。
        advance_edit_epoch(flow)
        flow.intercept()
        self._held[flow.id] = flow
        self._notify(flow)
        return True

    def release(self, flow_ids: Iterable[str], *, kill: bool = False) -> int:
        """Let the named held flows go; 不在账上的 id 直接忽略。"""
        released = 0
        for flow_id in flow_ids:
            flow = self._held.pop(flow_id, None)
            if flow is None:
                continue
            self._release(flow, kill=kill)
            released += 1
        return released

    def release_all(self, *, kill: bool = False) -> int:
        """Let every held flow go; ``kill`` 时顺手断连（停机路径用）。"""
        pending = list(self._held.values())
        self._held.clear()
        for flow in pending:
            self._release(flow, kill=kill)
        return len(pending)

    def forget(self, flow_ids: Iterable[str]) -> int:
        """Drop ids from the ledger **without** resuming them.

        给「这条 flow 已经不在 View 里了」的路径用：行都删了，放行谁都看不见，
        但账不能一直挂着占着上限的名额。
        """
        removed = 0
        for flow_id in flow_ids:
            flow = self._held.pop(flow_id, None)
            if flow is not None:
                advance_edit_epoch(flow)
                removed += 1
        return removed

    def _release(self, flow: Flow, *, kill: bool) -> None:
        # 顺序不能反：`kill()` 会把 `intercepted` 清成 False，而 `resume()` 开头就是
        # `if not self.intercepted: return` —— 先 kill 再 resume 会让这条 flow 永久
        # 挂死在 `wait_for_resume()` 上。`kill()` 还要先看 `killable`，否则抛
        # `ControlException`。（与 `GatewayState._release` 完全同一个坑。）
        advance_edit_epoch(flow)
        flow.resume()
        if kill and flow.killable:
            flow.kill()
        self._notify(flow)

    def _notify(self, flow: Flow) -> None:
        callback = self.on_intercept_changed
        if callback is not None:
            callback(flow)


def _describe(flow: Flow) -> str:
    request = getattr(flow, "request", None)
    return request.pretty_url if request is not None else flow.id


class FerretIntercept(Intercept):
    """原生 ``Intercept``，只把「真的去拦」这一步换成走 :class:`InterceptState`。

    继承而不是替写：命中判定（``should_intercept``：读 ``intercept_active`` +
    编译好的 filter + 排除重放）一个字都不改，只在动手拦的那一刻插入上限闸门和
    记账。这也是 `FerretTlsConfig` 用的同一种手法。
    """

    def __init__(self, state: InterceptState) -> None:
        # 原生 Intercept 没有 __init__（`filt` 是类属性），所以没有 super() 可调。
        self.state = state

    def process_flow(self, f: Flow) -> None:
        if self.should_intercept(f):
            self.state.arm(f)

    def done(self) -> None:
        """Master 要停了：账上挂着的断点全部放行并断连（#43）。

        与 ``GatewayL7Addon.done`` 同一个坑、同一个修法：跨代共用 View 时不清账
        会留 intercepted 死行，被拦流的 ``wait_for_resume`` 任务随旧 loop 一起消亡
        —— 放行 + kill 的顺序约束见 ``InterceptState._release``。原生 ``Intercept``
        没有 done，这份清账职责在我们这边。
        """
        self.state.release_all(kill=True)


# —— 界面 → flow 的写回 ——


@dataclass(frozen=True, slots=True)
class RequestEdit:
    """A request as edited in the UI: plain data only, safe to hand across threads.

    界面在 Qt 线程上装配它，`MitmRuntime.call` 把它 marshal 到 mitm 线程再落到
    flow 上 —— 中间不夹带任何 flow / Master 引用，所以不碰「Qt 线程不许读写 flow」
    那条红线。头用**有序键值对序列**而不是 dict：重复的 ``Cookie`` / ``Set-Cookie``
    完全合法，dict 会把重名的挤掉。

    ``content`` 为 ``None`` 表示「没有替换正文」：写回时线上原字节整段保留，通用
    setter 会按体长重算 Content-Length，对 HEAD 这类请求是不存在的语义（#89），
    对二进制体还省一次解压→回压的往返。**例外**：编码头（Content-Encoding）被改过
    时，核内会把旧正文严格解码后按新声明重编码 —— 转换失败整次拒绝，见
    :func:`apply_request_edit`。
    """

    method: str
    url: str
    headers: Sequence[tuple[str, str]]
    content: bytes | None


@dataclass(frozen=True, slots=True)
class ResponseEdit:
    """A response as edited in the UI (see :class:`RequestEdit`).

    ``content=None`` 对无正文响应（HEAD/204/304）尤其要紧：这些响应的
    ``Content-Length`` 由源站赋予长度语义，`response.content = b""` 会把它清成
    0，原样跳过才是「未编辑保真」（#89）。例外与 :class:`RequestEdit` 相同：
    只改 Content-Encoding 时核内按新声明转换旧正文。

    此外协议规则会覆盖正文意图：最终状态码/请求方法禁止正文时（HEAD 响应、1xx、
    204、205、304、成功 CONNECT），非空替换被拒绝、既有正文被清空、framing 与
    trailers 按 :func:`apply_response_edit` 的规则重整 —— 无正文响应的专用分支。
    """

    status_code: int
    headers: Sequence[tuple[str, str]]
    content: bytes | None
    reason: str = ""


def _build_headers(items: Sequence[tuple[str, str]]) -> Headers:
    """Turn UI key/value pairs into native ``Headers``.

    ``Headers`` 只收 bytes，且用 ``surrogateescape`` 的 UTF-8 —— 和原生
    `Response.make` 对 dict 的处理保持一致，非 ASCII 头值不至于在这里炸掉。
    """
    fields: list[tuple[bytes, bytes]] = []
    for name, value in items:
        key = name.strip()
        if not key:
            continue
        fields.append(
            (
                key.encode("utf-8", "surrogateescape"),
                value.encode("utf-8", "surrogateescape"),
            )
        )
    return Headers(fields)


def _checked_status(status_code: int) -> int:
    """Validate a UI-supplied status code before anything is written to the flow."""
    status_code = int(status_code)
    if not 100 <= status_code <= 599:
        raise ValueError(
            QCoreApplication.translate(
                "InterceptEdit", "无效的 HTTP 状态码：{}"
            ).format(status_code)
        )
    return status_code


def build_request_edit(flow: HTTPFlow) -> RequestEdit:
    """把活 flow 的请求压成 :class:`RequestEdit`，给 compose 页灌表单用。

    与 `apply_request_edit` 互为逆操作，和上游 `export.cleanup_request` 同一套
    处理：在**副本**上 decode（去 Content-Encoding，decode 自动摘头），绝不碰活
    流量 —— `request.copy()` 换的是 message 不是 flow，没有 `_snapshot` 那个 id
    被换掉的坑。`Host` 头保留：`build_compose_flow` 尊重显式 Host，经 MapRemote
    改过路由的流量语义不丢；`content-length` 摘掉，compose 发送时原生自动重算。
    """
    request = flow.request.copy()
    request.decode(strict=False)
    request.headers.pop("content-length", None)
    return RequestEdit(
        method=request.method,
        # pretty_url 与列表/导出同口径：客户端自解析 DNS 时 CONNECT 目标是 IP，
        # request.url 会拿到裸 IP，而 SNI 里的真实域名只在 pretty_host 上。
        url=request.pretty_url,
        # 保重复头（Cookie 等）：字典会吃掉同名头，见 `_load_message`。
        headers=list(request.headers.items(multi=True)),
        content=request.get_content(strict=False) or b"",
    )


def _normalized_ce(value: str | None) -> str:
    """编码头比较口径：大小写不敏感，缺省与显式 ``identity`` 等价。"""
    return (value or "").strip().lower() or "identity"


def _require_encodable(content: bytes, ce: str | None) -> None:
    """Pre-check the native content setter's encoding step.

    原生 ``set_content`` 对不支持的 Content-Encoding 会**静默删头**并把原字节
    存回去 —— 那是数据错乱不是成功，所以任何需要编码的路径都先在这里显式
    抛翻译后的错误，绝不静默回退。
    """
    try:
        encoding.encode(content, ce or "identity")
    except ValueError as exc:
        raise ValueError(
            QCoreApplication.translate(
                "InterceptEdit",
                "正文无法按新的 Content-Encoding（{}）编码，请改用受支持的编码或删除该头。",
            ).format(ce)
        ) from exc


def _apply_candidate_body(
    message: Any, headers: Headers, content: bytes | None
) -> None:
    """正文与编码头的全部校验/构建，跑在候选副本上（不 backup、不上 flow）。

    ``content=None`` 是「未替换正文」而非「跳过一切」：只改编码头时旧正文必须
    严格解码并按新声明重编码（设计 `.plans/intercept-body-kinds.md` §6）。任何
    失败都在这里抛翻译后的 ``ValueError``，调用方保证此时还没有 ``backup()``。
    """
    old_ce = message.headers.get("content-encoding")
    new_ce = headers.get("content-encoding")
    ce_changed = _normalized_ce(old_ce) != _normalized_ce(new_ce)
    if content is None and not ce_changed:
        # 未编辑保真（#89）：线上原字节与长度语义原样保留，只落头部编辑。
        message.headers = headers
        return
    if content is None:
        raw = message.raw_content
        if raw is None:
            # 缺失不是空：没有字节可供转换，拒绝把缺失当 b""。
            raise ValueError(
                QCoreApplication.translate(
                    "InterceptEdit",
                    "正文缺失，无法只改 Content-Encoding；请明确替换正文，或还原编码头。",
                )
            )
        if not raw:
            # 原生 decode() 对空体是 no-op；凭空造一个压缩空正文没有意义。
            if _normalized_ce(new_ce) != "identity":
                raise ValueError(
                    QCoreApplication.translate(
                        "InterceptEdit",
                        "原正文为空，不能生成压缩后的空正文；请替换正文，或删除新的 Content-Encoding。",
                    )
                )
            message.headers = headers
            return
        try:
            decoded = message.get_content(strict=True)
        except ValueError as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "InterceptEdit",
                    "原正文无法按当前 Content-Encoding 解码，不能只改编码头；"
                    "请明确替换正文，或还原编码头。",
                )
            ) from exc
        _require_encodable(decoded, new_ce)
        message.headers = headers
        message.set_content(decoded)
        return
    # 显式替换：草稿字节视为**已解码正文**，先按新声明预检再交原生 setter
    # 重算 Content-Length（手写长度是明令禁止的）。
    _require_encodable(content, new_ce)
    message.headers = headers
    message.set_content(content)


def _apply_forbidden_response_body(
    candidate: Any,
    headers: Headers,
    content: bytes | None,
    method: str,
    status_code: int,
    *,
    normalize: bool,
) -> None:
    """最终状态禁止正文的专用分支（设计 §7）。

    清空走 ``raw_content`` 直写而不是 content setter —— 带 gzip 头的
    ``content=b""`` 会生成非空 gzip member。HEAD/304 保留表示元数据
    （原 Content-Length 不动），1xx/204/成功 CONNECT 删除 framing 头而不生成
    长度值，205 由原生空体构建保证零长 framing。状态未动且未替换正文时原样
    放行，不借类型化面板静默修复畸形报文。
    """
    candidate.headers = headers
    if not normalize:
        return
    method = method.strip().upper()
    if status_code == 205:
        # 先摘编码头再走原生空体构建：无 TE 时原生落 content-length: 0，
        # 有 TE 时保持 chunked —— 两条路都不会出现 gzip member。
        candidate.headers.pop("content-encoding", None)
        candidate.set_content(b"")
    elif (
        100 <= status_code < 200
        or status_code == 204
        or (method == "CONNECT" and 200 <= status_code < 300)
    ):
        # 协议禁止 framing：删掉既有值，不生成长度值；缺失（None）保持缺失。
        candidate.headers.pop("content-length", None)
        candidate.headers.pop("transfer-encoding", None)
        if candidate.raw_content is not None:
            candidate.raw_content = b""
    else:
        # HEAD/304：Content-Length 描述的是「对应表示」，清字节不动元数据。
        if candidate.raw_content is not None:
            candidate.raw_content = b""
    candidate.trailers = None


def _rebuild_response_after_forbidden(
    candidate: Any, headers: Headers, content: bytes | None
) -> None:
    """禁止→允许（如 304→200）的正文重建。

    不能带着原表示长度发空体：原体为空时按空正文走原生 setter 重算长度；
    原体缺失（None）不是空，必须由用户明确替换。
    """
    if content is not None:
        _apply_candidate_body(candidate, headers, content)
        return
    raw = candidate.raw_content
    if raw is None:
        raise ValueError(
            QCoreApplication.translate(
                "InterceptEdit",
                "原响应正文缺失，切换到允许正文的状态后需要明确替换正文。",
            )
        )
    if not raw:
        new_ce = headers.get("content-encoding")
        if _normalized_ce(new_ce) != "identity":
            raise ValueError(
                QCoreApplication.translate(
                    "InterceptEdit",
                    "原正文为空，不能生成压缩后的空正文；请替换正文，或删除新的 Content-Encoding。",
                )
            )
        candidate.headers = headers
        candidate.set_content(b"")
        return
    _apply_candidate_body(candidate, headers, None)


def apply_request_edit(flow: HTTPFlow, edit: RequestEdit) -> None:
    """Write an edited request back onto a held flow. **Runs on the mitm thread.**

    全部校验与正文构建先在 ``flow.request`` 的**副本**上完成，成功后才
    ``backup()`` 并逐字段提交 —— 一次失败的保存既不留「已编辑」痕迹，也不留
    半套改动。正文/编码头语义见 :func:`_apply_candidate_body`。

    Raises:
        ValueError: 方法或 URL 为空、URL 不合法，或正文/编码头转换失败。
    """
    request = flow.request
    if request is None:
        raise ValueError(
            QCoreApplication.translate("InterceptEdit", "这条流量没有请求可改")
        )
    # `request.url` 的 setter 就是 `url.parse` 加四个字段赋值，先解一次等价于预检，
    # 把「URL 不合法」挡在动手之前。
    method = edit.method.strip().upper()
    if not method:
        raise ValueError(
            QCoreApplication.translate("InterceptEdit", "请求方法不能为空")
        )
    url = edit.url.strip()
    if not url:
        raise ValueError(
            QCoreApplication.translate("InterceptEdit", "请求 URL 不能为空")
        )
    http_url.parse(url)
    headers = _build_headers(edit.headers)
    candidate = request.copy()
    _apply_candidate_body(candidate, headers, edit.content)
    # 备份放在校验之后，原生 `Flow.revert()` 才有东西可回滚（`backup()` 已有备份时是
    # 空操作，所以反复编辑只会记下**最初**那份 —— 撤销就该撤到没动过的样子）。
    flow.backup()
    request.method = method
    request.url = url
    # 头与体都取自候选：content-length 已由原生 setter 在副本上算好；
    # raw_content 直写避免把候选字节再解压→回压一遍。
    request.headers = candidate.headers
    request.raw_content = candidate.raw_content


def apply_response_edit(flow: HTTPFlow, edit: ResponseEdit) -> None:
    """Write an edited response back onto a held flow. **Runs on the mitm thread.**

    原子性与 :func:`apply_request_edit` 相同；响应额外受协议约束 —— 以**最终**
    状态码和源请求方法判 :func:`response_body_allowed`，任何绕过 UI 的非空替换
    都在这里被拒绝。

    Raises:
        ValueError: 状态码不合法、正文替换与协议规则冲突，或编码头转换失败。
    """
    response = flow.response
    if response is None:
        raise ValueError(
            QCoreApplication.translate("InterceptEdit", "这条流量还没有响应可改")
        )
    status_code = _checked_status(edit.status_code)
    headers = _build_headers(edit.headers)
    candidate = response.copy()
    method = flow.request.method
    status_changed = status_code != candidate.status_code
    if response_body_allowed(method, status_code):
        if not response_body_allowed(method, candidate.status_code):
            _rebuild_response_after_forbidden(candidate, headers, edit.content)
        else:
            _apply_candidate_body(candidate, headers, edit.content)
        # 状态转换或明确替换后，原 chunked trailers 不再可发送；未动正文与
        # 状态时原样保留（畸形的原报文不静默修复）。
        if status_changed or edit.content is not None:
            candidate.trailers = None
    else:
        if edit.content is not None and edit.content:
            raise ValueError(
                QCoreApplication.translate(
                    "InterceptEdit",
                    "该响应受请求方法或状态码限制不能携带正文，请清空正文后再放行。",
                )
            )
        _apply_forbidden_response_body(
            candidate,
            headers,
            edit.content,
            method,
            status_code,
            normalize=status_changed or edit.content is not None,
        )
    # 同 `apply_request_edit`：校验全部先于 `backup()`，失败的保存不留「已编辑」痕迹。
    flow.backup()
    response.status_code = status_code
    response.reason = edit.reason or status_codes.RESPONSES.get(status_code, "")
    response.headers = candidate.headers
    response.raw_content = candidate.raw_content
    response.trailers = candidate.trailers


def fake_response(flow: HTTPFlow, edit: ResponseEdit) -> None:
    """Answer a request-phase breakpoint locally, without contacting the server.

    **Runs on the mitm thread.** 手法就是原生的「给 flow 挂一条 response」——
    `proxy/layers/http/__init__.py` 在请求钩子返回后先看 ``flow.response``，非空
    就直接回客户端、根本不拨号（原生 ``maplocal`` / ``BlockList`` 用的都是这招）。

    Raises:
        ValueError: 这条流量已经有响应了（那时候该走 :func:`apply_response_edit`）。
    """
    if flow.response is not None:
        raise ValueError(
            QCoreApplication.translate(
                "InterceptEdit",
                "这条流量已经有响应，请直接编辑响应",
            )
        )
    status_code = _checked_status(edit.status_code)
    headers = _build_headers(edit.headers)
    flow.backup()
    # `Response.make` 会补 reason 和 content-length，比自己拼 Response 稳。
    # 这里没有「未编辑」可言：None 就是空体，与改动前的 b"" 同一产物。
    content = edit.content if edit.content is not None else b""
    response = Response.make(status_code, content, headers)
    if edit.reason:
        response.reason = edit.reason
    flow.response = response
