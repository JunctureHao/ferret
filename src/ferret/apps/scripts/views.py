"""Scripts interface: entry list on top, source/error detail below."""

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
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
    PushButton,
    RoundMenu,
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success, show_warning
from ferret.apps.common.splitter import BaseSplitter
from ferret.apps.scripts.controllers import ScriptsController
from ferret.apps.scripts.dialogs import NewScriptDialog, ScriptRemoveDialog
from ferret.apps.scripts.editors import ScriptEditorPanel
from ferret.apps.scripts.models import (
    ScriptFilterProxyModel,
    ScriptTableModel,
    script_name,
    trust_warning,
)
from ferret.core.mitm import SCRIPT_ORIGIN_NEW, ScriptEntry


class ScriptsInterface(QWidget):
    """用户脚本页：上表（清单）下详情（正文 + 加载错误）。

    行序＝执行序（`FerretScriptAddon.addons` 按清单序返回脚本 ns），所以表不开
    排序，换序走拖拽与右键的上移/下移 —— 两个入口同一个 `move_script_to`。
    """

    def __init__(self, controller: ScriptsController, parent=None):
        super().__init__(parent)
        self.setObjectName("ScriptsInterface")
        self.controller = controller
        # 整表 reset 会清掉选中并发 selectionChanged，重填期间必须哑掉详情联动，
        # 否则勾一下「启用」就被当成「切换到空选中」，正在编辑的正文会被问一遍存不存。
        self._restoring = False

        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._on_scripts_changed(self.controller.scripts)

    # --- 构造 ---

    def __init_widget(self):
        self.toolbar = self.__build_toolbar()
        self.warning_bar = self.__build_warning_bar()

        self.source_model = ScriptTableModel(self)
        self.proxy_model = ScriptFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)

        self.table = TableView(self)
        self.table.verticalHeader().hide()
        self.table.setModel(self.proxy_model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        widths = [60, 180, 80, 120, 320]
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)
        header.setSectionResizeMode(len(widths) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        # 行内拖拽换序。模型的 dropMimeData 刻意返回 False（换序由控制器整批下发），
        # 所以 view 只负责画落点指示器。
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setDefaultDropAction(Qt.DropAction.MoveAction)

        self.empty_page = self.__build_empty_page()
        self.content_stack = QStackedWidget(self)
        self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.empty_page)

        self.panel = ScriptEditorPanel(self)

    def __build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.import_btn = PushButton(FluentIcon.FOLDER_ADD, self.tr("导入脚本"), bar)
        self.import_btn.setToolTip(self.tr("引用磁盘上现有的 .py 文件，不复制"))
        self.new_btn = PushButton(FluentIcon.ADD, self.tr("新建脚本"), bar)

        self.search_edit = LineEdit(bar)
        self.search_edit.setPlaceholderText(self.tr("搜索脚本"))
        self.search_edit.setFixedHeight(32)
        self.search_edit.setClearButtonEnabled(True)

        self.reload_btn = TransparentToolButton(FluentIcon.SYNC, bar)
        self.reload_btn.setFixedSize(32, 32)
        self.reload_btn.setIconSize(QSize(18, 18))
        self.reload_btn.setToolTip(self.tr("重载"))
        self.reload_btn.setEnabled(False)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, bar)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setIconSize(QSize(18, 18))
        self.delete_btn.setToolTip(self.tr("移除"))
        self.delete_btn.setEnabled(False)

        layout.addWidget(self.import_btn)
        layout.addWidget(self.new_btn)
        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.reload_btn)
        layout.addWidget(self.delete_btn)
        return bar

    def __build_warning_bar(self) -> QWidget:
        """常驻提示 —— 导入走系统文件对话框，警告没地方挂，只能常驻在页上。"""
        bar = QWidget(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 6)
        layout.setSpacing(6)
        icon = IconWidget(FluentIcon.INFO, bar)
        icon.setFixedSize(14, 14)
        label = CaptionLabel(trust_warning(), bar)
        label.setWordWrap(True)
        layout.addWidget(icon)
        layout.addWidget(label, 1)
        return bar

    def __build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("暂无脚本"), page)
        hint = CaptionLabel(
            self.tr(
                "脚本按 mitmproxy 的钩子模型参与流量处理，写 request/response 等函数即可"
            ),
            page,
        )
        buttons = QWidget(page)
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(0, 0, 0, 0)
        buttons_layout.setSpacing(8)
        import_btn = PushButton(FluentIcon.FOLDER_ADD, self.tr("导入脚本"), buttons)
        new_btn = PushButton(FluentIcon.ADD, self.tr("新建脚本"), buttons)
        buttons_layout.addWidget(import_btn)
        buttons_layout.addWidget(new_btn)

        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(buttons, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)

        import_btn.clicked.connect(self._on_import)
        new_btn.clicked.connect(self._on_new)
        return page

    def __init_layout(self):
        self.splitter = BaseSplitter(Qt.Orientation.Vertical, self)
        self.splitter.addWidget(self.content_stack)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([240, 360])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.warning_bar)
        layout.addWidget(self.splitter, 1)

    def __connect_signal_to_slot(self):
        self.import_btn.clicked.connect(self._on_import)
        self.new_btn.clicked.connect(self._on_new)
        self.reload_btn.clicked.connect(self._on_reload_selected)
        self.delete_btn.clicked.connect(self._on_remove)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.table.doubleClicked.connect(self._on_row_activated)

        self.source_model.enabled_toggled.connect(self.controller.set_enabled)
        self.source_model.rows_moved.connect(self.controller.move_script_to)

        self.panel.save_requested.connect(self._on_save)
        self.panel.save_as_requested.connect(self._on_save_as)
        self.panel.reload_requested.connect(self.controller.reload_script)
        self.panel.open_external_requested.connect(self._on_open_external)

        self.controller.scripts_changed.connect(self._on_scripts_changed)
        self.controller.statuses_changed.connect(self._on_statuses_changed)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.search_edit.setFocus()
        )
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table).activated.connect(
            self._on_remove
        )

    # --- helpers ---

    def _selected_rows(self) -> list[int]:
        rows = self.table.selectionModel().selectedRows()
        return sorted(self.proxy_model.mapToSource(index).row() for index in rows)

    def _selected_entries(self) -> list[ScriptEntry]:
        entries = [self.controller.script_at(row) for row in self._selected_rows()]
        return [entry for entry in entries if entry is not None]

    def _focus_path(self) -> str:
        """重填清单后要保住的那条：优先详情面板正在看的，其次唯一的选中行。"""
        entry = self.panel.entry
        if entry is not None:
            return entry.path
        entries = self._selected_entries()
        return entries[0].path if len(entries) == 1 else ""

    def _select_path(self, path: str) -> None:
        row = self.controller.index_of(path)
        if row < 0:
            return
        index = self.proxy_model.mapFromSource(self.source_model.index(row, 1))
        if not index.isValid():
            # 被搜索挡住了 —— 新建/另存为的脚本要是不在筛选结果里就等于「消失」，
            # 这时清掉筛选比让用户自己找更合适。
            self.search_edit.clear()
            index = self.proxy_model.mapFromSource(self.source_model.index(row, 1))
        if index.isValid():
            self.table.selectRow(index.row())
            self.table.scrollTo(index)

    def _update_content_state(self):
        has_scripts = self.source_model.rowCount() > 0
        self.content_stack.setCurrentWidget(
            self.table if has_scripts else self.empty_page
        )

    def _update_drag_state(self):
        """筛选期间关掉拖拽：看得见的行只是一部分，落点算出来的位置会骗人。"""
        filtering = bool(self.search_edit.text().strip())
        self.table.setDragEnabled(not filtering)
        self.table.setDragDropMode(
            QAbstractItemView.DragDropMode.NoDragDrop
            if filtering
            else QAbstractItemView.DragDropMode.InternalMove
        )

    @Slot()
    def _update_action_state(self):
        rows = self._selected_rows()
        self.reload_btn.setEnabled(bool(rows))
        self.delete_btn.setEnabled(bool(rows))

    def _guard_dirty(self) -> None:
        """离开一条有未保存改动的脚本前问一句。两个按钮：存（并重载）或丢。"""
        entry = self.panel.entry
        if entry is None or not self.panel.dirty:
            return
        box = MessageBox(
            self.tr("有未保存的改动"),
            self.tr('"{}" 的改动还没保存，保存后会立即重载。').format(
                script_name(entry)
            ),
            self.window(),
        )
        box.yesButton.setText(self.tr("保存并重载"))
        box.cancelButton.setText(self.tr("放弃改动"))
        if box.exec():
            self.controller.save_script(entry.path, self.panel.editor.text())

    def _load_entry(self, entry: ScriptEntry) -> None:
        status = self.controller.status_of(entry.path)
        try:
            text = self.controller.read_script(entry.path)
        except OSError as exc:
            self.panel.show_unreadable(entry, status, str(exc))
            return
        self.panel.show_entry(entry, status, text)

    def _sync_detail(self) -> None:
        """把详情面板对到当前选中行上；同一条脚本不重读正文（会吃掉正在改的内容）。"""
        if self._restoring:
            return
        entries = self._selected_entries()
        target = entries[0] if len(entries) == 1 else None
        current = self.panel.entry
        if target is not None and current is not None and target.path == current.path:
            self.panel.update_entry(target, self.controller.status_of(target.path))
            return
        # 被移除的条目没什么可保存的，只有还在清单里的才值得问一句。
        if current is not None and self.controller.index_of(current.path) >= 0:
            self._guard_dirty()
        if target is None:
            self.panel.clear()
            return
        self._load_entry(target)

    # --- slots ---

    @Slot(list)
    def _on_scripts_changed(self, scripts: list):
        keep = self._focus_path()
        self._restoring = True
        try:
            self.source_model.set_scripts(scripts)
            self.source_model.set_statuses(self.controller.statuses)
            self._update_content_state()
            if keep:
                self._select_path(keep)
        finally:
            self._restoring = False
        self._update_drag_state()
        self._update_action_state()
        self._sync_detail()

    @Slot(dict)
    def _on_statuses_changed(self, statuses: dict):
        self.source_model.set_statuses(statuses)
        entry = self.panel.entry
        if entry is not None:
            self.panel.set_status(statuses.get(entry.path))

    @Slot()
    def _on_selection_changed(self):
        self._update_action_state()
        self._sync_detail()

    @Slot(QModelIndex)
    def _on_row_activated(self, index: QModelIndex):
        """双击直接把焦点送进正文（详情面板本来就跟着选中变）。"""
        if self.panel.entry is not None:
            self.panel.editor.code_widget.setFocus()

    @Slot(str, str)
    def _on_operation_failed(self, title: str, detail: str):
        show_error(title, detail, self.window())

    @Slot(str)
    def _on_operation_succeeded(self, message: str):
        show_success(self.tr("成功"), message, self.window())

    @Slot(str)
    def _on_search_changed(self, text: str):
        self.proxy_model.set_filter_text(text)
        self._update_drag_state()
        self._update_action_state()

    @Slot()
    def _on_import(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.tr("导入脚本"),
            "",
            self.tr("Python 脚本 (*.py)"),
        )
        if not paths:
            return
        if not self.controller.import_scripts(paths):
            show_warning(self.tr("提示"), self.tr("脚本已在列表中"), self.window())

    @Slot()
    def _on_new(self):
        dialog = NewScriptDialog(self.controller.scripts_dir, parent=self.window())
        if not dialog.exec():
            return
        path = self.controller.create_script(dialog.get_filename())
        if path:
            self._select_path(path)

    @Slot(str, str)
    def _on_save(self, path: str, text: str):
        if self.controller.save_script(path, text):
            self.panel.mark_saved(text)

    @Slot(str)
    def _on_save_as(self, text: str):
        """另存为＝以当前正文新建一条 new 条目（导入的脚本借此变成可编辑的副本）。"""
        dialog = NewScriptDialog(
            self.controller.scripts_dir, title=self.tr("另存为"), parent=self.window()
        )
        if not dialog.exec():
            return
        path = self.controller.create_script(dialog.get_filename(), text)
        if path:
            self._select_path(path)

    @Slot()
    def _on_reload_selected(self):
        for entry in self._selected_entries():
            self.controller.reload_script(entry.path)

    @Slot(str)
    def _on_open_external(self, path: str):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            show_warning(
                self.tr("打开失败"),
                self.tr("系统没有关联可打开 .py 文件的程序"),
                self.window(),
            )

    @Slot()
    def _on_remove(self):
        rows = self._selected_rows()
        entries = self._selected_entries()
        if not rows or not entries:
            return
        # 只有应用内新建的脚本才问「要不要连文件一起删」；导入条目的文件从不动，
        # 移除它就是把引用去掉，没什么要确认的（同重写页删规则）。
        if any(entry.origin == SCRIPT_ORIGIN_NEW for entry in entries):
            dialog = ScriptRemoveDialog(
                [script_name(entry) for entry in entries], parent=self.window()
            )
            if not dialog.exec():
                return
            self.controller.remove_scripts(rows, delete_files=dialog.delete_files())
            return
        self.controller.remove_scripts(rows)

    @Slot(QPoint)
    def _on_context_menu(self, pos: QPoint):
        rows = self._selected_rows()
        entries = self._selected_entries()
        if not rows or not entries:
            return
        menu = RoundMenu(parent=self.table)
        if len(rows) == 1:
            row, entry = rows[0], entries[0]
            target = not entry.enabled
            toggle_action = BaseAction(
                icon=FluentIcon.VIEW if target else FluentIcon.HIDE,
                text=self.tr("启用") if target else self.tr("停用"),
                parent=menu,
            )
            toggle_action.triggered.connect(
                lambda: self.controller.set_enabled(row, target)
            )
            menu.addAction(toggle_action)
            # 列表序＝执行序：多个脚本按这个序依次拿到同一条流量，所以上下移动
            # 是有语义的操作，不只是排版（与重写页同一条理由）。
            total = len(self.controller.scripts)
            up_action = BaseAction(
                icon=FluentIcon.UP, text=self.tr("上移"), parent=menu
            )
            up_action.setEnabled(row > 0)
            up_action.triggered.connect(lambda: self.controller.move_script(row, -1))
            menu.addAction(up_action)
            down_action = BaseAction(
                icon=FluentIcon.DOWN, text=self.tr("下移"), parent=menu
            )
            down_action.setEnabled(row < total - 1)
            down_action.triggered.connect(lambda: self.controller.move_script(row, 1))
            menu.addAction(down_action)
            open_action = BaseAction(
                icon=FluentIcon.DEVELOPER_TOOLS,
                text=self.tr("在编辑器中打开"),
                parent=menu,
            )
            open_action.triggered.connect(lambda: self._on_open_external(entry.path))
            menu.addAction(open_action)
        else:
            enable_action = BaseAction(
                icon=FluentIcon.VIEW, text=self.tr("启用"), parent=menu
            )
            enable_action.triggered.connect(
                lambda: self.controller.set_scripts_enabled(rows, True)
            )
            menu.addAction(enable_action)
            disable_action = BaseAction(
                icon=FluentIcon.HIDE, text=self.tr("停用"), parent=menu
            )
            disable_action.triggered.connect(
                lambda: self.controller.set_scripts_enabled(rows, False)
            )
            menu.addAction(disable_action)
        reload_action = BaseAction(
            icon=FluentIcon.SYNC, text=self.tr("重载"), parent=menu
        )
        reload_action.triggered.connect(self._on_reload_selected)
        menu.addAction(reload_action)
        remove_action = BaseAction(
            icon=FluentIcon.DELETE, text=self.tr("移除"), parent=menu
        )
        remove_action.triggered.connect(self._on_remove)
        menu.addAction(remove_action)
        menu.exec(self.table.viewport().mapToGlobal(pos))
