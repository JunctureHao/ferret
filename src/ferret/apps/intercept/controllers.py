"""Breakpoint state owner: rule persistence, push-down, and the held-flow queue."""

from dataclasses import replace

from PySide6.QtCore import QObject, QTimer, Signal

from ferret.core.mitm import (
    HTTPFlow,
    InterceptRule,
    MitmFacade,
    RequestEdit,
    ResponseEdit,
    intercept_rules_from_config,
    intercept_rules_to_config,
)
from ferret.core.settings import CONFIG


def _usable(rule: InterceptRule) -> bool:
    """能否下发。停用/空值的规则不参与下发（判据与 `_is_active` 一致），天然可用。"""
    if not rule.enabled or not rule.value.strip():
        return True
    try:
        rule.validate()
    except ValueError:
        return False
    return True


class InterceptController(QObject):
    """规则的唯一权威副本，同时是拦截队列的唯一来源。

    队列不自己记账，每次都向 `MitmFacade.intercepted_flows()` 重新要一份快照：内核
    那边有三种「谁攥着这条流量」的情形（断点 addon、网关挂起策略、原生 addon），
    只有扫 `flow.intercepted` 才能全看见，本地记账必然对不齐。
    """

    rules_changed = Signal(list)
    enabled_changed = Signal(bool)
    flows_changed = Signal(list)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        self._flows: list[HTTPFlow] = []
        self._refresh_queued = False
        self._enabled = bool(CONFIG.get(CONFIG.intercept_enabled))
        # 手改坏的 config 不该让规则整批失效（options.update 是原子的，一条坏表达式
        # 会把整批回滚），也不该悄悄消失：停用它、留给用户改。
        self._rules = [
            rule if _usable(rule) else replace(rule, enabled=False)
            for rule in intercept_rules_from_config(CONFIG.get(CONFIG.intercept_rules))
        ]
        self._mitm.set_intercept_rules(self._rules)
        self._mitm.set_intercept_enabled(self._enabled)

        runtime = self._mitm.runtime
        # 拦下/放行都从这条信号来（`runtime._on_flow_intercepted` 在 mitm 线程上发，
        # 跨线程走 Qt 队列连接）。这里只当「该重新问一次」的触发器用，不读它带的
        # flow —— 那是内核线程上的活对象，Qt 线程不许碰（AGENTS.md §3）。
        runtime.flow_intercepted.connect(self._schedule_refresh)
        runtime.ready.connect(self._schedule_refresh)
        runtime.stopped.connect(self._on_runtime_stopped)

    @property
    def rules(self) -> list[InterceptRule]:
        return list(self._rules)

    @property
    def enabled(self) -> bool:
        """断点总开关。关掉后不再拦新流量，已拦下的不会自动放行。"""
        return self._enabled

    @property
    def flows(self) -> list[HTTPFlow]:
        return list(self._flows)

    def rule_at(self, index: int) -> InterceptRule | None:
        if 0 <= index < len(self._rules):
            return self._rules[index]
        return None

    def flow_at(self, index: int) -> HTTPFlow | None:
        if 0 <= index < len(self._flows):
            return self._flows[index]
        return None

    # —— 规则 ——

    def add_rule(self, rule: InterceptRule) -> bool:
        return self._commit([*self._rules, rule], self.tr("已添加断点规则"))

    def update_rule(self, index: int, rule: InterceptRule) -> bool:
        if not (0 <= index < len(self._rules)):
            return False
        rules = list(self._rules)
        rules[index] = rule
        return self._commit(rules, self.tr("已更新断点规则"))

    def remove_rules(self, indexes: list[int]) -> bool:
        dropped = {i for i in indexes if 0 <= i < len(self._rules)}
        if not dropped:
            return False
        rules = [r for i, r in enumerate(self._rules) if i not in dropped]
        return self._commit(rules, self.tr("已删除 {} 条断点规则").format(len(dropped)))

    def set_enabled(self, index: int, enabled: bool) -> bool:
        rule = self.rule_at(index)
        if rule is None or rule.enabled == enabled:
            return False
        rules = list(self._rules)
        rules[index] = replace(rule, enabled=enabled)
        return self._commit(rules, "")

    def set_intercept_enabled(self, enabled: bool) -> bool:
        """Flip the master switch. 关掉不会放行已拦下的流量（与网关刻意相反）。"""
        if enabled == self._enabled:
            return False
        try:
            self._mitm.set_intercept_enabled(enabled)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.enabled_changed.emit(self._enabled)
            self.operation_failed.emit(self.tr("总开关未生效"), str(exc))
            return False
        self._enabled = enabled
        CONFIG.set(CONFIG.intercept_enabled, enabled)
        self.enabled_changed.emit(enabled)
        self.operation_succeeded.emit(
            self.tr("断点已开启") if enabled else self.tr("断点已关闭")
        )
        return True

    def _commit(self, rules: list[InterceptRule], message: str) -> bool:
        previous = self._rules
        try:
            self._mitm.set_intercept_rules(rules)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._rules = previous
            self.rules_changed.emit(list(previous))
            self.operation_failed.emit(self.tr("规则未生效"), str(exc))
            return False
        self._rules = rules
        # QConfig.set 开头会比较 item.value == value，必须传新 list 才会落盘。
        CONFIG.set(CONFIG.intercept_rules, intercept_rules_to_config(rules))
        self.rules_changed.emit(list(rules))
        if message:
            self.operation_succeeded.emit(message)
        return True

    # —— 拦截队列 ——

    def refresh_flows(self) -> None:
        """重新向内核要一份拦截队列快照。"""
        self._refresh_queued = False
        try:
            flows = self._mitm.intercepted_flows()
        except (RuntimeError, TimeoutError):
            # 内核正在起/停的窗口期问不到，等下一次信号再问；这不是用户的错，不报。
            return
        self._flows = flows
        self.flows_changed.emit(list(flows))

    def _schedule_refresh(self, *_args) -> None:
        """把一串拦截事件并成一次刷新。

        `intercepted_flows()` 要跨线程 `runtime.call`，一条流量拦下来就同步问一次
        太贵；同一轮事件循环里攒着，末尾问一次就够（队列本来就短）。
        """
        if self._refresh_queued:
            return
        self._refresh_queued = True
        QTimer.singleShot(0, self.refresh_flows)

    def _on_runtime_stopped(self) -> None:
        """内核停了，队列里那些快照已经没有对应的活流量，清掉。"""
        self._refresh_queued = False
        self._flows = []
        self.flows_changed.emit([])

    # —— 放行 / 丢弃 / 编辑写回 ——

    def release_flows(self, flow_ids: list[str]) -> bool:
        return self._resume(flow_ids, kill=False)

    def drop_flows(self, flow_ids: list[str]) -> bool:
        return self._resume(flow_ids, kill=True)

    def release_all(self) -> bool:
        """放行全部：不管是断点、网关挂起还是原生 addon 拦下的，一并放掉。"""
        try:
            count = self._mitm.release_all_intercepted()
        except (RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("放行失败"), str(exc))
            return False
        self.refresh_flows()
        if count:
            self.operation_succeeded.emit(self.tr("已放行 {} 条").format(count))
        return bool(count)

    def _resume(self, flow_ids: list[str], *, kill: bool) -> bool:
        if not flow_ids:
            return False
        try:
            count = (
                self._mitm.drop_flows(flow_ids)
                if kill
                else self._mitm.release_flows(flow_ids)
            )
        except (RuntimeError, TimeoutError) as exc:
            # 整句分支写死，不拿动词去拼：别的语言语序不同，拼出来的句子没法翻。
            title = self.tr("丢弃失败") if kill else self.tr("放行失败")
            self.operation_failed.emit(title, str(exc))
            return False
        self.refresh_flows()
        if count:
            done = self.tr("已丢弃 {} 条") if kill else self.tr("已放行 {} 条")
            self.operation_succeeded.emit(done.format(count))
        return bool(count)

    def revert_flow(self, flow_id: str) -> bool:
        return self._mutate(
            lambda: self._mitm.revert_flow(flow_id), self.tr("已撤销编辑")
        )

    def apply_request(
        self, flow_id: str, edit: RequestEdit, *, release: bool = False
    ) -> bool:
        return self._mutate(
            lambda: self._mitm.apply_request_edits(flow_id, edit, release=release),
            self.tr("已放行") if release else self.tr("已应用请求改动"),
        )

    def apply_response(
        self, flow_id: str, edit: ResponseEdit, *, release: bool = False
    ) -> bool:
        return self._mutate(
            lambda: self._mitm.apply_response_edits(flow_id, edit, release=release),
            self.tr("已放行") if release else self.tr("已应用响应改动"),
        )

    def fake_response(self, flow_id: str, edit: ResponseEdit) -> bool:
        """请求期直接返回：构造一条响应回给客户端，不发往服务器（顺手放行）。"""
        return self._mutate(
            lambda: self._mitm.fake_response(flow_id, edit),
            self.tr("已直接返回伪造响应"),
        )

    def _mutate(self, action, message: str) -> bool:
        """写回类操作的唯一出口：失败只报错不刷队列（内核那边什么都没改）。"""
        try:
            action()
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("操作失败"), str(exc))
            return False
        self.refresh_flows()
        self.operation_succeeded.emit(message)
        return True
