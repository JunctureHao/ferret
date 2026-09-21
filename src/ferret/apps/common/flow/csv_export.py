"""字段抽取导出（mitmproxy 命令行 `cut` 的 GUI 等效物，规格见 .plans/cut-csv-export.md）。

`cut` 的本质是「多选流量 → 按字段路径抽取 → 拼成 CSV」。这里把它落成 GUI：

- `CSV_FIELDS` 声明可选字段（界面名标记 + `flow_detail` 里的 dict key），
  一期字段全部已在 `core/mitm/detail.py::build_flow_detail` 的产出里，零 core 改动。
- `build_csv` 是纯函数（dict 列表 + 选中 key → CSV 文本），不碰 Qt，单测直接钉。
- `CsvFieldDialog` 是字段勾选对话框，两条出口（复制剪贴板 / 保存文件）。

AGENTS.md §3 红线：只读 `flow_detail` 返回的 dict，不碰活 flow、不给 master 挂 cut addon。
"""

from __future__ import annotations

import csv
import io
from typing import Any, NamedTuple

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QWidget
from qfluentwidgets import CheckBox, MessageBoxBase, PushButton, SubtitleLabel

from ferret.core.settings import CONFIG
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 翻译 context 一律写字面量 "CsvExport"：lupdate 静态扫描只认字面量，用变量转发
# 一层就提取不到（utils/i18n.py 的铁律）。字段名用 QT_TRANSLATE_NOOP 标记、到
# field_label 用 QCoreApplication.translate 求值，两处 context 必须逐字一致。


class CsvField(NamedTuple):
    """一个可导出字段：稳定 key（存配置、取值都用它）+ 界面名标记。

    `key` 同时是 `flow_detail` dict 的键 —— 抽取时 `detail.get(key)` 直接命中。
    `label` 是 `QT_TRANSLATE_NOOP` 标记，模块级不能求值（翻译器还没装，见
    utils/i18n.py），到使用点用 `field_label` 求值。
    """

    key: str
    label: str


# 一期字段清单：全部已在 flow_detail 里（core/mitm/detail.py:317,412）。
# 顺序即对话框网格顺序、也是 CSV 默认列序。key 是 detail dict 的键、不随语言变，
# 所以拿它存配置最稳（界面名会因语言不同而变）。
CSV_FIELDS: tuple[CsvField, ...] = (
    CsvField("Method", QT_TRANSLATE_NOOP("CsvExport", "方法")),
    CsvField("Host", QT_TRANSLATE_NOOP("CsvExport", "主机")),
    CsvField("Path", QT_TRANSLATE_NOOP("CsvExport", "路径")),
    CsvField("URL", QT_TRANSLATE_NOOP("CsvExport", "URL")),
    CsvField("Scheme", QT_TRANSLATE_NOOP("CsvExport", "Scheme")),
    CsvField("HTTP Version", QT_TRANSLATE_NOOP("CsvExport", "HTTP 版本")),
    CsvField("Status Code", QT_TRANSLATE_NOOP("CsvExport", "状态码")),
    CsvField("Reason", QT_TRANSLATE_NOOP("CsvExport", "原因短语")),
    CsvField(
        "Request Content-Type", QT_TRANSLATE_NOOP("CsvExport", "请求 Content-Type")
    ),
    CsvField(
        "Response Content-Type", QT_TRANSLATE_NOOP("CsvExport", "响应 Content-Type")
    ),
    CsvField("Server Address", QT_TRANSLATE_NOOP("CsvExport", "服务器地址")),
    CsvField("Protocol", QT_TRANSLATE_NOOP("CsvExport", "协议")),
    CsvField("duration_ms", QT_TRANSLATE_NOOP("CsvExport", "耗时(ms)")),
    CsvField("req_decoded_size", QT_TRANSLATE_NOOP("CsvExport", "请求体大小")),
    CsvField("res_decoded_size", QT_TRANSLATE_NOOP("CsvExport", "响应体大小")),
    CsvField("total_size", QT_TRANSLATE_NOOP("CsvExport", "总大小")),
    CsvField("comment", QT_TRANSLATE_NOOP("CsvExport", "备注")),
)

_FIELDS_BY_KEY: dict[str, CsvField] = {field.key: field for field in CSV_FIELDS}

# 默认勾选：抓包最常导出的六列。用户改过之后存进 CONFIG，下次沿用。
_DEFAULT_KEYS: tuple[str, ...] = (
    "Method",
    "Host",
    "Path",
    "Status Code",
    "duration_ms",
    "res_decoded_size",
)


def field_label(key: str) -> str:
    """字段 key → 当前语言的界面名。未知 key 回落到 key 本身（防御，正常不发生）。"""
    field = _FIELDS_BY_KEY.get(key)
    if field is None:
        return key
    return QCoreApplication.translate("CsvExport", field.label)


def _cell(value: Any) -> str:
    """一个字段值 → CSV 单元格文本。

    缺字段（未完成流量的 `duration_ms` 等）在 detail 里就没有这个键，`get` 拿到
    None —— 写空串，与 cut「missing 给空」语义一致。裸数字（大小 / 耗时）直接
    `str()`，对 Excel 更友好；耗时是 float，去掉无意义的小数尾巴。
    """
    if value is None:
        return ""
    if isinstance(value, float):
        # 142.0 → "142"，142.7 → "142.7"：整数值不留 ".0"，非整数保留。
        return str(int(value)) if value.is_integer() else str(value)
    return str(value)


