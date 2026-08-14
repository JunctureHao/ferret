import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPoint,
    QSize,
    Qt,
    Slot,
)
from PySide6.QtGui import QKeySequence, QShortcut
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
    FluentIcon,
    IndeterminateProgressBar,
    LineEdit,
    PushButton,
    TableView,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.menu import RoundMenu

from ferret.apps.capture.views import CapturesDataPanel, CapturesDataTable
from ferret.apps.common.flow.protocols import READONLY_CAPABILITIES
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.common.splitter import OrientationSplitter
from ferret.apps.session.controllers import SessionController, SessionViewController
from ferret.apps.session.dialogs import SessionDeleteDialog, SessionNameDialog
from ferret.apps.session.models import (
    SessionFilterProxyModel,
    SessionMeta,
    SessionTableModel,
)


class SessionsInterface(QWidget):
    """会话一级页面：列表页 + 查看器页切换"""

    def __init__(self, controller: SessionController, parent=None):
        super().__init__(parent)
        self.setObjectName("SessionsInterface")
        self.controller = controller

        self.__init_widget()
        self.__connect_signal_to_slot()

    def __init_widget(self):
        self.stack = QStackedWidget(self)
        self.list_page = SessionListPage(self.controller, self)
        self.viewer_page = SessionViewerPage(self.controller, self)
        self.stack.addWidget(self.list_page)
        self.stack.addWidget(self.viewer_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

    def __connect_signal_to_slot(self):
        self.controller.session_opened.connect(self.__on_session_opened)

    @Slot(object, object)
    def __on_session_opened(self, meta: SessionMeta, vc: SessionViewController):
        self.viewer_page.load(meta, vc)
        self.stack.setCurrentWidget(self.viewer_page)

    def show_list(self):
        self.stack.setCurrentWidget(self.list_page)

    def refresh(self):
        self.list_page.refresh()


class SessionListPage(QWidget):
    """会话列表页：工具栏 + 表格/空状态"""

    def __init__(self, controller: SessionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.__init_widget()
        self.__init_layout()
        self.__connect_signal_to_slot()
        self._update_content_state()
        self.refresh()

    def __init_widget(self):
        self.toolbar = self.__build_toolbar()
        self.loading_bar = IndeterminateProgressBar(self)
        self.loading_bar.setVisible(False)
        self.loading_bar.setFixedHeight(3)

        self.source_model = SessionTableModel(self)
        self.proxy_model = SessionFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.source_model)

        self.table = TableView(self)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().hide()
        self.table.setModel(self.proxy_model)
        self.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        widths = [320, 170, 90, 100, 90]
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for i, w in enumerate(widths):
            self.table.setColumnWidth(i, w)
        header.setSectionResizeMode(len(widths) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.empty_page = self.__build_empty_page()
        self.content_stack = QStackedWidget(self)
        self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.empty_page)

    def __build_toolbar(self) -> QWidget:
        bar = QWidget(self)
        bar.setFixedHeight(46)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(6)

        self.import_btn = PushButton(FluentIcon.DOWNLOAD, self.tr("导入"), bar)

        self.refresh_btn = TransparentToolButton(FluentIcon.SYNC, bar)
        self.refresh_btn.setFixedSize(32, 32)
        self.refresh_btn.setIconSize(QSize(18, 18))
        self.refresh_btn.setToolTip(self.tr("刷新"))

        self.search_edit = LineEdit(bar)
        self.search_edit.setPlaceholderText(self.tr("搜索会话"))
        self.search_edit.setFixedHeight(32)
        self.search_edit.setClearButtonEnabled(True)

        self.rename_btn = TransparentToolButton(FluentIcon.EDIT, bar)
        self.rename_btn.setFixedSize(32, 32)
        self.rename_btn.setIconSize(QSize(18, 18))
        self.rename_btn.setToolTip(self.tr("重命名") + " (F2)")
        self.rename_btn.setEnabled(False)

        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, bar)
        self.delete_btn.setFixedSize(32, 32)
        self.delete_btn.setIconSize(QSize(18, 18))
        self.delete_btn.setToolTip(self.tr("删除"))
        self.delete_btn.setEnabled(False)

        layout.addWidget(self.import_btn)
        layout.addWidget(self.refresh_btn)
        layout.addWidget(self.search_edit, 1)
        layout.addWidget(self.rename_btn)
        layout.addWidget(self.delete_btn)
        return bar

    def __build_empty_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = BodyLabel(self.tr("暂无保存的会话"), page)
        import_btn = PushButton(FluentIcon.DOWNLOAD, self.tr("导入 Flow"), page)
        layout.addStretch(1)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(import_btn, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        import_btn.clicked.connect(self._on_import)
        return page

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.loading_bar)
        layout.addWidget(self.content_stack, 1)

    def __connect_signal_to_slot(self):
        self.import_btn.clicked.connect(self._on_import)
        self.refresh_btn.clicked.connect(self.refresh)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.rename_btn.clicked.connect(self._on_rename)
        self.delete_btn.clicked.connect(self._on_delete)
        self.table.doubleClicked.connect(self._on_row_activated)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        self.controller.sessions_loaded.connect(self._on_sessions_loaded)
        self.controller.session_created.connect(self._on_session_created)
        self.controller.session_updated.connect(self.source_model.update_session)
        self.controller.session_deleted.connect(self.source_model.remove_session)
        self.controller.busy_changed.connect(self._on_busy)
        self.controller.operation_failed.connect(self._on_operation_failed)
        self.controller.operation_succeeded.connect(self._on_operation_succeeded)

        self.table.selectionModel().selectionChanged.connect(self._update_action_state)

        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: self.search_edit.setFocus()
        )
        QShortcut(QKeySequence(Qt.Key.Key_Return), self.table).activated.connect(
            self._open_selected
        )
        QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table).activated.connect(
            self._on_delete
        )
        QShortcut(QKeySequence(Qt.Key.Key_F2), self.table).activated.connect(
            self._on_rename
        )

    # --- slots ---

    @Slot(list)
    def _on_sessions_loaded(self, sessions: list):
        self.source_model.set_sessions(sessions)
        self._update_content_state()
        self._update_action_state()

    @Slot(object)
    def _on_session_created(self, meta: SessionMeta):
        self.source_model.add_session(meta)
        self._update_content_state()

    @Slot(bool)
    def _on_busy(self, busy: bool):
        self.loading_bar.setVisible(busy)

    @Slot(str, str)
    def _on_operation_failed(self, title, detail):
        show_error(title, detail, self)

    @Slot(str)
    def _on_operation_succeeded(self, message: str):
        show_success(self.tr("成功"), message, self)

    @Slot(str)
    def _on_search_changed(self, text: str):
        self.proxy_model.set_filter_text(text)

    @Slot()
    def refresh(self):
        self.controller.refresh()

    @Slot()
    def _on_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, self.tr("导入 Flow 文件"), "", self.tr("Flow 文件 (*.flow)")
        )
        if path:
            self.controller.import_session(Path(path))

    @Slot(QModelIndex)
    def _on_row_activated(self, index: QModelIndex):
        self._open_selected()

    def _open_selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = self.proxy_model.mapToSource(rows[0]).row()
        meta = self.source_model.session_at(row)
        if meta:
            self.controller.open_session(meta.session_id)

    @Slot()
    def _on_rename(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = self.proxy_model.mapToSource(rows[0]).row()
        meta = self.source_model.session_at(row)
        if not meta:
            return
        dlg = SessionNameDialog(
            self.tr("重命名会话"),
            default_name=meta.name,
            parent=self.window(),
        )
        if dlg.exec():
            self.controller.rename_session(meta.session_id, dlg.get_name())

    @Slot()
    def _on_delete(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        row = self.proxy_model.mapToSource(rows[0]).row()
        meta = self.source_model.session_at(row)
        if not meta:
            return
        dlg = SessionDeleteDialog(meta.name, self.window())
        if dlg.exec():
            self.controller.delete_session(meta.session_id)

    @Slot(QPoint)
    def _on_context_menu(self, pos: QPoint):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        source_index = self.proxy_model.mapToSource(index)
        meta = self.source_model.session_at(source_index.row())
        if not meta:
            return

        menu = RoundMenu(parent=self)
        menu.addAction(self._make_action("打开", self._open_selected))
        menu.addAction(self._make_action("重命名", self._on_rename))
        menu.addAction(
            self._make_action("导出 Flow", lambda: self._export_session(meta))
        )
        menu.addAction(
            self._make_action(
                self.tr("在文件管理器中显示"),
                lambda: self._show_in_explorer(meta),
            )
        )
        menu.addSeparator()
        menu.addAction(self._make_action("删除", self._on_delete, FluentIcon.DELETE))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _make_action(self, text, callback, icon=None):
        action = BaseAction(icon=icon, text=text, parent=self)
        action.triggered.connect(callback)
        return action

    def _export_session(self, meta: SessionMeta):
        path, _ = QFileDialog.getSaveFileName(
            self.window(),
            self.tr("导出会话"),
            f"{meta.name}.flow",
            self.tr("Flow 文件 (*.flow)"),
        )
        if path:
            self.controller.export_session(meta.session_id, Path(path))

    def _show_in_explorer(self, meta: SessionMeta):
        flow_path = str(meta.path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", flow_path])
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(QUrl.fromLocalFile(str(meta.path.parent)))

    def _update_content_state(self):
        if self.source_model.rowCount() == 0:
            self.content_stack.setCurrentWidget(self.empty_page)
        else:
            self.content_stack.setCurrentWidget(self.table)

    def _update_action_state(self):
        has_selection = bool(self.table.selectionModel().selectedRows())
        self.rename_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)

    def eventFilter(self, obj, event):
        return super().eventFilter(obj, event)


class SessionViewerPage(QWidget):
    """只读会话查看器"""

    def __init__(self, controller: SessionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.vc: SessionViewController | None = None
        self.__init_widget()
        self.__init_layout()

    def __init_widget(self):
        self.back_btn = TransparentToolButton(FluentIcon.RETURN, self)
        self.back_btn.setFixedSize(32, 32)
        self.back_btn.setIconSize(QSize(18, 18))
        self.back_btn.setToolTip(self.tr("返回列表"))

        self.name_label = BodyLabel(self)
        self.readonly_badge = BodyLabel(self.tr("只读"), self)

        self.export_btn = TransparentToolButton(FluentIcon.SAVE, self)
        self.export_btn.setFixedSize(32, 32)
        self.export_btn.setIconSize(QSize(18, 18))
        self.export_btn.setToolTip(self.tr("导出会话"))

        self.splitter = OrientationSplitter(parent=self)
        self.table = CapturesDataTable(self, None, READONLY_CAPABILITIES)
        self.panel = CapturesDataPanel(self, None)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.panel)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([1, 0])

        self.table.row_selected.connect(self._on_row_selected)
        self.table.row_double_clicked.connect(self._on_row_double_clicked)
        self.back_btn.clicked.connect(self._go_back)
        self.export_btn.clicked.connect(self._on_export)

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget(self)
        toolbar.setFixedHeight(44)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(12, 6, 12, 6)
        tb_layout.setSpacing(6)
        tb_layout.addWidget(self.back_btn)
        tb_layout.addWidget(self.name_label, 1)
        tb_layout.addWidget(self.readonly_badge)
        tb_layout.addSpacing(8)
        tb_layout.addWidget(self.export_btn)

        layout.addWidget(toolbar)
        layout.addWidget(self.splitter, 1)

    def load(self, meta: SessionMeta, vc: SessionViewController):
        self.vc = vc
        self._meta = meta
        self.name_label.setText(f"{meta.name}  ·  {meta.flow_count} 条  ·  ")

        self.table.set_controller(vc)
        self.panel.set_controller(vc)
        self.table.set_view(vc.view)

    def _go_back(self):
        iface = self.parent()
        while iface is not None and not isinstance(iface, SessionsInterface):
            iface = iface.parent()
        if isinstance(iface, SessionsInterface):
            iface.show_list()

    @Slot(dict)
    def _on_row_selected(self, data: dict):
        self.panel.set_data(data)
        if self.splitter.sizes()[1] == 0:
            self.splitter.setSizes([1, 1])

    @Slot(dict)
    def _on_row_double_clicked(self, data: dict):
        self.panel.set_data(data)
        w = self.splitter.width()
        if w > 0:
            self.splitter.setSizes([w // 2, w // 2])

    @Slot()
    def _on_export(self):
        if not self.vc:
            return
        path, _ = QFileDialog.getSaveFileName(
            self.window(),
            self.tr("导出会话"),
            f"{self._meta.name}.flow",
            self.tr("Flow 文件 (*.flow)"),
        )
        if path:
            self.controller.export_session(self._meta.session_id, Path(path))
