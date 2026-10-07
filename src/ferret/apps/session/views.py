from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
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
    PushButton,
    RoundMenu,
    TableView,
    TransparentToolButton,
)

from ferret.apps.common.flow.protocols import READONLY_CAPABILITIES
from ferret.apps.common.flow.views import FlowViewerPane
from ferret.apps.common.icon import BaseAction
from ferret.apps.common.info_bar import show_error, show_success
from ferret.apps.session.controllers import SessionController, SessionViewController
from ferret.apps.session.dialogs import SessionDeleteDialog, SessionNameDialog
from ferret.apps.session.models import (
    SessionFilterProxyModel,
    SessionMeta,
    SessionTableModel,
)
from ferret.core.mitm import FlowRow, flow_row


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

    # --- titlebar 搜索协议转发（路由检查的是加入 stackedWidget 的本类，§4.2）---

    def search_placeholder(self) -> str:
        return self.list_page.search_placeholder()

    def apply_search(self, text: str) -> None:
        self.list_page.apply_search(text)

    def current_search_text(self) -> str:
        return self.list_page.current_search_text()

    def search_focus_target(self) -> QWidget:
        return self.list_page.search_focus_target()


class SessionListPage(QWidget):
    """会话列表页：工具栏 + 表格/空状态"""

    def __init__(self, controller: SessionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._search_text = ""  # titlebar 框回填源（协议 current_search_text，§4.2）
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
        # 右键先选中再弹菜单（流量表同款）：菜单里的动作全部作用于选区，
        # 不开这个，右键一个未选中的行会出现「导出是这行、删除是旧选区」的分裂。
        self.table.setSelectRightClickedRow(True)
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
        layout.addStretch(1)
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

    # --- titlebar 搜索协议（规格 §4.2）---

    def search_placeholder(self) -> str:
        return self.tr("搜索会话")

    def apply_search(self, text: str) -> None:
        self._search_text = text
        self.proxy_model.set_filter_text(text)

    def current_search_text(self) -> str:
        return self._search_text

    def search_focus_target(self) -> QWidget:
        return self.table

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
        # 重命名是单条语义：多选时按钮已置灰，F2 同一口径不动作，
        # 不然静默只改第一行。
        if len(rows) != 1:
            return
        row = self.proxy_model.mapToSource(rows[0]).row()
        meta = self.source_model.session_at(row)
        if not meta:
            return
        old_id = meta.session_id
        dlg = SessionNameDialog(
            self.tr("重命名会话"),
            default_name=meta.name,
            flow_count=meta.flow_count,
            parent=self.window(),
        )
        try:
            if dlg.exec():
                new_name = dlg.get_name()
                if new_name != meta.name:
                    self.controller.rename_session(old_id, dlg.get_name())
        finally:
            dlg.deleteLater()

    @Slot()
    def _on_delete(self):
        metas = self._selection_metas()
        if not metas:
            return

        if len(metas) == 1:
            names = metas[0].name
        else:
            names = self.tr("选中 {} 个会话").format(len(metas))

        dlg = SessionDeleteDialog(names, self.window())
        try:
            if dlg.exec():
                self.controller.delete_sessions([m.session_id for m in metas])
        finally:
            dlg.deleteLater()

    @Slot(QPoint)
    def _on_context_menu(self, pos: QPoint):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        source_index = self.proxy_model.mapToSource(index)
        meta = self.source_model.session_at(source_index.row())
        if not meta:
            return

        selected = self._selection_metas()

        menu = RoundMenu(parent=self)
        menu.closedSignal.connect(menu.deleteLater)
        menu.addAction(self._make_action(menu, self.tr("打开"), self._open_selected))
        # 重命名是单条语义：多选时菜单项置灰（工具栏按钮同一口径），
        # 点了没反应的死菜单项比没有更糟。
        rename_action = self._make_action(menu, self.tr("重命名"), self._on_rename)
        rename_action.setEnabled(len(selected) == 1)
        menu.addAction(rename_action)
        # 导出按选区分支：单条走存文件对话框，多条选一个目录按名字各落一个
        # .flow（删除本就支持批量）。
        if len(selected) > 1:
            menu.addAction(
                self._make_action(
                    menu,
                    self.tr("导出 {} 个会话").format(len(selected)),
                    lambda: self._export_selected(selected),
                )
            )
        else:
            menu.addAction(
                self._make_action(
                    menu, self.tr("导出 Flow"), lambda: self._export_session(meta)
                )
            )
        menu.addAction(
            self._make_action(
                menu,
                self.tr("在文件管理器中显示"),
                lambda: self._show_in_explorer(meta),
            )
        )
        menu.addSeparator()
        menu.addAction(
            self._make_action(menu, self.tr("删除"), self._on_delete, FluentIcon.DELETE)
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _make_action(self, menu, text, callback, icon=None):
        action = BaseAction(icon=icon, text=text, parent=menu)
        action.triggered.connect(callback)
        return action

    def _selection_metas(self) -> list[SessionMeta]:
        """当前选区对应的会话元数据（经代理模型映射回源行）。"""
        metas: list[SessionMeta] = []
        for r in self.table.selectionModel().selectedRows():
            src_row = self.proxy_model.mapToSource(r).row()
            m = self.source_model.session_at(src_row)
            if m:
                metas.append(m)
        return metas

    def _export_session(self, meta: SessionMeta):
        path, _ = QFileDialog.getSaveFileName(
            self.window(),
            self.tr("导出会话"),
            f"{meta.name}.flow",
            self.tr("Flow 文件 (*.flow)"),
        )
        if path:
            self.controller.export_session(meta.session_id, Path(path))

    def _export_selected(self, metas: list[SessionMeta]):
        directory = QFileDialog.getExistingDirectory(
            self.window(), self.tr("选择导出目录")
        )
        if directory:
            self.controller.export_sessions(metas, Path(directory))

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
        rows = self.table.selectionModel().selectedRows()
        # 重命名是单条语义，多选置灰（_on_rename 同一口径）；删除支持批量。
        self.rename_btn.setEnabled(len(rows) == 1)
        self.delete_btn.setEnabled(bool(rows))

    def eventFilter(self, obj, event):
        return super().eventFilter(obj, event)


class _SessionRowSource:
    """会话页的 FlowSource：把文件读回的死 flow 就地折成行快照（#90）。

    没有内核线程 competing，折叠在 GUI 线程直接做；remove 按 id 反查回死
    flow 再交给 View（mitmproxy View.remove 只认 flow 对象）。
    """

    def __init__(self, view) -> None:
        self._view = view

    def __iter__(self) -> Iterator[FlowRow]:
        return iter(flow_row(flow) for flow in self._view)

    def clear(self) -> None:
        self._view.clear()

    def remove(self, flow_ids) -> None:
        flows = [
            flow for fid in flow_ids if (flow := self._view.get_by_id(fid)) is not None
        ]
        if flows:
            self._view.remove(flows)


class SessionViewerPage(QWidget):
    """只读会话查看器。

    工具栏骨架常驻；`FlowViewerPane`（详情面板那一大家子 qfw 控件，几十 MB 量级）
    推迟到首次 `_ensure_viewer` 才构造——打开会话前这个查看器永远不可见，启动没
    必要为它常驻第二套 pane（捕获页已有第一套）。
    """

    def __init__(self, controller: SessionController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.vc: SessionViewController | None = None
        self.splitter: FlowViewerPane | None = None
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

        self.back_btn.clicked.connect(self._go_back)
        self.export_btn.clicked.connect(self._on_export)

    def __init_layout(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QWidget(self)
        toolbar.setFixedHeight(44)
        self._toolbar_layout = QHBoxLayout(toolbar)
        self._toolbar_layout.setContentsMargins(12, 6, 12, 6)
        self._toolbar_layout.setSpacing(6)
        self._toolbar_layout.addWidget(self.back_btn)
        self._toolbar_layout.addWidget(self.name_label, 1)
        self._toolbar_layout.addWidget(self.readonly_badge)
        self._toolbar_layout.addSpacing(8)
        self._toolbar_layout.addWidget(self.export_btn)

        layout.addWidget(toolbar)
        self._body_layout = layout

    def _ensure_viewer(self) -> FlowViewerPane:
        """首次使用才构造查看器本体；可无数据独立调用（测试与 load 共用入口）。

        模式切换按钮此刻才归位工具栏：实体归 splitter/pane 所有（翻转 / 图标文案 /
        显隐都在那边），插在导出按钮之前，addWidget 会顺带把它 reparent 过来显示。
        """
        if self.splitter is None:
            splitter = FlowViewerPane(
                parent=self,
                controller=None,
                capabilities=READONLY_CAPABILITIES,
            )
            self.table = splitter.table
            self._toolbar_layout.insertWidget(
                self._toolbar_layout.count() - 1, splitter.mode_button
            )
            self._body_layout.addWidget(splitter, 1)
            self.splitter = splitter
        return self.splitter

    def load(self, meta: SessionMeta, vc: SessionViewController):
        self.vc = vc
        self._meta = meta
        # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
        flows = self.tr("{} 条").format(meta.flow_count)
        self.name_label.setText(f"{meta.name}  ·  {flows}  ·  ")

        splitter = self._ensure_viewer()
        splitter.set_controller(vc)
        # 会话 flow 从文件读回、没有 mitm 线程：包一层就地折叠的数据源（快照在
        # GUI 线程生成，死对象直读安全，#90）。经 pane fan-out 同时喂平铺与连接
        # 树两个模型（树模式在会话页同样可用）。
        splitter.set_source(_SessionRowSource(vc.view))

    def _go_back(self):
        iface = self.parent()
        while iface is not None and not isinstance(iface, SessionsInterface):
            iface = iface.parent()
        if isinstance(iface, SessionsInterface):
            iface.show_list()

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
