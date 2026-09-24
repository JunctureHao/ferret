"""主题切换棘轮止血补丁（阶段 1，方案见 `.plans/0-MEMORY_OPTIMIZATION_PLAN.executable.md` §3）。

qfluentwidgets 1.11.x 的 `style_sheet.updateStyleSheet()` 每次主题切换都对每个已登记控件
调 `setStyleSheet(widget, 已存compose)`（`register=True` → `register(..., reset=True)`），把旧
compose 整包塞进新 `StyleSheetCompose([旧compose, CustomStyleSheet(widget)])` 当第一个元素——
compose 每切一次主题再套一层，`content()` 递归 join 出的文本随层数**无上界**膨胀
（实测每往返 +2.22 MB privWS，是全部内存项里唯一随时间无限增长的）。

刀口两处：
1. 主路径改用 `register=False` 重渲染既有 compose——跳过 `register` 的套壳，只按当前主题重算
   文本；compose 内已含 `CustomStyleSheet(widget)`，自定义 qss 不丢。
2. 懒分支（`lazy=True` 且控件不可见）只打 `dirty-qss` 标记，**不再** re-register（原逻辑那句
   `register(file, widget)` 同样走 `reset=True`，一样套壳）。控件下次 paint 时由
   `DirtyStyleSheetWatcher` 从 depth-1 的 compose 重渲染，语义不变。

单点覆盖即全覆盖：`updateStyleSheet` 在整个 qfluentwidgets 里只被本模块的 `setTheme` /
`setThemeColor` 以**模块全局名**调用，`common/__init__` 也未 re-export 它。Python 在调用时才
解析全局名，故只要替换 `style_sheet.updateStyleSheet` 这一个模块属性，两个内部调用点自动走
补丁版。

升级 qfluentwidgets 时复核上游 `style_sheet.py` 的 `updateStyleSheet`：若 compose 不再自我
嵌套（上游已修），删除本模块。阶段 2（QSS 全局化）落地后本补丁被接管版吸收，届时本模块只作
阶段 2 未落地期的独立止血件保留。
"""

from __future__ import annotations

from qfluentwidgets.common import style_sheet as _style_sheet

_installed = False


def _patched_update_style_sheet(lazy: bool = False) -> None:
    """替换 qfw 的 `updateStyleSheet`：重渲染既有 compose，不再把它套壳。"""
    manager = _style_sheet.styleSheetManager
    removes = []
    for widget, source in list(manager.items()):
        try:
            if not (lazy and widget.visibleRegion().isNull()):
                # register=False：跳过 register(reset=True) 的套壳，按当前主题重渲染。
                # setStyleSheet 经模块属性取，以便阶段 2 覆盖它后本补丁自动跟随。
                _style_sheet.setStyleSheet(
                    widget, source, _style_sheet.qconfig.theme, register=False
                )
            else:
                # 懒分支只标脏；下次 paint 时 DirtyStyleSheetWatcher 从 depth-1 compose 重渲染。
                widget.setProperty("dirty-qss", True)
        except RuntimeError:
            # 控件已析构（原逻辑：先收集，循环后 deregister）。
            removes.append(widget)
    for widget in removes:
        manager.deregister(widget)


def install_theme_ratchet_fix() -> None:
    """覆盖 `style_sheet.updateStyleSheet`；须在任何控件创建前调用（幂等）。"""
    global _installed
    if _installed:
        return
    # 猴补丁：覆盖第三方模块属性，签名较原版更严（原 lazy 无注解），ty 视为不可赋值。
    _style_sheet.updateStyleSheet = _patched_update_style_sheet  # ty: ignore[invalid-assignment]
    _installed = True
