"""Gateway rule model: nine L4/L7 traffic policies over mitmproxy's natives.

Ferret 自研的部分只有「判定」：把用户规则编译成正则，再算出命中哪条策略。
真正的动作全是原生的 —— 连接级绕行/仅允许是 ``ignore_hosts`` / ``allow_hosts``
（`mitmproxy/addons/next_layer.py`），拦截是 ``Response.make`` / ``Flow.kill``，
挂起是 ``Flow.intercept``。这里没有 Qt，也没有任何 UI 文案。

**匹配为什么不用 flowfilter**：``~d``（``FDomain``）匹配的是 ``request.host`` /
``request.pretty_host``，**不带端口**；而连接级的 ``next_layer._ignore_connection``
匹配的每一项都是 ``f"{host}:{port}"`` 形式（peername / address / Host 头 / SNI 全部
补过端口，没有一个是裸主机名）。两者拿同一条 pattern 会对「带端口的主机规则」给出
不同答案。网关的两个平面必须**逐字节同义**，所以两边共用本模块编译出的同一个
``re.Pattern``，按 ``host:port`` 去 ``search`` —— 这也正是 mitmproxy 自己在
`next_layer.py:245-261` 做的事（裸 ``re.search`` + ``re.IGNORECASE``），不是另造轮子。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ferret.core.mitm.bindings import status_codes
from ferret.core.mitm.blocklist import BlockField
from ferret.core.mitm.blocklist import rules_from_config as block_rules_from_config

# 444：沿用原生 BlockList 的约定，见到它走 flow.kill() 断连而不是回一个空响应。
GATEWAY_STATUS_CLOSE: int = status_codes.NO_RESPONSE
GATEWAY_STATUS_DEFAULT: int = 403

# 下发时恒写全量：规则删光也要把选项写回空列表，否则内核继续用上一批 pattern。
GATEWAY_OPTIONS: tuple[str, ...] = ("allow_hosts", "ignore_hosts")

# 网关动过的 flow 会在 ``flow.metadata`` 上留下策略名（原生 BlockList 也是这么标的，
# 见 `addons/blocklist.py:75`），供流量表标出「已屏蔽」/「挂起中」。
# ``metadata`` 会随 flow 一起存档（`flow.py:154`），所以只放字符串这类可序列化的值。
GATEWAY_METADATA_KEY: str = "gateway"


class GatewayLayer(StrEnum):
    """Which plane a rule acts on."""

    L4 = "l4"
    L7 = "l7"


class GatewayPolicy(StrEnum):
    """What a matching rule does."""

    ALLOW_ONLY = "allow_only"
    BYPASS = "bypass"
    BLOCK = "block"  # 仅 L4：连接到不了服务器
    BLOCK_OUT = "block_out"  # 仅 L7：拦住发往服务器的请求
    BLOCK_IN = "block_in"  # 仅 L7：拦住发往客户端的响应
    SUSPEND_OUT = "suspend_out"  # 仅 L7：请求期挂住
    SUSPEND_IN = "suspend_in"  # 仅 L7：响应期挂住


class GatewayField(StrEnum):
    """Which part of the traffic a rule matches against.

    只有主机和方法两种 —— 主机是连接级 pattern 和 flow 级判定**唯一都能看见**的
    subject，所以两个平面不可能分歧；方法只有 L7 有。
    """

    HOST = "host"
    METHOD = "method"


class GatewayLogic(StrEnum):
    """How a rule value is turned into a regular expression."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


# 每层允许的策略：L4 三种、L7 六种，合起来就是界面上的九宫格。
LAYER_POLICIES: dict[GatewayLayer, tuple[GatewayPolicy, ...]] = {
    GatewayLayer.L4: (
        GatewayPolicy.ALLOW_ONLY,
        GatewayPolicy.BYPASS,
        GatewayPolicy.BLOCK,
    ),
    GatewayLayer.L7: (
        GatewayPolicy.ALLOW_ONLY,
        GatewayPolicy.BYPASS,
        GatewayPolicy.BLOCK_OUT,
        GatewayPolicy.BLOCK_IN,
        GatewayPolicy.SUSPEND_OUT,
        GatewayPolicy.SUSPEND_IN,
    ),
}

