"""Capture-specific services.

Shared mitmproxy runtime, flow I/O, and export helpers live in
``ferret.core.mitm``. This module only contains behavior owned by the capture
application.
"""

from ferret.core.mitm import parse_filter


def build_filter_expression(raw: str = "") -> str:
    """Wrap a user-authored flowfilter expression with the ``~http`` base.

    过滤面板只产出**一条**原生 flowfilter 表达式（`.plans/0-filter-redesign.
    expression-first.md`）：字段/逻辑/值那套弱结构化模型已退役，表达式即唯一事实源。

    ``~http`` 底座不许丢：View 的基础过滤器（`runtime.py`）也以它开头，少了它 tcp/udp
    流量会直接涌进表格。raw 非空时整体加括号作最后一个原子拼入——flowfilter 里并列会
    攥住 `|`，不加括号 `~http & a | b` 的优先级会把整串段拧错（AGENTS.md §5）。

    括号**内两侧留空格**：原生词法里不带参数的选择器（`~websocket` / `~marked` / `~q`）
    的匹配会贪进紧挨的 `)`，`(~websocket)` 直接解析失败，`( ~websocket )` 才过。
    """
    raw = raw.strip()
    if not raw:
        return "~http"
    return f"~http & ( {raw} )"


def compile_filter(raw: str = ""):
    """Compile the capture filter expression into a mitmproxy matcher."""
    return parse_filter(build_filter_expression(raw))
