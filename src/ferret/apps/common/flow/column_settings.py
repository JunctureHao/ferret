"""流列表「列设置」对话框（.plans/0-flow-list-columns.md §2.1 / §4.2）。

一个 `MessageBoxBase`：勾选决定显隐、拖拽决定顺序、「恢复默认列」一键回落。产出经
`columns.normalize` 收敛，所以对话框侧不必自己保证「必需列不被隐藏 / index 居首」——
这些不变量由归一化兜底（§3.2）。UI 只负责表达用户意图。

- 显示标题走 `column_display_title`：与 `headerData` 同一条翻译路径（context 钉死
  "FlowTableModel"，§4.4），列头译文不另起 context。
- 必需列（index/method/url）复选框禁用、恒勾选；`index` 额外不可拖动（连接树装饰绑
  逻辑列 0，§0）。可选列可勾可拖。
- 列宽不在此配置（表头拖动实时存），`result_layout` 沿用传入布局的 widths。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QListWidgetItem, QWidget
from qfluentwidgets import (
    BodyLabel,
    ListWidget,
    MessageBoxBase,
    PushButton,
    SubtitleLabel,
)

from ferret.apps.common.flow.columns import (
    REQUIRED_KEYS,
    ColumnLayout,
    column_display_title,
    default_layout,
    is_pinned,
    normalize,
)

_KEY_ROLE = Qt.ItemDataRole.UserRole


class ColumnSettingsDialog(MessageBoxBase):
    """列显隐 + 顺序设置。`result_layout()` 在 accept 后读当前列表状态归一化产出。"""

    def __init__(self, layout: ColumnLayout, parent: QWidget) -> None:
        super().__init__(parent)
        self._base = layout

        self.title_label = SubtitleLabel(self.tr("列设置"), self)
        self.hint_label = BodyLabel(
            self.tr("勾选要显示的列，拖拽调整顺序（必需列不可隐藏）。"), self
        )

        # 顺序 = 列表自上而下；显隐 = 勾选态。InternalMove 让用户拖动重排。
        self.list_widget = ListWidget(self)
        self.list_widget.setDragDropMode(
            QAbstractItemView.DragDropMode.InternalMove
        )
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._populate(layout)

        self.reset_button = PushButton(self.tr("恢复默认列"), self)
        self.reset_button.clicked.connect(self._reset)

        self.viewLayout.addWidget(self.title_label)
        self.viewLayout.addWidget(self.hint_label)
        self.viewLayout.addWidget(self.list_widget)
        self.viewLayout.addWidget(self.reset_button)
        self.widget.setMinimumWidth(360)
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))

    def _populate(self, layout: ColumnLayout) -> None:
        """按布局顺序铺列表项，逐项设显示标题 / 勾选 / 可拖可勾标志。"""
        self.list_widget.clear()
        for key in layout.order:
            item = QListWidgetItem(column_display_title(key))
            item.setData(_KEY_ROLE, key)
            required = key in REQUIRED_KEYS
            checked = required or layout.is_visible(key)
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            )
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if not required:  # 必需列复选框禁用（恒勾选），其余可勾
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            if not is_pinned(key):  # index 钉死首位、不可拖（连接树装饰绑逻辑列 0）
                flags |= Qt.ItemFlag.ItemIsDragEnabled
            item.setFlags(flags)
            self.list_widget.addItem(item)

    def _reset(self) -> None:
        self._populate(default_layout())

    def result_layout(self) -> ColumnLayout:
        """读当前列表：顺序＝自上而下，可见＝勾选态；沿用原 widths，交 normalize 收敛。

        归一化会强制 index 居首、必需列可见、补齐缺列，所以这里不必自己守这些不变量。
        """
        order: list[str] = []
        visible: list[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            key = item.data(_KEY_ROLE)
            order.append(key)
            if item.checkState() == Qt.CheckState.Checked:
                visible.append(key)
        raw = self._base.to_dict()
        raw["order"] = order
        raw["visible"] = visible
        return normalize(raw)


__all__ = ["ColumnSettingsDialog"]
