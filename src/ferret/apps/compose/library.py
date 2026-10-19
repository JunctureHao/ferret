"""Compose 已保存请求库面板：搜索 + 单层文件夹树 + 底部新建工具行。

面板只发意图信号（``open_requested`` 带 request id、``create_requested``），
表单装载与脏检查由 ComposeInterface 负责；store 的 ``changed`` 驱动刷新，
搜索词、选中请求与文件夹展开状态在刷新间保留。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTreeView,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    IconWidget,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    RoundMenu,
    SearchLineEdit,
    SubtitleLabel,
    ToolButton,
    ToolTipFilter,
    TreeWidget,
    setCustomStyleSheet,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.compose.models import SavedRequest, duplicate
from ferret.apps.compose.store import ComposeRequestStore

# UserRole 存 ("request", id) / ("folder", 名称)。
_ROLE = Qt.ItemDataRole.UserRole


class _RequestTree(TreeWidget):
    """紧凑的单列请求树，分支绘制与点击区域使用同一套缩进。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setIndentation(16)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setWordWrap(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # 默认样式另加 20px 行内留白，会和两级树缩进叠加挤占请求名称。
        qss = "QTreeView::item { padding-left: 4px; }"
        setCustomStyleSheet(self, qss, qss)

    def drawBranches(self, painter, rect, index) -> None:
        # 与 FlowConnTree 一样，绕过 QFW 固定的分支偏移，保留 Fluent 样式。
        QTreeView.drawBranches(self, painter, rect, index)

    def viewportEvent(self, event: QEvent) -> bool:
        return QTreeView.viewportEvent(self, event)


class _NameDialog(MessageBoxBase):
    """新建文件夹和重命名共用的名字输入框。"""

    def __init__(self, title: str, parent: QWidget, *, name: str = ""):
        super().__init__(parent)
        self.name_edit = LineEdit(self)
        self.name_edit.setText(name)
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        self.viewLayout.addWidget(self.name_edit)
        self.widget.setMinimumWidth(360)
        self.yesButton.setText(self.tr("确定"))
        self.cancelButton.setText(self.tr("取消"))
        self.name_edit.textChanged.connect(
            lambda text: self.yesButton.setEnabled(bool(text.strip()))
        )
        self.yesButton.setEnabled(bool(name.strip()))

    def name(self) -> str:
        return self.name_edit.text().strip()


class ComposeLibraryPanel(QWidget):
    """已保存请求库：文件夹单层分组，未分组请求平铺在根级。"""

    open_requested = Signal(str)
    create_requested = Signal()

    def __init__(self, store: ComposeRequestStore, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("ComposeLibraryPanel")
        self._store = store
        self._current_id: str | None = None
        self._dirty = False
        self._request_items: dict[str, QTreeWidgetItem] = {}
        self._expanded_folders: set[str] | None = None
        self._last_query = ""
        self._context_menu: RoundMenu | None = None
        self._move_menu: RoundMenu | None = None

        self.search_edit = SearchLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("搜索请求"))

        self.tree = _RequestTree(self)

        # 与 FlowEmptyState 同一层级的空态：小图标 + 标题 + 说明。
        self.empty_hint = QWidget(self)
        empty_icon = IconWidget(FluentIcon.BOOK_SHELF, self.empty_hint)
        empty_icon.setFixedSize(32, 32)
        self.empty_title = BodyLabel(self.tr("暂无已保存请求"), self.empty_hint)
        self.empty_subtitle = CaptionLabel(
            self.tr("编辑请求后点击顶栏「保存」"), self.empty_hint
        )
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle.setWordWrap(True)
        empty_layout = QVBoxLayout(self.empty_hint)
        empty_layout.setContentsMargins(16, 16, 16, 16)
        empty_layout.addStretch(1)
        empty_layout.addWidget(empty_icon, 0, Qt.AlignmentFlag.AlignCenter)
        empty_layout.addSpacing(8)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_subtitle)
        empty_layout.addStretch(1)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.empty_hint)

        self.new_folder_btn = ToolButton(FluentIcon.FOLDER_ADD, self)
        self.new_folder_btn.setToolTip(self.tr("新建文件夹"))
        self.new_folder_btn.installEventFilter(ToolTipFilter(self.new_folder_btn, 700))
        self.new_request_btn = ToolButton(FluentIcon.ADD, self)
        self.new_request_btn.setToolTip(self.tr("新建请求"))
        self.new_request_btn.installEventFilter(
            ToolTipFilter(self.new_request_btn, 700)
        )
        tool_row = QHBoxLayout()
        tool_row.setContentsMargins(0, 0, 0, 0)
        tool_row.setSpacing(4)
        tool_row.addWidget(self.new_folder_btn)
        tool_row.addWidget(self.new_request_btn)
        tool_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.stack, 1)
        layout.addLayout(tool_row)

        self.search_edit.textChanged.connect(self.refresh)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.new_folder_btn.clicked.connect(self._prompt_new_folder)
        self.new_request_btn.clicked.connect(self.create_requested.emit)
        self._store.changed.connect(self.refresh)

        self.refresh()

    # ── 树构建 ────────────────────────────────

    def refresh(self) -> None:
        """按 store 当前内容重建；保留搜索词，文件夹展开状态尽力保留。"""
        if self._expanded_folders is None:
            expanded: set[str] | None = None  # 首次构建默认全展开
        elif not self._last_query:
            expanded = set(self._expanded_folders)
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                if item is None:
                    continue
                data = item.data(0, _ROLE)
                if data and data[0] == "folder":
                    if item.isExpanded():
                        expanded.add(data[1])
                    else:
                        expanded.discard(data[1])
        else:
            expanded = set(self._expanded_folders)
        query = self.search_edit.text().strip().lower()
        self._last_query = query
        requests = [
            request
            for request in self._store.requests()
            if not query
            or query in request.name.lower()
            or query in request.url.lower()
            or query in request.method.lower()
        ]
        folders = self._store.folders()
        self._expanded_folders = set(folders) if expanded is None else expanded
        self._request_items.clear()
        self.tree.clear()
        for folder in folders:
            children = [request for request in requests if request.folder == folder]
            if query and not children:
                continue
            item = QTreeWidgetItem()
            item.setText(0, folder)
            item.setToolTip(0, folder)
            # icon() 固定创建时的颜色；qicon() 随主题绘制，避免切换后白图标融入浅色背景。
            item.setIcon(0, FluentIcon.FOLDER.qicon())
            item.setData(0, _ROLE, ("folder", folder))
            self.tree.addTopLevelItem(item)
            for request in children:
                item.addChild(self._make_request_item(request))
            item.setExpanded(bool(query) or folder in self._expanded_folders)
        for request in requests:
            if not request.folder or request.folder not in folders:
                self.tree.addTopLevelItem(self._make_request_item(request))
        self.set_current_request(self._current_id, dirty=self._dirty)
        self.empty_title.setText(
            self.tr("没有匹配的请求") if query else self.tr("暂无已保存请求")
        )
        self.empty_subtitle.setText(
            self.tr("试试其他名称、方法或 URL")
            if query
            else self.tr("编辑请求后点击顶栏「保存」")
        )
        self.stack.setCurrentWidget(
            self.tree if self.tree.topLevelItemCount() else self.empty_hint
        )

    def _make_request_item(self, request: SavedRequest) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setText(0, f"{request.method}  {request.name}")
        item.setData(0, _ROLE, ("request", request.id))
        item.setToolTip(0, f"{request.method}  {request.name}\n{request.url}")
        self._request_items[request.id] = item
        return item

    def set_current_request(
        self, request_id: str | None, *, dirty: bool = False
    ) -> None:
        previous_id = self._current_id
        self._current_id, self._dirty = request_id, dirty
        for key in (previous_id, request_id):
            item = self._request_items.get(key) if key is not None else None
            request = self._store.get(key) if key is not None else None
            if item is not None and request is not None:
                marker = " *" if key == request_id and dirty else ""
                item.setText(0, f"{request.method}  {request.name}{marker}")
        current = (
            self._request_items.get(request_id) if request_id is not None else None
        )
        if current is None:
            self.tree.setCurrentIndex(QModelIndex())
            self.tree.clearSelection()
        else:
            self.tree.setCurrentItem(current)

    # ── 交互 ──────────────────────────────────

    @Slot(QTreeWidgetItem, int)
    def _on_item_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        data = item.data(0, _ROLE)
        if data and data[0] == "request":
            self.open_requested.emit(data[1])

    @Slot(QTreeWidgetItem, int)
    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        if item.data(0, _ROLE):
            self._start_rename(item)

    def _start_rename(self, item: QTreeWidgetItem) -> None:
        data = item.data(0, _ROLE)
        if not data:
            return
        kind, key = data
        request = self._store.get(key) if kind == "request" else None
        old = request.name if request is not None else key
        dialog = _NameDialog(self.tr("重命名"), self.window(), name=old)

        def rename() -> None:
            name = dialog.name()
            if name and name != old:
                if kind == "request":
                    current = self._store.get(key)
                    if current is not None:
                        self._store.upsert(current.touched(name=name))
                else:
                    self._store.rename_folder(key, name)

        dialog.accepted.connect(rename)
        self._show_dialog(dialog)

    @Slot()
    def _prompt_new_folder(self) -> None:
        dialog = _NameDialog(self.tr("新建文件夹"), self.window())
        dialog.accepted.connect(lambda: self._store.add_folder(dialog.name()))
        self._show_dialog(dialog)

    @staticmethod
    def _show_dialog(dialog: MessageBoxBase | MessageBox) -> None:
        # 不在菜单鼠标事件里嵌套 exec，让点击立即退栈再处理弹窗交互。
        dialog.finished.connect(dialog.deleteLater)
        dialog.setModal(True)
        dialog.show()

    # ── 右键菜单 ───────────────────────────────

    @Slot(object)
    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        # Qt 的弹出窗口状态可能跨一次开关保留引用；复用菜单窗口，只更新动作。
        if self._context_menu is None:
            self._context_menu = RoundMenu(parent=self)
        menu = self._context_menu
        self._clear_menu_actions(menu)
        if item is None:
            folder_action = BaseAction(
                FluentIcon.FOLDER_ADD, self.tr("新建文件夹"), menu
            )
            folder_action.triggered.connect(self._prompt_new_folder)
            menu.addAction(folder_action)
            request_action = BaseAction(FluentIcon.ADD, self.tr("新建请求"), menu)
            # 表单的脏状态确认仍可能执行 exec，也须等菜单点击退栈后再发意图。
            request_action.triggered.connect(
                lambda: QTimer.singleShot(0, self, self.create_requested.emit)
            )
            menu.addAction(request_action)
        else:
            data = item.data(0, _ROLE)
            if data and data[0] == "request":
                self._build_request_menu(menu, data[1], item)
            elif data:
                self._build_folder_menu(menu, data[1], item)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _clear_menu_actions(menu: RoundMenu) -> None:
        menu.timer.stop()
        menu.lastHoverItem = None
        menu.lastHoverSubMenuItem = None
        actions = list(menu.menuActions())
        menu.clear()
        # RoundMenu.clear() 只移除动作和子菜单，分隔线需一并清掉。
        menu.view.clear()
        for action in actions:
            action.deleteLater()

    def _build_request_menu(
        self, menu: RoundMenu, request_id: str, item: QTreeWidgetItem
    ) -> None:
        open_action = BaseAction(FluentIcon.VIEW, self.tr("打开"), menu)
        open_action.triggered.connect(
            lambda: QTimer.singleShot(
                0, self, lambda: self.open_requested.emit(request_id)
            )
        )
        menu.addAction(open_action)
        rename_action = BaseAction(FluentIcon.EDIT, self.tr("重命名"), menu)
        rename_action.triggered.connect(lambda: self._start_rename(item))
        menu.addAction(rename_action)
        if self._move_menu is None:
            self._move_menu = RoundMenu(self.tr("移动到…"), menu)
        move_menu = self._move_menu
        self._clear_menu_actions(move_menu)
        move_menu.setIcon(FluentIcon.MOVE)
        ungrouped = BaseAction(None, self.tr("未分组"), move_menu)
        ungrouped.triggered.connect(lambda: self._move_request(request_id, ""))
        move_menu.addAction(ungrouped)
        for folder in self._store.folders():
            action = BaseAction(None, folder, move_menu)
            action.triggered.connect(
                lambda checked=False, target=folder: self._move_request(
                    request_id, target
                )
            )
            move_menu.addAction(action)
        menu.addMenu(move_menu)
        copy_action = BaseAction(FluentIcon.COPY, self.tr("复制副本"), menu)
        copy_action.triggered.connect(lambda: self._duplicate_request(request_id))
        menu.addAction(copy_action)
        menu.addSeparator()
        delete_action = BaseAction(FluentIcon.DELETE, self.tr("删除"), menu)
        delete_action.triggered.connect(lambda: self._delete_request(request_id))
        menu.addAction(delete_action)

    def _build_folder_menu(
        self, menu: RoundMenu, folder: str, item: QTreeWidgetItem
    ) -> None:
        rename_action = BaseAction(FluentIcon.EDIT, self.tr("重命名"), menu)
        rename_action.triggered.connect(lambda: self._start_rename(item))
        menu.addAction(rename_action)
        delete_action = BaseAction(FluentIcon.DELETE, self.tr("删除文件夹"), menu)
        delete_action.triggered.connect(lambda: self._delete_folder(folder))
        menu.addAction(delete_action)

    # ── store 操作 ─────────────────────────────

    def _move_request(self, request_id: str, folder: str) -> None:
        request = self._store.get(request_id)
        if request is not None and request.folder != folder:
            self._store.upsert(request.touched(folder=folder))

    def _duplicate_request(self, request_id: str) -> None:
        request = self._store.get(request_id)
        if request is not None:
            self._store.upsert(
                duplicate(request, name=self.tr("{}（副本）").format(request.name))
            )

    def _delete_request(self, request_id: str) -> None:
        request = self._store.get(request_id)
        if request is None:
            return
        box = MessageBox(
            self.tr("删除请求"),
            self.tr("确定删除「{}」？此操作不可撤销。").format(request.name),
            self.window(),
        )
        box.accepted.connect(lambda: self._store.remove(request_id))
        self._show_dialog(box)

    def _delete_folder(self, folder: str) -> None:
        box = MessageBox(
            self.tr("删除文件夹"),
            self.tr("删除文件夹「{}」？其中的请求会移到未分组。").format(folder),
            self.window(),
        )
        box.accepted.connect(lambda: self._store.remove_folder(folder))
        self._show_dialog(box)
