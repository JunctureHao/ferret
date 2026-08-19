"""Block-rule state owner: persistence plus push-down to the mitmproxy kernel."""

from dataclasses import replace

from PySide6.QtCore import QObject, Signal

from ferret.core.mitm import (
    BlockField,
    BlockLogic,
    BlockRule,
    MitmFacade,
    rules_from_config,
    rules_to_config,
)
from ferret.core.settings import CONFIG


def _usable(rule: BlockRule) -> bool:
    """能否下发。停用/空值的规则不参与下发，天然可用。"""
    if not rule.enabled or not rule.value.strip():
        return True
    try:
        rule.to_spec()
    except ValueError:
        return False
    return True


class BlockListController(QObject):
    """规则的唯一权威副本：配置读写与 facade 下发都只从这里发生。"""

    rules_changed = Signal(list)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        # 手改坏的 config 不该让规则整批失效，也不该悄悄消失：停用它、留给用户改。
        self._rules = [
            rule if _usable(rule) else replace(rule, enabled=False)
            for rule in rules_from_config(CONFIG.get(CONFIG.block_list))
        ]
        self._mitm.set_block_rules(self._rules)

    @property
    def rules(self) -> list[BlockRule]:
        return list(self._rules)

    def rule_at(self, index: int) -> BlockRule | None:
        if 0 <= index < len(self._rules):
            return self._rules[index]
        return None

    def add_rule(self, rule: BlockRule) -> bool:
        return self._commit([*self._rules, rule], self.tr("已添加屏蔽规则"))

    def update_rule(self, index: int, rule: BlockRule) -> bool:
        if not (0 <= index < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index] = rule
        return self._commit(rules, self.tr("已更新屏蔽规则"))

    def remove_rules(self, indexes: list[int]) -> bool:
        dropped = {i for i in indexes if 0 <= i < len(self._rules)}
        if not dropped:
            return False
        rules = [r for i, r in enumerate(self._rules) if i not in dropped]
        return self._commit(rules, self.tr("已删除 {} 条屏蔽规则").format(len(dropped)))

    def set_enabled(self, index: int, enabled: bool) -> bool:
        rule = self.rule_at(index)
        if rule is None or rule.enabled == enabled:
            return False
        rules = list(self._rules)
        rules[index] = replace(rule, enabled=enabled)
        return self._commit(rules, "")

    def add_host_rule(self, host: str) -> bool:
        """右键「屏蔽此主机」：精确匹配该主机，已存在则不重复添加。"""
        host = (host or "").strip()
        if not host:
            self.operation_failed.emit(
                self.tr("无法屏蔽"), self.tr("该流量没有可用的主机名")
            )
            return False
        rule = BlockRule(field=BlockField.HOST, logic=BlockLogic.EQUALS, value=host)
        for existing in self._rules:
            if (
                existing.field == BlockField.HOST
                and existing.logic == BlockLogic.EQUALS
                and existing.value.strip() == host
            ):
                self.operation_failed.emit(
                    self.tr("无需重复添加"),
                    self.tr("{} 已在屏蔽列表中").format(host),
                )
                return False
        return self._commit([*self._rules, rule], self.tr("已屏蔽 {}").format(host))

    def _commit(self, rules: list[BlockRule], message: str) -> bool:
        previous = self._rules
        try:
            self._mitm.set_block_rules(rules)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._rules = previous
            self.rules_changed.emit(list(previous))
            self.operation_failed.emit(self.tr("规则未生效"), str(exc))
            return False
        self._rules = rules
        # QConfig.set 开头会比较 item.value == value，必须传新 list 才会落盘。
        CONFIG.set(CONFIG.block_list, rules_to_config(rules))
        self.rules_changed.emit(list(rules))
        if message:
            self.operation_succeeded.emit(message)
        return True