# 冲突优先级：仅允许 > 绕行 > 屏蔽 > 挂起（Reqable 文档 3.2.23）。数值小的先赢，
# 同值按规则列表自上而下。这是让文档里「自上而下匹配」和「冲突优先级」两句话同时
# 成立的唯一读法，也是两个平面不可能分歧的唯一读法（原生匹配没有「行序」概念）。
_POLICY_PRIORITY: dict[GatewayPolicy, int] = {
    GatewayPolicy.ALLOW_ONLY: 0,
    GatewayPolicy.BYPASS: 1,
    GatewayPolicy.BLOCK: 2,
    GatewayPolicy.BLOCK_OUT: 2,
    GatewayPolicy.BLOCK_IN: 2,
    GatewayPolicy.SUSPEND_OUT: 3,
    GatewayPolicy.SUSPEND_IN: 3,
}

# 只有屏蔽（出）会把状态码回给客户端；其余策略的 status_code 无意义。
_NEEDS_STATUS: frozenset[GatewayPolicy] = frozenset({GatewayPolicy.BLOCK_OUT})

# 挂起类策略：命中后同步 intercept()，直到被显式放行。
SUSPEND_POLICIES: frozenset[GatewayPolicy] = frozenset(
    {GatewayPolicy.SUSPEND_OUT, GatewayPolicy.SUSPEND_IN}
)


