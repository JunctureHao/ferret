"""Rewrite-rule state owner: persistence plus push-down to the mitmproxy kernel."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from ferret.core.mitm import (
    MitmFacade,
    RewriteRule,
    rewrite_rules_from_config,
    rewrite_rules_to_config,
)
from ferret.core.settings import CONFIG


def _usable(rule: RewriteRule) -> bool:
    """能否下发。停用/没填完的规则不参与下发，天然可用。

    判据必须和 `RewriteRuleSet` 编译时的取舍完全一致（都用 `RewriteRule.filled`）：
    只按「匹配值为空」放行的话，一条手填了 URL 却缺头名的头规则会在这里被
    `validate` 判成坏规则、开机时被停用，而下发链路本来就会跳过它。
    """
    if not rule.enabled or not rule.filled:
        return True
    try:
        rule.validate()
    except ValueError:
        return False
    return True


class RewriteController(QObject):
    """规则的唯一权威副本：配置读写与 facade 下发都只从这里发生。"""

    rules_changed = Signal(list)
    enabled_changed = Signal(bool)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        # 总开关只在 runtime 内存里（plans/rewrite-ui.md §8：settings.py 零改动），
        # 每次启动都是开 —— 关掉只是「临时下发空规则」，不碰各行 enabled 落盘值。
        self._enabled = mitm.rewrite_enabled
        # 手改坏的 config 不该让规则整批失效（快照编译是整批的，一条坏规则
        # 会把整批回滚），也不该悄悄消失：停用它、留给用户改。
        self._rules = [
            rule if _usable(rule) else replace(rule, enabled=False)
            for rule in rewrite_rules_from_config(CONFIG.get(CONFIG.rewrite_rules))
        ]
        self._mitm.set_rewrite_rules(self._rules)

    @property
    def rules(self) -> list[RewriteRule]:
        return list(self._rules)

    @property
    def enabled(self) -> bool:
        """重写总开关。关掉后所有重写规则一律不生效。"""
        return self._enabled

    def rule_at(self, index: int) -> RewriteRule | None:
        if 0 <= index < len(self._rules):
            return self._rules[index]
        return None

    def add_rule(self, rule: RewriteRule) -> bool:
        return self._commit([*self._rules, rule], self.tr("已添加重写规则"))

    def update_rule(self, index: int, rule: RewriteRule) -> bool:
        if not (0 <= index < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index] = rule
        return self._commit(rules, self.tr("已更新重写规则"))

    def remove_rules(self, indexes: list[int]) -> bool:
        dropped = {i for i in indexes if 0 <= i < len(self._rules)}
        if not dropped:
            return False
        rules = [r for i, r in enumerate(self._rules) if i not in dropped]
        return self._commit(rules, self.tr("已删除 {} 条重写规则").format(len(dropped)))

    def set_enabled(self, index: int, enabled: bool) -> bool:
        rule = self.rule_at(index)
        if rule is None or rule.enabled == enabled:
            return False
        rules = list(self._rules)
        rules[index] = replace(rule, enabled=enabled)
        return self._commit(rules, "")

    def set_rules_enabled(self, indexes: list[int], enabled: bool) -> bool:
        """批量启停（§4.3）：一次 `_commit` 整批下发，行序不变。"""
        targets = {
            i
            for i in indexes
            if 0 <= i < len(self._rules) and self._rules[i].enabled != enabled
        }
        if not targets:
            return False
        rules = [
            replace(rule, enabled=enabled) if i in targets else rule
            for i, rule in enumerate(self._rules)
        ]
        return self._commit(rules, "")

    def set_rewrite_enabled(self, enabled: bool) -> bool:
        """Flip the master switch. 关掉后所有重写规则一律不生效。"""
        if enabled == self._enabled:
            return False
        try:
            self._mitm.set_rewrite_enabled(enabled)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.enabled_changed.emit(self._enabled)
            self.operation_failed.emit(self.tr("总开关未生效"), str(exc))
            return False
        self._enabled = enabled
        self.enabled_changed.emit(enabled)
        self.operation_succeeded.emit(
            self.tr("重写已开启") if enabled else self.tr("重写已关闭")
        )
        return True

    def move_rule(self, index: int, offset: int) -> bool:
        """调整优先级。自研引擎按行序对同一条流量**逐条**作用（没有短路），
        所以行序有语义。"""
        target = index + offset
        if not (0 <= index < len(self._rules)) or not (0 <= target < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index], rules[target] = rules[target], rules[index]
        return self._commit(rules, "")

    def _commit(self, rules: list[RewriteRule], message: str) -> bool:
        previous = self._rules
        try:
            self._mitm.set_rewrite_rules(rules)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._rules = previous
            self.rules_changed.emit(list(previous))
            self.operation_failed.emit(self.tr("规则未生效"), str(exc))
            return False
        self._rules = rules
        # QConfig.set 开头会比较 item.value == value，必须传新 list 才会落盘。
        CONFIG.set(CONFIG.rewrite_rules, rewrite_rules_to_config(rules))
        self.rules_changed.emit(list(rules))
        if message:
            self.operation_succeeded.emit(message)
        return True
