"""Gateway-rule state owner: persistence plus push-down to the mitmproxy kernel."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from ferret.core.log import get_logger
from ferret.core.mitm import (
    GatewayField,
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
    MitmFacade,
    gateway_rules_from_block_config,
    gateway_rules_from_config,
    gateway_rules_to_config,
)
from ferret.core.settings import CONFIG

log = get_logger("mitmproxy")


def _usable(rule: GatewayRule) -> bool:
    """能否下发。停用/空值的规则不参与下发，天然可用。"""
    if not rule.enabled or not rule.value.strip():
        return True
    try:
        rule.validate()
    except ValueError:
        return False
    return True


class GatewayController(QObject):
    """规则的唯一权威副本：配置读写与 facade 下发都只从这里发生。"""

    rules_changed = Signal(list)
    enabled_changed = Signal(bool)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        # 页面构造时还没人接信号，迁移里丢掉的规则得先攒着，等界面起来再报一次。
        self.pending_notice: tuple[str, str] | None = None
        self._enabled = bool(CONFIG.get(CONFIG.gateway_enabled))
        # 手改坏的 config 不该让规则整批失效（options.update 是原子的，一条坏 pattern
        # 会把整批回滚），也不该悄悄消失：停用它、留给用户改。
        self._rules = [
            rule if _usable(rule) else replace(rule, enabled=False)
            for rule in self._load_rules()
        ]
        self._mitm.set_gateway_rules(self._rules)
        self._mitm.set_gateway_enabled(self._enabled)

    def _load_rules(self) -> list[GatewayRule]:
        """Read the gateway rules, migrating the legacy blocklist on first run.

        网关页取代了屏蔽页，`Proxy.BlockList` 里的老规则等价物是 L7 屏蔽（出），
        一次性搬过来再把老键清空。只在网关自己还没有规则时搬，否则用户后来删掉的
        规则会在每次启动时复活。
        """
        raw = CONFIG.get(CONFIG.gateway_rules)
        if raw:
            return gateway_rules_from_config(raw)
        legacy = CONFIG.get(CONFIG.block_list)
        if not legacy:
            return []
        rules, dropped = gateway_rules_from_block_config(legacy)
        # 先落盘再清空老键：中途崩了顶多重跑一次迁移，不会两头都没有。
        CONFIG.set(CONFIG.gateway_rules, gateway_rules_to_config(rules))
        CONFIG.set(CONFIG.block_list, [])
        log.info("已把 %d 条旧屏蔽规则迁移为网关规则", len(rules))
        if dropped:
            # 网关不做 URI 匹配（两个平面里连接级那一半看不见 URI），这些规则没有
            # 等价物，只能丢弃 —— 但绝不能悄悄丢。
            log.warning("%d 条按 URL 匹配的旧屏蔽规则无法迁移，已丢弃", dropped)
            self.pending_notice = (
                self.tr("部分旧屏蔽规则已丢弃"),
                self.tr(
                    "{} 条按 URL 匹配的规则无法迁移：网关只按主机和方法匹配"
                ).format(dropped),
            )
        return rules

    @property
    def rules(self) -> list[GatewayRule]:
        return list(self._rules)

    @property
    def enabled(self) -> bool:
        """网关总开关。关掉之后所有规则一律不判，挂起中的流量立刻放行。"""
        return self._enabled

    def rule_at(self, index: int) -> GatewayRule | None:
        if 0 <= index < len(self._rules):
            return self._rules[index]
        return None

    def add_rule(self, rule: GatewayRule) -> bool:
        return self._commit([*self._rules, rule], self.tr("已添加网关规则"))

    def update_rule(self, index: int, rule: GatewayRule) -> bool:
        if not (0 <= index < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index] = rule
        return self._commit(rules, self.tr("已更新网关规则"))

    def remove_rules(self, indexes: list[int]) -> bool:
        dropped = {i for i in indexes if 0 <= i < len(self._rules)}
        if not dropped:
            return False
        rules = [r for i, r in enumerate(self._rules) if i not in dropped]
        return self._commit(rules, self.tr("已删除 {} 条网关规则").format(len(dropped)))

    def set_enabled(self, index: int, enabled: bool) -> bool:
        rule = self.rule_at(index)
        if rule is None or rule.enabled == enabled:
            return False
        rules = list(self._rules)
        rules[index] = replace(rule, enabled=enabled)
        return self._commit(rules, "")

    def move_rule(self, index: int, offset: int) -> bool:
        """调整裁决顺序。策略优先级先说话（仅允许 > 绕行 > 屏蔽 > 挂起），
        **同优先级**内才按行序取靠前的那条，所以上下移动是有语义的操作。"""
        target = index + offset
        if not (0 <= index < len(self._rules)) or not (0 <= target < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index], rules[target] = rules[target], rules[index]
        return self._commit(rules, "")

    def add_host_rule(self, host: str) -> bool:
        """右键「屏蔽此主机」：精确匹配该主机的 L7 屏蔽（出），已存在则不重复添加。"""
        host = (host or "").strip()
        if not host:
            self.operation_failed.emit(
                self.tr("无法屏蔽"), self.tr("该流量没有可用的主机名")
            )
            return False
        rule = GatewayRule(
            layer=GatewayLayer.L7,
            policy=GatewayPolicy.BLOCK_OUT,
            field=GatewayField.HOST,
            logic=GatewayLogic.EQUALS,
            value=host,
        )
        for existing in self._rules:
            if (
                existing.field == GatewayField.HOST
                and existing.logic == GatewayLogic.EQUALS
                and existing.value.strip() == host
            ):
                self.operation_failed.emit(
                    self.tr("无需重复添加"),
                    self.tr("{} 已有对应的网关规则").format(host),
                )
                return False
        return self._commit([*self._rules, rule], self.tr("已屏蔽 {}").format(host))

    def set_gateway_enabled(self, enabled: bool) -> bool:
        """Flip the master switch. 关掉时内核会把挂起中的流量一并放行。"""
        if enabled == self._enabled:
            return False
        try:
            self._mitm.set_gateway_enabled(enabled)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.enabled_changed.emit(self._enabled)
            self.operation_failed.emit(self.tr("总开关未生效"), str(exc))
            return False
        self._enabled = enabled
        CONFIG.set(CONFIG.gateway_enabled, enabled)
        self.enabled_changed.emit(enabled)
        self.operation_succeeded.emit(
            self.tr("网关已开启") if enabled else self.tr("网关已关闭")
        )
        return True

    def _commit(self, rules: list[GatewayRule], message: str) -> bool:
        previous = self._rules
        try:
            self._mitm.set_gateway_rules(rules)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._rules = previous
            self.rules_changed.emit(list(previous))
            self.operation_failed.emit(self.tr("规则未生效"), str(exc))
            return False
        self._rules = rules
        # QConfig.set 开头会比较 item.value == value，必须传新 list 才会落盘。
        CONFIG.set(CONFIG.gateway_rules, gateway_rules_to_config(rules))
        self.rules_changed.emit(list(rules))
        if message:
            self.operation_succeeded.emit(message)
        return True
