"""流列表列定义与布局归一化（.plans/0-flow-list-columns.md）。

本模块是列自定义的**唯一事实源**，刻意保持轻：只 import 标准库与 QtCore 级的
翻译标记，**不** import `models.py` / `core.mitm`（那条链拖进整个 mitmproxy）。
因此归一化纯函数可在 `tests/apps/` 直接调，不必起 QApplication（§0/§4.1）。

两个标识分开，别混：

- **稳定 key**（`index` / `mark` / …）：存配置、跨语言稳定、版本迁移都用它。
- **header 分派串**（`#` / `Mark` / `Method` / …）：`_headers` 里的字符串，
  `flow_cell` / `_conn_data` 按它分派渲染。逻辑列顺序＝`HEADERS` 顺序恒定不动，
  所以 key→逻辑列是一张常量表（`logical_index`）。

显示标题走 `column_display_title`：context 钉死 "FlowTableModel"，只有 `mark`
译成「标记」、其余列头用 header 原文 —— 与 `headerData` 同一条翻译路径（§4.4），
列设置对话框和表头必须复用它，不另起 context。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from PySide6.QtCore import QCoreApplication

from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 配置结构版本。错误版本整体回落默认（见 normalize）——v1 只此一档。
SCHEMA_VERSION = 1

# 归一化时的宽度下限：低于此值视为脏数据、回落该列默认宽度（§3.2）。
_MIN_WIDTH = 30


class ColumnDef(NamedTuple):
    """一列的静态定义。`title_marker` 为 None 时显示标题＝header 原文。"""

    key: str  # 稳定 key，存配置
    header: str  # _headers 分派串，flow_cell/_conn_data 按它分派
    title_marker: str | None  # 显示标题标记（QT_TRANSLATE_NOOP，context FlowTableModel）
    default_visible: bool
    default_width: int
    required: bool  # 必需列：不可隐藏
    pinned: bool  # 固定视觉最左、不可移动（仅 index：连接树装饰绑逻辑列 0）
    fixed_width: bool  # 视图侧 Fixed resize、宽度钉死，widths 配置对它无效（仅 mark）


# 默认顺序＝HEADERS 顺序＝逻辑列顺序，三者恒等。改这里等于改默认布局。
COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("index", "#", None, True, 80, required=True, pinned=True, fixed_width=False),
    ColumnDef(
        "mark",
        "Mark",
        QT_TRANSLATE_NOOP("FlowTableModel", "标记"),
        True,
        64,
        required=False,
        pinned=False,
        fixed_width=True,
    ),
    ColumnDef("method", "Method", None, True, 80, required=True, pinned=False, fixed_width=False),
    ColumnDef("url", "URL", None, True, 420, required=True, pinned=False, fixed_width=False),
    ColumnDef("status", "Status", None, True, 65, required=False, pinned=False, fixed_width=False),
    ColumnDef("type", "Type", None, True, 100, required=False, pinned=False, fixed_width=False),
    ColumnDef("size", "Size", None, True, 80, required=False, pinned=False, fixed_width=False),
    ColumnDef("time", "Time", None, True, 80, required=False, pinned=False, fixed_width=False),
)

_BY_KEY: dict[str, ColumnDef] = {col.key: col for col in COLUMNS}
_BY_HEADER: dict[str, ColumnDef] = {col.header: col for col in COLUMNS}

DEFAULT_ORDER: tuple[str, ...] = tuple(col.key for col in COLUMNS)
REQUIRED_KEYS: frozenset[str] = frozenset(col.key for col in COLUMNS if col.required)
# 响应式窄窗优先隐藏的列（原 setColumnHidden(4/5)＝Status/Type，按 key 固定）。
RESPONSIVE_KEYS: tuple[str, ...] = ("status", "type")


def header_of(key: str) -> str:
    """稳定 key → _headers 分派串。"""
    return _BY_KEY[key].header


def key_of_header(header: str) -> str:
    """_headers 分派串 → 稳定 key。"""
    return _BY_HEADER[header].key


def logical_index(key: str) -> int:
    """稳定 key → 逻辑列索引（恒定：＝HEADERS 里的位置，moveSection 不改逻辑列）。"""
    return DEFAULT_ORDER.index(key)


def is_pinned(key: str) -> bool:
    return _BY_KEY[key].pinned


def is_fixed_width(key: str) -> bool:
    return _BY_KEY[key].fixed_width


def default_width(key: str) -> int:
    return _BY_KEY[key].default_width


def column_display_title(key: str) -> str:
    """稳定 key → 当前语言的显示标题。与 headerData 同一条翻译路径（§4.4）。"""
    col = _BY_KEY.get(key)
    if col is None:
        return key
    if col.title_marker is not None:
        return QCoreApplication.translate("FlowTableModel", col.title_marker)
    return col.header


@dataclass(frozen=True)
class ColumnLayout:
    """一份已归一化、必然自洽的列布局。frozen＝改动一律产出新对象（§3.2 写回红线）。

    - `order`：全部已知列的稳定 key，index 恒在首位。
    - `visible`：可见列集（必含全部必需列，至少非空）。
    - `widths`：非固定宽列的宽度（mark 不入表）。
    """

    order: tuple[str, ...]
    visible: frozenset[str]
    widths: tuple[tuple[str, int], ...]  # 有序 tuple 便于 frozen；对外用 width()

    def is_visible(self, key: str) -> bool:
        return key in self.visible

    def visible_in_order(self) -> list[str]:
        return [k for k in self.order if k in self.visible]

    def width(self, key: str) -> int:
        for k, w in self.widths:
            if k == key:
                return w
        return default_width(key)

    def widths_dict(self) -> dict[str, int]:
        return dict(self.widths)

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "order": list(self.order),
            "visible": self.visible_in_order(),
            "widths": self.widths_dict(),
        }

    def with_visible(self, key: str, show: bool) -> ColumnLayout:
        """显示/隐藏一列并重新归一化（必需列不可隐藏，静默忽略）。"""
        raw = self.to_dict()
        vis = set(raw["visible"])
        if show:
            vis.add(key)
        else:
            vis.discard(key)
        raw["visible"] = [k for k in raw["order"] if k in vis]
        return normalize(raw)

    def with_order(self, order: list[str]) -> ColumnLayout:
        raw = self.to_dict()
        raw["order"] = list(order)
        return normalize(raw)

    def with_width(self, key: str, value: int) -> ColumnLayout:
        raw = self.to_dict()
        widths = dict(raw["widths"])
        widths[key] = int(value)
        raw["widths"] = widths
        return normalize(raw)


def default_layout() -> ColumnLayout:
    """出厂布局：默认顺序、默认可见、默认宽度。"""
    return ColumnLayout(
        order=DEFAULT_ORDER,
        visible=frozenset(col.key for col in COLUMNS if col.default_visible),
        widths=tuple(
            (col.key, col.default_width) for col in COLUMNS if not col.fixed_width
        ),
    )


def normalize(raw: object) -> ColumnLayout:
    """把任意持久化产物（或 None / 脏数据）收敛成自洽的 ColumnLayout。

    容错口径见 §3.2：未配置 / 错误版本回落默认；未知 key 忽略；缺失的新列按默认
    位置与默认可见性追加、不覆盖用户已有列；order 去重补齐、index 强制首位；
    visible 与 order 求交后强制加必需列；非法/过小宽度回落默认；mark 宽度忽略。
    """
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        # 未配置（{}）或版本对不上：整体回落，不拿陈旧结构冒险。
        return default_layout()

    order_raw = raw.get("order")
    known: set[str] = set()  # 用户旧配置**认识**的列（决定新列是否走默认可见）
    order: list[str] = []
    if isinstance(order_raw, list):
        for key in order_raw:
            if key in _BY_KEY and key not in known:
                known.add(key)
                order.append(key)
    # 缺失的新列按默认顺序追加到末尾（不打乱用户已排的部分）。
    for col in COLUMNS:
        if col.key not in known:
            order.append(col.key)
    # index 钉死视觉/逻辑首位（连接树装饰绑逻辑列 0，见 §0/§1）。
    if "index" in order:
        order = ["index", *[k for k in order if k != "index"]]

    visible_raw = raw.get("visible")
    visible_set = (
        {k for k in visible_raw if k in _BY_KEY}
        if isinstance(visible_raw, list)
        else None
    )
    visible: set[str] = set()
    for key in order:
        if visible_set is None:
            # 无 visible 段：全按默认可见。
            if _BY_KEY[key].default_visible:
                visible.add(key)
        elif key in known:
            # 用户旧配置认识这列：尊重它当时的显隐。
            if key in visible_set:
                visible.add(key)
        elif _BY_KEY[key].default_visible:
            # 新列：用默认可见性，不被旧 visible 段误判为隐藏。
            visible.add(key)
    visible |= REQUIRED_KEYS  # 必需列强制可见，杜绝空布局

    widths_raw = raw.get("widths")
    widths: list[tuple[str, int]] = []
    for col in COLUMNS:
        if col.fixed_width:
            continue  # mark：Fixed resize，宽度配置无效
        value = col.default_width
        if isinstance(widths_raw, dict):
            stored = widths_raw.get(col.key)
            if isinstance(stored, (int, float)) and stored >= _MIN_WIDTH:
                value = int(stored)
        widths.append((col.key, value))

    return ColumnLayout(
        order=tuple(order), visible=frozenset(visible), widths=tuple(widths)
    )


def load_layout() -> ColumnLayout:
    """从 CONFIG 读并归一化列布局；未配置 / 脏数据回落默认（见 normalize）。"""
    from ferret.core.settings import CONFIG

    return normalize(CONFIG.get(CONFIG.flow_columns))


def save_layout(layout: ColumnLayout) -> None:
    """把布局写回 CONFIG。

    QConfig.set 开头 `if item.value == value: return`，原地 mutate 再 set 会静默
    不落盘（settings.py 里 block_list 那一族的坑）—— to_dict 每次都是新 dict。
    """
    from ferret.core.settings import CONFIG

    CONFIG.set(CONFIG.flow_columns, layout.to_dict())


__all__ = [
    "COLUMNS",
    "DEFAULT_ORDER",
    "REQUIRED_KEYS",
    "RESPONSIVE_KEYS",
    "SCHEMA_VERSION",
    "ColumnDef",
    "ColumnLayout",
    "column_display_title",
    "default_layout",
    "default_width",
    "header_of",
    "is_fixed_width",
    "is_pinned",
    "key_of_header",
    "load_layout",
    "logical_index",
    "normalize",
    "save_layout",
]
