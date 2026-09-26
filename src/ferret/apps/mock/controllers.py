"""Mock 响应池的状态权威：CONFIG 读写 + facade 下发（.plans/0-server-playback.md）。

池内容（flow 副本）由 facade 托管在 `runtime.mock_pool` + 池文件里，控制器只持有
它的**纯数据快照**；旋钮的落盘与下发都从这里走（RewriteController 同款职责划分）。
"""

from typing import Any

from PySide6.QtCore import QObject, Signal

from ferret.core.mitm import MitmFacade
from ferret.core.settings import CONFIG

# 原生 server_replay_extra 的 choices。与 core/settings.py::mock_extra 的
# OptionsValidator 是同一份清单，改一处必须同步另一处。
EXTRA_VALUES: tuple[str, ...] = ("forward", "kill", "204", "400", "404", "500")

# 旋钮键（原生 server_replay_* 选项名）→ 对应的 CONFIG item。controller 开机把
# CONFIG 播种进 runtime、变更时先推 facade 再写 CONFIG（失败回滚不落盘）。
_KNOB_ITEMS: dict[str, Any] = {
    "server_replay_extra": CONFIG.mock_extra,
    "server_replay_reuse": CONFIG.mock_reuse,
    "server_replay_refresh": CONFIG.mock_refresh,
    "server_replay_ignore_host": CONFIG.mock_ignore_host,
    "server_replay_ignore_params": CONFIG.mock_ignore_params,
    "server_replay_use_headers": CONFIG.mock_use_headers,
}


class MockController(QObject):
    """池快照 + 总开关 + 旋钮的唯一入口；界面只认这里的信号。"""

    pool_changed = Signal(dict)
    enabled_changed = Signal(bool)
    knobs_changed = Signal()
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        # CONFIG 是旋钮的落盘权威；`_knobs` 是已生效值的镜像，兼作下发失败时的
        # 回滚源。开机就交给 facade（内核起来时 `_apply_serverplayback` 播种同一
        # 份），与规则控制器「构造即下发」同一时序。
        self._enabled = bool(CONFIG.get(CONFIG.mock_enabled))
        self._knobs: dict[str, Any] = {}
        for key, item in _KNOB_ITEMS.items():
            value = CONFIG.get(item)
            # 列表项拷贝成新 list，后续回滚写回才不会触发 QConfig.set 的
            # 「同值不落盘」短路（core/settings.py 注释里的坑）。
            self._knobs[key] = list(value) if isinstance(value, list) else value
        self._mitm.set_mock_knobs(self._knobs)
        self._mitm.set_mock_enabled(self._enabled)
        self._snapshot: dict[str, Any] = {
            "enabled": self._enabled,
            "count": 0,
            "entries": [],
        }
        self.refresh()

    @property
    def enabled(self) -> bool:
        """mock 总开关。关闭 = 原生 flowmap 清空，所有请求照常直连。"""
        return self._enabled

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    @property
    def knobs(self) -> dict[str, Any]:
        return dict(self._knobs)

    def refresh(self) -> None:
        """从 facade 重取池快照并广播。"""
        self._snapshot = self._mitm.mock_snapshot()
        self.pool_changed.emit(self._snapshot)

    def add_from_selection(self, flow_ids: list[str]) -> None:
        """捕获页右键「加入 Mock 响应」的落点（MainWindow 牵线进来）。"""
        if not flow_ids:
            return
        try:
            added = self._mitm.add_mock_flows(flow_ids)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("加入 Mock 失败"), str(exc))
            return
        if added:
            self.operation_succeeded.emit(
                self.tr("已加入 {} 条 Mock 响应").format(added)
            )
        self.refresh()

    def import_file(self, path: str) -> None:
        try:
            added = self._mitm.add_mock_file(path)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("导入失败"), str(exc))
            return
        self.operation_succeeded.emit(
            self.tr("已导入 {} 条 Mock 响应").format(added)
        )
        self.refresh()

    def remove_entries(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        try:
            removed = self._mitm.remove_mock_flows(entry_ids)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("删除失败"), str(exc))
            return
        if removed:
            self.operation_succeeded.emit(
                self.tr("已删除 {} 条 Mock 响应").format(removed)
            )
        self.refresh()

    def clear_all(self) -> None:
        try:
            self._mitm.clear_mock()
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("清空失败"), str(exc))
            return
        self.operation_succeeded.emit(self.tr("已清空 Mock 响应池"))
        self.refresh()

    def export_pool(self, path: str) -> None:
        try:
            count = self._mitm.export_mock_pool(path)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.operation_failed.emit(self.tr("导出失败"), str(exc))
            return
        self.operation_succeeded.emit(
            self.tr("已导出 {} 条 Mock 响应").format(count)
        )

    def set_enabled(self, enabled: bool) -> bool:
        if enabled == self._enabled:
            return False
        try:
            self._mitm.set_mock_enabled(enabled)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.enabled_changed.emit(self._enabled)
            self.operation_failed.emit(self.tr("总开关未生效"), str(exc))
            return False
        self._enabled = enabled
        CONFIG.set(CONFIG.mock_enabled, enabled)
        self.enabled_changed.emit(enabled)
        self.operation_succeeded.emit(
            self.tr("Mock 已开启") if enabled else self.tr("Mock 已关闭")
        )
        self.refresh()
        return True

    # —— 旋钮。CONFIG 项由 SettingCard 直接写（自持久化），这里挂的是
    # valueChanged 的下游：推送 facade、失败回滚 CONFIG（卡片随之弹回）。 ——

    def attach_config_watchers(self) -> None:
        """把 CONFIG 旋钮项的变更接进控制器。由界面构造完成后调用一次。

        拆出独立方法而不是放进 __init__：控制器先于界面创建（MainWindow 构造
        顺序），而值是设置卡写的，接早了只是空转。绑的是实例方法，控制器销毁
        时 Qt 自动断连（CONFIG 是进程级单例，不能留悬空连接）。
        """
        for item in _KNOB_ITEMS.values():
            item.valueChanged.connect(self._on_config_changed)

    def _on_config_changed(self, _value: Any = None) -> None:
        """任一旋钮 CONFIG 项变更 → 逐项对账、把变化的键推给 facade。"""
        for key, item in _KNOB_ITEMS.items():
            value = CONFIG.get(item)
            if self._knobs.get(key) == value:
                continue
            try:
                self._mitm.set_mock_knobs({key: value})
            except (ValueError, RuntimeError, TimeoutError) as exc:
                # 回滚 CONFIG（必须传新 list，见 core/settings.py 的注释），
                # 绑同一项的卡片会跟着 valueChanged 弹回旧值；回滚引发的
                # valueChanged 在这里按「与镜像一致」短路，不会形成回环。
                previous = self._knobs[key]
                CONFIG.set(
                    item,
                    list(previous) if isinstance(previous, list) else previous,
                )
                self.operation_failed.emit(self.tr("设置未生效"), str(exc))
                continue
            self._knobs[key] = value
            self.knobs_changed.emit()
