"""User-script state owner: persistence, file management, kernel push-down."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ferret.core.log import get_logger
from ferret.core.mitm import (
    SCRIPT_ORIGIN_NEW,
    MitmFacade,
    ScriptEntry,
    ScriptStatus,
    scripts_from_config,
    scripts_to_config,
)
from ferret.core.settings import CONFIG, get_scripts_dir

log = get_logger("scripts")

# 「新建」入口的初始内容（plans/scripts.md §7 第 4 条：v1 只给最简模板）。
# 这是写进脚本文件的正文，不是界面文案，所以不过 tr()。钩子名与签名照
# mitmproxy 的 addon 事件模型，用户照着改就能用。
SCRIPT_TEMPLATE = '''"""Ferret 用户脚本。

钩子函数名沿用 mitmproxy 的 addon 事件模型，写了哪个就会被调用哪个：
request / response / websocket_message / error 等等。
脚本以应用同等权限执行，只跑自己信得过的代码。
"""


def request(flow):
    """每条请求转发前调用；改 flow.request 即改发出去的报文。"""


def response(flow):
    """每条响应回到客户端前调用；改 flow.response 即改收到的报文。"""
'''


def _usable(entry: ScriptEntry) -> bool:
    """能否下发。配置是可以手改的，一条写坏的路径会让整批 `validate` 抛。"""
    try:
        entry.validate()
    except ValueError:
        return False
    return True


class ScriptsController(QObject):
    """脚本清单的唯一权威副本：配置读写、文件读写与 facade 下发都只从这里发生。"""

    scripts_changed = Signal(list)
    statuses_changed = Signal(dict)
    operation_failed = Signal(str, str)
    operation_succeeded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        self._statuses: dict[str, ScriptStatus] = {}
        # 手改坏的 config 不该把整批拖下水（`apply_scripts` 是整批校验的）。坏条目
        # 只能丢 —— 与重写页「停用留着」的处理不同：那边停用的规则仍能存回配置，
        # 这边连停用条目也要过 `validate`，留着就等于每次下发都抛。
        entries = scripts_from_config(CONFIG.get(CONFIG.scripts))
        self._scripts = [entry for entry in entries if _usable(entry)]
        if len(self._scripts) != len(entries):
            log.warning(
                "配置里有无法使用的脚本条目，已丢弃 %d 条",
                len(entries) - len(self._scripts),
            )
        self._mitm.set_scripts(self._scripts)
        # 装载状态由 mitm 线程经 runtime 信号送达（队列连接，见 core/mitm/runtime.py）。
        self._mitm.runtime.script_status_changed.connect(self._on_status_changed)
        # 内核停了就没有「已装载」这回事了：清空回到「待装载」，别让上一轮的绿勾
        # 一直挂着骗人（下次启动 `_apply_scripts` 会重新播种状态）。
        self._mitm.runtime.stopped.connect(self._on_kernel_stopped)

    # --- 只读 ---

    @property
    def scripts(self) -> list[ScriptEntry]:
        return list(self._scripts)

    @property
    def statuses(self) -> dict[str, ScriptStatus]:
        return dict(self._statuses)

    def script_at(self, index: int) -> ScriptEntry | None:
        if 0 <= index < len(self._scripts):
            return self._scripts[index]
        return None

    def index_of(self, path: str) -> int:
        for i, entry in enumerate(self._scripts):
            if entry.path == path:
                return i
        return -1

    def status_of(self, path: str) -> ScriptStatus | None:
        return self._statuses.get(path)

    @property
    def scripts_dir(self) -> Path:
        """应用内「新建」脚本的落盘目录（打包后仍可写）。"""
        return get_scripts_dir()

    # --- 增删改 ---

    def import_scripts(self, paths: list[str]) -> bool:
        """导入外部 `.py`：只存路径不复制，文件原地被引用（同 `mitmproxy -s`）。"""
        existing = {entry.path for entry in self._scripts}
        fresh = [
            ScriptEntry(path=str(Path(path)))
            for path in paths
            if str(Path(path)) not in existing
        ]
        if not fresh:
            return False
        return self._commit(
            [*self._scripts, *fresh], self.tr("已添加 {} 个脚本").format(len(fresh))
        )

    def create_script(self, filename: str, text: str = SCRIPT_TEMPLATE) -> str:
        """在托管目录里新建一个脚本并加进清单；返回落盘路径（失败返回空串）。"""
        target = self.scripts_dir / filename
        try:
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.operation_failed.emit(self.tr("新建脚本失败"), str(exc))
            return ""
        entry = ScriptEntry(path=str(target), origin=SCRIPT_ORIGIN_NEW)
        if not self._commit([*self._scripts, entry], self.tr("已新建脚本")):
            return ""
        return entry.path

    def remove_scripts(self, indexes: list[int], *, delete_files: bool = False) -> bool:
        """从清单移除；`delete_files` 才会动磁盘上的文件（默认只移除条目）。

        **删文件只对 new 条目生效**：import 条目引用的是用户自己的文件，移除时
        「文件不动」是 plans/scripts.md §3.4 那张表钉死的语义 —— 混选时勾了
        「同时删除文件」也不许碰它们。
        """
        dropped = {i for i in indexes if 0 <= i < len(self._scripts)}
        if not dropped:
            return False
        victims = [self._scripts[i] for i in sorted(dropped)]
        remaining = [e for i, e in enumerate(self._scripts) if i not in dropped]
        if not self._commit(
            remaining, self.tr("已移除 {} 个脚本").format(len(victims))
        ):
            return False
        if delete_files:
            for entry in victims:
                if entry.origin != SCRIPT_ORIGIN_NEW:
                    continue
                try:
                    Path(entry.path).unlink(missing_ok=True)
                except OSError as exc:
                    self.operation_failed.emit(self.tr("删除文件失败"), str(exc))
        return True

    def set_enabled(self, index: int, enabled: bool) -> bool:
        entry = self.script_at(index)
        if entry is None or entry.enabled == enabled:
            return False
        entries = list(self._scripts)
        entries[index] = replace(entry, enabled=enabled)
        return self._commit(entries, "")

    def set_scripts_enabled(self, indexes: list[int], enabled: bool) -> bool:
        """多选批量启停：一次 `_commit`，不是每行下发一遍。"""
        entries = list(self._scripts)
        touched = False
        for i in indexes:
            if not (0 <= i < len(entries)) or entries[i].enabled == enabled:
                continue
            entries[i] = replace(entries[i], enabled=enabled)
            touched = True
        if not touched:
            return False
        return self._commit(entries, "")

    def move_script(self, index: int, offset: int) -> bool:
        """上移 / 下移。列表序＝执行序，所以这是有语义的操作，不只是排版。"""
        return self.move_script_to(index, index + offset)

    def move_script_to(self, index: int, target: int) -> bool:
        """把第 `index` 条搬到 `target` 位（拖拽换序的落点语义）。"""
        if not (0 <= index < len(self._scripts)):
            return False
        if not (0 <= target < len(self._scripts)) or target == index:
            return False
        entries = list(self._scripts)
        entries.insert(target, entries.pop(index))
        return self._commit(entries, "")

    # --- 文件内容 ---

    def read_script(self, path: str) -> str:
        """读脚本正文。

        Raises:
            OSError: 文件不存在 / 读不动（调用方翻成面板上的提示）。
        """
        return Path(path).read_text(encoding="utf-8")

    def save_script(self, path: str, text: str) -> bool:
        """保存 = 写盘 + 立即重载（plans/scripts.md §3.4）。"""
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            self.operation_failed.emit(self.tr("保存失败"), str(exc))
            return False
        self.reload_script(path)
        self.operation_succeeded.emit(self.tr("已保存并重载"))
        return True

    def reload_script(self, path: str) -> bool:
        """强制重载一条（外部编辑器改完必点）；内核没跑时只是 no-op。"""
        try:
            self._mitm.reload_script(path)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            self.operation_failed.emit(self.tr("重载失败"), str(exc))
            return False
        return True

    # --- 内部 ---

    def _commit(self, entries: list[ScriptEntry], message: str) -> bool:
        previous = self._scripts
        try:
            self._mitm.set_scripts(entries)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self._scripts = previous
            self.scripts_changed.emit(list(previous))
            self.operation_failed.emit(self.tr("脚本未生效"), str(exc))
            return False
        self._scripts = entries
        # QConfig.set 开头会比较 item.value == value，必须传新 list 才会落盘。
        CONFIG.set(CONFIG.scripts, scripts_to_config(entries))
        # 清掉已经不在清单里的状态，免得同名路径重新加回来时读到上一轮的旧结果。
        live = {entry.path for entry in entries}
        self._statuses = {p: s for p, s in self._statuses.items() if p in live}
        self.scripts_changed.emit(list(entries))
        self.statuses_changed.emit(dict(self._statuses))
        if message:
            self.operation_succeeded.emit(message)
        return True

    def _on_status_changed(self, path: str, status: object) -> None:
        if not isinstance(status, ScriptStatus):
            return
        self._statuses[path] = status
        self.statuses_changed.emit(dict(self._statuses))

    def _on_kernel_stopped(self) -> None:
        if not self._statuses:
            return
        self._statuses = {}
        self.statuses_changed.emit({})