@dataclass(frozen=True, slots=True)
class GatewayRule:
    """A single user-authored gateway rule."""

    layer: GatewayLayer = GatewayLayer.L7
    policy: GatewayPolicy = GatewayPolicy.BYPASS
    field: GatewayField = GatewayField.HOST
    logic: GatewayLogic = GatewayLogic.CONTAINS
    value: str = ""
    status_code: int = GATEWAY_STATUS_DEFAULT
    enabled: bool = True

    @property
    def pattern(self) -> str:
        r"""The regular expression source this rule matches with.

        主机的 EQUALS **必须**补 ``(?::\d+)?``：连接级能拿到的主机名全是
        ``host:port`` 形式（`next_layer.py:220-241` 每一项都补过端口），
        ``^example\.com$`` 一条都匹配不上。
        """
        value = self.value.strip()
        if not value:
            raise ValueError("匹配值不能为空")
        if self.logic == GatewayLogic.REGEX:
            return value
        literal = re.escape(value)
        if self.logic == GatewayLogic.EQUALS:
            if self.field == GatewayField.HOST:
                return rf"^{literal}(?::\d+)?$"
            return f"^{literal}$"
        return literal

    def compile(self) -> re.Pattern[str]:
        """Compile this rule's pattern, mirroring mitmproxy's own host matching.

        Raises:
            ValueError: 匹配值为空，或正则编译不过。

        必须由 ferret 自己先编一遍：`NextLayer.configure` 也会 ``re.compile`` 这些
        pattern，但它抛的 ``re.error`` 被 `addonmanager.safecall` 记日志吞掉了
        （只有 ``AddonHalt`` / ``OptionsError`` 会重抛），结果是**选项值已经更新、
        编译后的列表还是旧的** —— 界面显示生效、内核其实没生效。
        """
        try:
            return re.compile(self.pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"无效的正则表达式：{exc}") from exc

    def validate(self) -> None:
        """Reject rules the two planes could not honour.

        Raises:
            ValueError: 层与策略不匹配、L4 用了方法匹配、状态码越界或正则不合法。
        """
        if self.policy not in LAYER_POLICIES[self.layer]:
            raise ValueError(f"{self.layer} 不支持策略 {self.policy}")
        if self.layer == GatewayLayer.L4 and self.field != GatewayField.HOST:
            # 连接还没有 HTTP 语义，拿不到方法。
            raise ValueError("传输层规则只能按主机匹配")
        if self.policy in _NEEDS_STATUS and not 100 <= self.status_code <= 599:
            raise ValueError(f"无效的 HTTP 状态码：{self.status_code}")
        self.compile()

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": str(self.layer),
            "policy": str(self.policy),
            "field": str(self.field),
            "logic": str(self.logic),
            "value": self.value,
            "status_code": self.status_code,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "GatewayRule":
        """Rebuild a rule from persisted data; raises on anything unusable."""
        if not isinstance(raw, dict):
            raise TypeError("规则必须是对象")
        try:
            layer = GatewayLayer(str(raw.get("layer", GatewayLayer.L7)))
            policy = GatewayPolicy(str(raw.get("policy", GatewayPolicy.BYPASS)))
            field = GatewayField(str(raw.get("field", GatewayField.HOST)))
            logic = GatewayLogic(str(raw.get("logic", GatewayLogic.CONTAINS)))
        except ValueError as exc:
            raise ValueError(f"未知的规则字段：{exc}") from exc
        try:
            status_code = int(raw.get("status_code", GATEWAY_STATUS_DEFAULT))
        except (TypeError, ValueError) as exc:
            raise ValueError("无效的 HTTP 状态码") from exc
        return cls(
            layer=layer,
            policy=policy,
            field=field,
            logic=logic,
            value=str(raw.get("value", "")),
            status_code=status_code,
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass(frozen=True, slots=True)
class GatewayDecision:
    """The winning policy for one piece of traffic."""

    policy: GatewayPolicy
    status_code: int = GATEWAY_STATUS_DEFAULT


@dataclass(frozen=True, slots=True)
class _Compiled:
    """One active rule plus its compiled matcher."""

    rule: GatewayRule
    matcher: re.Pattern[str]

    def matches(self, host: str, port: int, method: str | None) -> bool:
        if self.rule.field == GatewayField.METHOD:
            if method is None:
                return False
            return bool(self.matcher.search(method))
        # 和 next_layer 一样按 host:port 匹配（见模块 docstring）。
        return bool(self.matcher.search(f"{host}:{port}"))

    def undecidable(self, method: str | None) -> bool:
        """连接级（还没有方法）时这条规则判不了。"""
        return self.rule.field == GatewayField.METHOD and method is None


def _active(rule: GatewayRule) -> bool:
    return rule.enabled and bool(rule.value.strip())


class GatewayRuleSet:
    """A pre-compiled, immutable snapshot of the rules.

    只在下发时构造一次，之后每条连接 / 每条 flow 只读它 —— 不在钩子里编译正则。

    Raises:
        ValueError: 任何一条启用的规则不合法（构造即校验，坏规则不会留到运行期）。
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: Iterable[GatewayRule] = ()) -> None:
        compiled: list[_Compiled] = []
        for rule in rules:
            if not _active(rule):
                continue
            rule.validate()
            compiled.append(_Compiled(rule, rule.compile()))
        self._rules: tuple[_Compiled, ...] = tuple(compiled)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def decide(
        self, host: str, port: int, method: str | None = None
    ) -> GatewayDecision | None:
        """Resolve the policy for one piece of traffic; ``None`` 表示正常抓取。

        ``method=None`` 表示连接级判定（还没有 HTTP 语义）：此时按方法匹配的规则
        整条**跳过**，连「存在仅允许规则」都不算 —— 否则一条按方法的仅允许规则会把
        所有连接都判成绕行，而方法只有抓下来才知道，等于自己把自己锁死。
        """
        if not self._rules:
            return None

        allow_only_present = False
        allow_only_hit = False
        best: _Compiled | None = None
        best_priority = len(_POLICY_PRIORITY) + 1

        for item in self._rules:
            if item.undecidable(method):
                continue
            policy = item.rule.policy
            hit = item.matches(host, port, method)
            if policy == GatewayPolicy.ALLOW_ONLY:
                allow_only_present = True
                allow_only_hit = allow_only_hit or hit
                continue
            if not hit:
                continue
            priority = _POLICY_PRIORITY[policy]
            # 严格小于：同优先级保留列表里靠前的那条。
            if priority < best_priority:
                best, best_priority = item, priority

        if allow_only_present:
            # 仅允许是白名单语义：命中的正常抓取（且优先级最高，压过一切），没命中的
            # 走**绕行** —— 不是丢弃，Reqable 文档明确写了这一点。
            if allow_only_hit:
                return None
            return GatewayDecision(GatewayPolicy.BYPASS)

        if best is None:
            return None
        return GatewayDecision(best.rule.policy, best.rule.status_code)


def gateway_option_updates(
    rules: Iterable[GatewayRule],
    *,
    enabled: bool = True,
) -> dict[str, list[str]]:
    r"""Translate L4 rules into the native ``options.update`` kwargs.

    只有 L4 规则会落到原生选项上（L7 要看连接级拿不到的 HTTP 语义，统一交给钩子
    平面）。两个键恒出现，所以一次 ``options.update(**updates)`` 既能下发新规则、
    也能清掉被删掉的老规则。

    **只要有启用的 L4 仅允许规则，``ignore_hosts`` 就必须是空列表。**
    原生 `next_layer.py:245-261` 先查 ``allow_hosts`` 再查 ``ignore_hosts``，两者都
    命中时结果是「忽略」，即 ``绕行 > 仅允许`` —— 和要求的优先级正好相反。而
    ``allow_hosts`` 自己就已经把「未命中的一律绕行」做掉了，所以清空 ``ignore_hosts``
    得到的正是要求的语义：命中仅允许→抓，只命中绕行→不在允许名单里→绕行，
    两者都不命中→绕行。

    Raises:
        ValueError: 任何一条启用的 L4 规则不合法。
    """
    allow: list[str] = []
    ignore: list[str] = []
    if enabled:
        for rule in rules:
            if not _active(rule) or rule.layer != GatewayLayer.L4:
                continue
            rule.validate()
            if rule.policy == GatewayPolicy.ALLOW_ONLY:
                allow.append(rule.pattern)
            elif rule.policy == GatewayPolicy.BYPASS:
                ignore.append(rule.pattern)
    return {"allow_hosts": allow, "ignore_hosts": [] if allow else ignore}


def gateway_rules_from_config(raw: Any) -> list[GatewayRule]:
    """Read rules back from persisted config, dropping entries we cannot parse."""
    if not isinstance(raw, list):
        return []
    rules: list[GatewayRule] = []
    for item in raw:
        try:
            rules.append(GatewayRule.from_dict(item))
        except (TypeError, ValueError):
            continue
    return rules


def gateway_rules_to_config(rules: Iterable[GatewayRule]) -> list[dict[str, Any]]:
    """Serialize rules for persistence."""
    return [rule.to_dict() for rule in rules]


# BlockField.URL 故意不在表里：网关不做 URI 匹配，老规则只能丢弃。
_BLOCK_FIELD_MAP: dict[BlockField, GatewayField] = {
    BlockField.HOST: GatewayField.HOST,
    BlockField.METHOD: GatewayField.METHOD,
}


def gateway_rules_from_block_config(raw: Any) -> tuple[list[GatewayRule], int]:
    """Migrate legacy ``Proxy.BlockList`` entries into L7 屏蔽（出）rules.

    一次性迁移，只在配置里还没有网关规则时跑。返回 ``(规则, 丢弃条数)``：
    ``BlockField.URL`` 的老规则**没有等价物**（网关的匹配对象不含 URI），只能丢弃，
    由调用方记一条 warning —— 悄悄消失比丢弃更糟。
    """
    rules: list[GatewayRule] = []
    dropped = 0
    for old in block_rules_from_config(raw):
        field = _BLOCK_FIELD_MAP.get(old.field)
        if field is None:
            dropped += 1
            continue
        rules.append(
            GatewayRule(
                layer=GatewayLayer.L7,
                policy=GatewayPolicy.BLOCK_OUT,
                field=field,
                logic=GatewayLogic(str(old.logic)),
                value=old.value,
                status_code=old.status_code,
                enabled=old.enabled,
            )
        )
    return rules, dropped
