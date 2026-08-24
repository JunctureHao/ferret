"""翻译标记：给模块级/类级的文案表用。

界面文案在**模块级或类体里求值就废了** —— `core/application.py` 在顶层就
`from ferret.apps.window import MainWindow`，那一刻 `_init_i18n()` 还没装翻译器，
求出来的字符串会永久冻结在源语言（英文）上，切中文再也换不回来。所以这类表只能存
**标记**，等到使用点才用 `QCoreApplication.translate(context, marked)` 求值。

标记必须写成 `QT_TRANSLATE_NOOP("Context", "text")`：`pyside6-lupdate` 认的是调用点
的函数名，换个名字（哪怕只是转发一层）就提取不到。而 PySide6 的 stub 把它标成返回
`object`（运行期它原样返回入参那个 str），直接用会让 ty 判定文案表的元素不是 `str`。
这里重新绑定同名符号并补上真实签名，两边都满足 —— 各处从本模块导入即可。

`resolve_marker` 是使用点那一半：查表拿标记、当场求值。文案表都长一个样（enum → 标记），
各页自己写一遍查表 + `translate` 只是把同一段话抄七遍。
"""

from collections.abc import Callable
from typing import Any, cast

from PySide6.QtCore import QT_TRANSLATE_NOOP as _qt_translate_noop
from PySide6.QtCore import QCoreApplication

QT_TRANSLATE_NOOP = cast("Callable[[str, str], str]", _qt_translate_noop)


def resolve_marker(
    table: dict[Any, str], key: Any, context: str, fallback: str = ""
) -> str:
    """把 `table[key]` 那条 `QT_TRANSLATE_NOOP` 标记译成当前语言。

    `context` 必须和标记里写的那个字符串逐字一致 —— 运行期按 (context, source) 查
    目录，这里传变量不影响提取（提取只看 `QT_TRANSLATE_NOOP` 那两个字面量）。
    表里没有这一项时返回 `fallback`，调用方通常给 enum 的原始值或空串。
    """
    marked = table.get(key)
    if marked is None:
        return fallback
    return QCoreApplication.translate(context, marked)


__all__ = ["QT_TRANSLATE_NOOP", "resolve_marker"]
