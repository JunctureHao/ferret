"""应用内更新：velopack SDK 的薄封装（docs/design.md#update）。

边界约定：velopack 类型（``UpdateInfo`` 等）在本模块里是**不透明句柄** —— apps 层
只在 ``check`` → ``download`` → ``apply_and_restart`` 之间透传，不读其属性；界面要
展示的字段由 ``check()`` 一并提炼成 ``UpdateBrief``。三步各自新建 UpdateManager：
构造只是读安装定位器，代价可忽略，换来的是本模块完全无状态。

形态闸门（``update_supported``）：原地更新只对「Nuitka 编译产物 + 安装态」成立。
开发态没有 velopack 安装目录，UpdateManager 构造即抛；便携版（Portable.zip）有目录
但 Velopack 的更新语义是替换整个安装目录，便携包不在此列 —— 这两类形态的 UI 一律
降级成「去发布页下载」。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Any

import velopack
from PySide6.QtCore import QCoreApplication

from ferret.core.meta import REPO_URL


class UpdateError(RuntimeError):
    """检查/下载/应用更新的统一失败出口，消息已按界面语言本地化。"""


@dataclass(frozen=True)
class UpdateBrief:
    """一次「有新版本」的展示快照。notes 恒为空串：feed 里没有 release notes
    （CI 是发布后才用 gh 补 body 的），字段留给以后，v1 走 release_page 链接。"""

    current: str
    target: str
    size: int
    notes_markdown: str
    release_page: str


def _new_manager() -> velopack.UpdateManager:
    return velopack.UpdateManager(velopack.GithubSource(REPO_URL))


def update_supported() -> bool:
    """原地更新是否可用：编译产物 + 安装态（非便携目录）。"""
    if "__compiled__" not in globals():
        return False
    try:
        return not _new_manager().get_is_portable()
    except Exception:  # noqa: BLE001 — pyo3 抛出的异常类型不固定，构造失败即未安装
        return False


def current_version() -> str:
    """当前版本号：编译态问 velopack 定位器，开发态读已安装包元数据。"""
    if "__compiled__" in globals():
        try:
            return _new_manager().get_current_version()
        except Exception:  # noqa: S110, BLE001 — 定位器失败是故意的静默，退回包元数据兜底
            pass
    try:
        return package_version("ferret")
    except PackageNotFoundError:
        return QCoreApplication.translate("Update", "未知")


def check() -> tuple[Any, UpdateBrief] | None:
    """检查更新。无更新返回 None；有则返回 (不透明句柄, 展示快照)。"""
    try:
        manager = _new_manager()
        info = manager.check_for_updates()
        if info is None:
            return None
        asset = info.TargetFullRelease
        # 实际下载优先走增量：有可用 delta 时显示其总大小，而非恒为全量包大小。
        # 基线失配时 velopack 会静默回退全量，此处只是尽力预估。
        deltas = list(info.DeltasToTarget)
        size = sum(d.Size for d in deltas) if deltas else asset.Size
        return info, UpdateBrief(
            current=manager.get_current_version(),
            target=asset.Version,
            size=size,
            notes_markdown=asset.NotesMarkdown or "",
            release_page=f"{REPO_URL}/releases/tag/v{asset.Version}",
        )
    except Exception as exc:  # pyo3 异常类型不固定，统一收口
        raise UpdateError(
            QCoreApplication.translate("Update", "检查更新失败") + f"：{exc}"
        ) from exc


def download(info: Any, on_progress: Callable[[int], None] | None = None) -> None:
    """下载更新包；``on_progress`` 收 0-100 的整数进度（在工作线程上回调）。"""
    try:
        _new_manager().download_updates(info, on_progress)
    except Exception as exc:
        raise UpdateError(
            QCoreApplication.translate("Update", "下载更新失败") + f"：{exc}"
        ) from exc


def apply_and_restart(info: Any) -> None:
    """应用更新并重启；这个调用**成功即意味着进程即将退出**，只有失败才返回。"""
    try:
        _new_manager().apply_updates_and_restart(info)
    except Exception as exc:
        raise UpdateError(
            QCoreApplication.translate("Update", "应用更新失败") + f"：{exc}"
        ) from exc
