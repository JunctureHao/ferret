"""Request library browsing and operations preserve request identity and grouping."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication, QDialog
from qfluentwidgets import MessageBox, RoundMenu

from ferret.apps.compose.library import ComposeLibraryPanel, _NameDialog
from ferret.apps.compose.models import SavedBody, SavedRequest
from ferret.apps.compose.store import ComposeRequestStore

app = QApplication.instance() or QApplication([])


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = ComposeRequestStore(
            path=Path(self.directory.name) / "requests.json"
        )
        self.panel = ComposeLibraryPanel(self.store)

    def tearDown(self) -> None:
        self.store.flush()
        self.panel.deleteLater()
        self.store.deleteLater()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        self.directory.cleanup()

    def _add(
        self, name="请求", *, folder="分组", method="POST", url="https://example.test/"
    ):
        request = SavedRequest.create(
            name=name,
            method=method,
            url=url,
            folder=folder,
            headers=(("X-Repeat", "a"), ("X-Repeat", "b")),
            body=SavedBody(kind="json", text='{"a":1}'),
        )
        self.store.upsert(request)
        return request

    @staticmethod
    def _finish_dialog(dialog: QDialog, *, accepted: bool = True) -> None:
        # 数据操作测试直接完成对话框；淡入/淡出和鼠标事件由生命周期回归覆盖。
        QDialog.done(
            dialog,
            QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected,
        )
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_empty_library_can_create_a_draft_or_folder(self):
        self.assertIs(self.panel.stack.currentWidget(), self.panel.empty_hint)
        created = []
        self.panel.create_requested.connect(lambda: created.append(True))
        self.panel.new_request_btn.click()
        self.assertEqual(created, [True])
        self.panel._prompt_new_folder()
        dialog = self.panel.findChild(_NameDialog)
        assert dialog is not None
        dialog.name_edit.setText("文件夹")
        self.assertEqual(self.store.folders(), [])
        self._finish_dialog(dialog)
        self.assertEqual(self.store.folders(), ["文件夹"])
        self.assertIs(self.panel.stack.currentWidget(), self.panel.tree)

    def test_search_matches_method_name_and_url_and_restores_collapsed_folder(self):
        request = self._add(name="Orders", url="https://example.test/v1/orders")
        folder = self.panel.tree.topLevelItem(0)
        assert folder is not None
        folder.setExpanded(False)
        for query in ("  Orders  ", "POST", "example.test"):
            self.panel.apply_search(query)
            self.assertEqual(self.panel.current_search_text(), query)
            self.assertIn(request.id, self.panel._request_items)
            folder = self.panel.tree.topLevelItem(0)
            assert folder is not None
            self.assertTrue(folder.isExpanded())
        self.panel.apply_search("missing")
        self.assertIs(self.panel.stack.currentWidget(), self.panel.empty_hint)
        self.assertEqual(self.panel.empty_title.text(), "没有匹配的请求")
        self.panel.apply_search("")
        folder = self.panel.tree.topLevelItem(0)
        assert folder is not None
        self.assertFalse(folder.isExpanded())

    def test_selection_and_dirty_marker_survive_refresh_and_open_emits_id(self):
        request = self._add()
        self.panel.set_current_request(request.id, dirty=True)
        self.store.add_folder("其他")
        selected = self.panel.tree.currentItem()
        self.assertEqual(
            selected.data(0, Qt.ItemDataRole.UserRole), ("request", request.id)
        )
        self.assertTrue(selected.text(0).endswith(" *"))
        opened = []
        self.panel.open_requested.connect(opened.append)
        self.panel._on_item_clicked(selected, 0)
        self.assertEqual(opened, [request.id])

    def test_rename_move_duplicate_and_folder_deletion_preserve_payload(self):
        request = self._add()
        self.panel._start_rename(self.panel._request_items[request.id])
        dialog = self.panel.findChild(_NameDialog)
        assert dialog is not None
        dialog.name_edit.setText("新名称")
        self._finish_dialog(dialog)
        renamed = self.store.get(request.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "新名称")
        self.panel._move_request(request.id, "其他")
        moved = self.store.get(request.id)
        assert moved is not None
        self.assertEqual(moved.folder, "其他")
        self.panel._duplicate_request(request.id)
        requests = self.store.requests()
        self.assertEqual(len(requests), 2)
        copy = next(item for item in requests if item.id != request.id)
        self.assertEqual(copy.name, "新名称（副本）")
        self.assertEqual(copy.body, request.body)
        self.assertEqual(copy.headers, request.headers)
        self.panel._delete_folder("其他")
        box = self.panel.findChild(MessageBox)
        assert box is not None
        self._finish_dialog(box)
        self.assertEqual([item.folder for item in self.store.requests()], ["", ""])

    def test_cancelled_delete_keeps_request_and_confirmed_delete_removes_only_target(
        self,
    ):
        request = self._add()
        other = self._add(name="其他")
        self.panel._delete_request(request.id)
        box = self.panel.findChild(MessageBox)
        assert box is not None
        self._finish_dialog(box, accepted=False)
        self.assertIsNotNone(self.store.get(request.id))
        self.panel._delete_request(request.id)
        box = self.panel.findChild(MessageBox)
        assert box is not None
        self.assertIsNotNone(self.store.get(request.id))
        self._finish_dialog(box)
        self.assertIsNone(self.store.get(request.id))
        self.assertIsNotNone(self.store.get(other.id))

    def test_context_menu_keeps_only_latest_target_after_reopening(self):
        first = self._add(name="第一个", folder="")
        second = self._add(name="第二个", folder="")
        self.panel.resize(300, 600)
        self.panel.show()
        app.processEvents()
        opened = []
        self.panel.open_requested.connect(opened.append)
        for request in (first, second, first):
            item = self.panel._request_items[request.id]
            self.panel._on_context_menu(self.panel.tree.visualItemRect(item).center())
            menu = self.panel._context_menu
            assert menu is not None
            menu.close()
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            # 复用菜单窗口，上次的动作已释放。
            roots = [
                child
                for child in self.panel.findChildren(RoundMenu)
                if child.parentWidget() is self.panel
            ]
            self.assertEqual(roots, [menu])
            menu.menuActions()[0].trigger()
            app.processEvents()
            self.assertEqual(opened[-1], request.id)
        self.assertEqual(opened, [first.id, second.id, first.id])
