"""User-script model layer and the module loader ported from mitmproxy.

用户脚本扩展（plans/scripts.md）：ScriptEntry 是落盘模型（路径 + 启用位 +
来源标记），装载/卸载/状态上报在 addons.py::FerretScriptAddon，钩子派发借
原生 addonmanager 机制，不重写。

本模块只碰模型与纯 importlib 装载：validate() 的错误文案会上界面，所以过
`QCoreApplication.translate`；`from_dict` 那几条不译 —— 它们被
`scripts_from_config` 吞掉，从不上界面（与 rewrite.py 同一条惯例）。

`load_script_module` 是原生 `mitmproxy.addons.script.load_script` 的等价搬运
（上游 BSD 许可证）：纯 importlib 逻辑照搬，分叉只有四处 ——
错误经参数传入的 `report` 回调上报（原生走 logging）；模块名用路径 md5
（原生用 basename，同名脚本互相顶掉 sys.modules 里的槽位）；冻结环境提示
换中文译文；装载一律现编源码、不碰 `__pycache__`（见 `_FreshSourceLoader`）。
改装载语义之前先读上游源码再动。
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType
from typing import Any

from PySide6.QtCore import QCoreApplication

# 条目来源标记：只服务 UI 分叉（导入的外部文件 vs 应用内新建的托管文件），
# core 装载一律不读它（plans/scripts.md §3.4）。
SCRIPT_ORIGIN_IMPORT = "import"
SCRIPT_ORIGIN_NEW = "new"


@dataclass(frozen=True)
class ScriptEntry:
    """One user script: a path plus an enable bit.

    存在性检查刻意**不**放在 validate()：文件可以先配置后落盘（与重写引擎
    `@file` 的刻意差异同一条姿态），文件缺失在装载时翻成 MISSING 状态。
    """

    path: str
    enabled: bool = True
    origin: str = SCRIPT_ORIGIN_IMPORT

    def validate(self) -> None:
        """Raises:
        ValueError: 路径为空或不是 ``.py`` 文件。
        """
        if not self.path.strip():
            raise ValueError(QCoreApplication.translate("Scripts", "脚本路径不能为空"))
        if not self.path.lower().endswith(".py"):
            raise ValueError(
                QCoreApplication.translate("Scripts", "脚本必须是 .py 文件")
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "enabled": self.enabled,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> ScriptEntry:
        """Rebuild an entry from persisted data; raises on anything unusable."""
        if not isinstance(raw, dict):
            raise TypeError("脚本条目必须是对象")
        path = str(raw.get("path", ""))
        if not path:
            raise ValueError("脚本条目缺少 path")
        return cls(
            path=path,
            enabled=bool(raw.get("enabled", True)),
            # 老配置没有 origin 键，缺省视为 import（外部文件原地引用）。
            origin=str(raw.get("origin", SCRIPT_ORIGIN_IMPORT)),
        )


class ScriptState(StrEnum):
    """Load-state of one entry; addon 回调给 UI 的只读快照，不落盘。"""

    LOADED = "loaded"
    ERROR = "error"
    MISSING = "missing"
    DISABLED = "disabled"


@dataclass(frozen=True)
class ScriptStatus:
    """Snapshot of one entry's load-state; ``error`` 是 traceback 文本（不译）。"""

    state: ScriptState
    error: str = ""


def scripts_from_config(raw: Any) -> list[ScriptEntry]:
    """Read entries back from persisted config, dropping entries we cannot parse."""
    if not isinstance(raw, list):
        return []
    entries: list[ScriptEntry] = []
    for item in raw:
        try:
            entries.append(ScriptEntry.from_dict(item))
        except (TypeError, ValueError):
            continue
    return entries


def scripts_to_config(entries: list[ScriptEntry]) -> list[dict[str, Any]]:
    """Serialize entries for persistence."""
    return [entry.to_dict() for entry in entries]


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    """SourceFileLoader that always compiles the source, never a cached ``.pyc``.

    第四处分叉（原生直接用 `SourceFileLoader`）：`SourceLoader.get_code` 会读写
    脚本旁边的 `__pycache__`，而缓存有效性只看「源文件 mtime 的**整秒**值 + 字节
    数」。内嵌编辑器是「保存即重载」，同一秒内把 `"1"` 改成 `"9"` 这种等长改动
    完全正常 —— 走原生缓存的话重载会拿回改之前的字节码，用户看到的是「保存了却
    没生效」。原生靠 1s 轮询 watcher 撞不上这个窗口，我们撞得上。

    覆写只去掉缓存这一层：源码字节仍由 `get_data` 读、仍由 `source_to_code`
    编译（编码嗅探、行结束符归一化都还是 CPython 那一套），顺带也不再往用户脚本
    目录里写 `__pycache__`。文件不存在时 `get_data` 抛 FileNotFoundError，
    MISSING 语义不变。
    """

    def get_code(self, fullname: str) -> types.CodeType:
        filename = self.get_filename(fullname)
        return self.source_to_code(self.get_data(filename), filename)


def load_script_module(
    path: str, report: Callable[[str, BaseException], None]
) -> ModuleType | None:
    """Load a script file as a fresh module; 失败经 ``report`` 上报并返回 None。

    原生 `load_script`（mitmproxy/addons/script.py）的等价搬运：sys.path 临时
    插入脚本所在目录（脚本 import 同目录辅助模块靠它），finally 里恢复；模块名
    前缀 `__mitmproxy_script__.` 与原生一致，后缀换成路径 md5 —— 原生用
    basename，两个同名脚本会互相顶掉；每次装载前 pop 掉 sys.modules 里的旧槽位，
    重载拿到的是全新模块对象。

    FileNotFoundError 也走 ``report``（调用方据此翻成 ScriptState.MISSING）。
    """
    fullname = "__mitmproxy_script__.{}".format(
        hashlib.md5(os.path.abspath(path).encode("utf-8")).hexdigest()
    )
    sys.modules.pop(fullname, None)
    oldpath = sys.path[:]
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    try:
        loader = _FreshSourceLoader(fullname, path)
        spec = importlib.util.spec_from_loader(fullname, loader=loader)
        if spec is None:
            raise ImportError(f"无法为脚本创建加载规格: {path}")
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        if not getattr(module, "name", None):
            module.name = path  # ty: ignore[unresolved-attribute]
        return module
    except BaseException as exc:  # noqa: BLE001
        if getattr(sys, "frozen", False):
            # Nuitka 打包后脚本由内置解释器执行，import 第三方包必炸（上游同款
            # 提示，见 plans/scripts.md §4）。追加译文进异常消息，不吞原异常。
            hint = QCoreApplication.translate(
                "Scripts",
                "注意：打包版本自带 Python 环境，脚本无法 import 额外安装的第三方包。",
            )
            try:
                exc.args = (*exc.args, hint)
            except (AttributeError, TypeError):
                pass
        report(path, exc)
        return None
    finally:
        sys.path[:] = oldpath
