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
    """能否下发。停用/空匹配值的规则不参与下发，天然可用。"""
    if not rule.enabled or not rule.value.strip():
        return True
    try:
        rule.to_spec()
    except ValueError:
        return False
    return True


class RewriteController(QObject):
    """规则的唯一权威副本：配置读写与 facade 下发都只从这里发生。"""

    rules_changed = Signal(list)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        # 手改坏的 config 不该让规则整批失效（options.update 是原子的，一条坏 spec
        # 会把整批回滚），也不该悄悄消失：停用它、留给用户改。
        self._rules = [
            rule if _usable(rule) else replace(rule, enabled=False)
            for rule in rewrite_rules_from_config(CONFIG.get(CONFIG.rewrite_rules))
        ]
        self._mitm.set_rewrite_rules(self._rules)

    @property
    def rules(self) -> list[RewriteRule]:
        return list(self._rules)

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

    def move_rule(self, index: int, offset: int) -> bool:
        """调整优先级。原生 MapRemote 按 spec 顺序**逐条**改写同一条 URL
        （mapremote.py::request 的 for 循环没有 break），所以行序有语义。"""
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