def build_csv(details: list[dict[str, Any]], keys: list[str]) -> str:
    """详情字典列表 + 选中字段 key → UTF-8 CSV 文本（首行表头，一行一 flow）。

    纯函数：不碰 Qt、不读活 flow，只消费 `flow_detail` 产出的 dict。表头用当前
    语言的界面名（`field_label`），数据行按 `keys` 顺序取值。

    `io.StringIO(newline="")` + `csv.writer`：让 writer 自己控制行结束符，不被
    StringIO 的换行转换二次插入（同 mitmproxy cut.py 的姿态）。
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow([field_label(key) for key in keys])
    for detail in details:
        writer.writerow([_cell(detail.get(key)) for key in keys])
    return buffer.getvalue()


def load_selected_keys() -> list[str]:
    """从 CONFIG 读上次勾选的字段，滤掉已失效的 key；空了回落默认。

    存的是 detail key（跨语言稳定）。老配置里的字段若被本版删掉，这里静默丢弃，
    不让一条陈旧 key 把整张表带崩。
    """
    stored = CONFIG.get(CONFIG.csv_export_fields)
    if isinstance(stored, list):
        valid = [key for key in stored if key in _FIELDS_BY_KEY]
        if valid:
            return valid
    return list(_DEFAULT_KEYS)


def save_selected_keys(keys: list[str]) -> None:
    """把勾选写回 CONFIG。

    QConfig.set 开头 `if item.value == value: return`，原地 mutate 再 set 会静默
    不落盘（settings.py 里 block_list 那一族的坑）—— 传一个新 list。
    """
    CONFIG.set(CONFIG.csv_export_fields, list(keys))


class CsvFieldDialog(MessageBoxBase):
    """字段勾选对话框：网格复选 + 全选/全不选 + 复制剪贴板 / 保存文件两条出口。

    `result_action` 记录用户点了哪条出口（``"clip"`` / ``"save"``），调用方按它分流。
    默认的 yesButton 复用成「保存为文件…」，另插一枚「复制到剪贴板」按钮。
    """

    def __init__(self, flow_count: int, selected: list[str], parent: QWidget) -> None:
        super().__init__(parent)
        self.result_action: str = ""
        selected_set = set(selected)

        self.title_label = SubtitleLabel(self.tr("导出字段为 CSV"), self)
        self.hint_label = SubtitleLabel(self)
        self.hint_label.setText(
            self.tr("已选 {count} 条流量 · 勾选要导出的列").format(count=flow_count)
        )

        # 字段复选网格：三列铺开，顺序同 CSV_FIELDS。
        self.checks: dict[str, CheckBox] = {}
        grid_host = QWidget(self)
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        columns = 3
        for index, field in enumerate(CSV_FIELDS):
            check = CheckBox(field_label(field.key), grid_host)
            check.setChecked(field.key in selected_set)
            check.stateChanged.connect(self._refresh_buttons)
            self.checks[field.key] = check
            grid.addWidget(check, index // columns, index % columns)

        # 全选 / 全不选。
        self.select_all_button = PushButton(self.tr("全选"), self)
        self.clear_all_button = PushButton(self.tr("全不选"), self)
        self.select_all_button.clicked.connect(lambda: self._set_all(True))
        self.clear_all_button.clicked.connect(lambda: self._set_all(False))
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(0, 0, 0, 0)
        toggle_row.addWidget(self.select_all_button)
        toggle_row.addWidget(self.clear_all_button)
        toggle_row.addStretch(1)

        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.hint_label)
        self.viewLayout.addWidget(grid_host)
        self.viewLayout.addLayout(toggle_row)
        self.widget.setMinimumWidth(560)

        # yesButton = 保存文件；另插一枚复制按钮。两条出口都先记 action 再 accept，
        # exec() 返回后调用方读 result_action + selected_keys() 分流。
        self.yesButton.setText(self.tr("保存为文件…"))
        self.clip_button = PushButton(self.tr("复制到剪贴板"), self)
        self.clip_button.clicked.connect(self._on_clip)
        # 插在 yesButton 左侧：buttonLayout 尾部是 [yes, cancel]，塞在 yes 之前。
        self.buttonLayout.insertWidget(self.buttonLayout.count() - 2, self.clip_button)
        self.yesButton.clicked.connect(self._on_save)

        self._refresh_buttons()

    def selected_keys(self) -> list[str]:
        """按 CSV_FIELDS 的固定顺序返回勾选的 key —— 列序稳定，不随点击顺序跳。"""
        return [field.key for field in CSV_FIELDS if self.checks[field.key].isChecked()]

    def _set_all(self, checked: bool) -> None:
        for check in self.checks.values():
            check.setChecked(checked)

    def _refresh_buttons(self) -> None:
        """一个字段都没勾时两条出口都禁用 —— 空表没有导出的意义。"""
        any_checked = any(check.isChecked() for check in self.checks.values())
        self.yesButton.setEnabled(any_checked)
        self.clip_button.setEnabled(any_checked)

    def _on_clip(self) -> None:
        self.result_action = "clip"
        self.accept()

    def _on_save(self) -> None:
        self.result_action = "save"
        # yesButton 自带的 accept 已连；这里只补记 action。


__all__ = [
    "CSV_FIELDS",
    "CsvField",
    "CsvFieldDialog",
    "build_csv",
    "field_label",
    "load_selected_keys",
    "save_selected_keys",
]
