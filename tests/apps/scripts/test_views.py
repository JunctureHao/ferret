"""脚本页视图层的测试：清单与详情面板之间那几条容易互相踩的联动。

这一层真正难的不是控件摆放，而是三个「看起来无关、实际会互相吃掉状态」的点：

1. **整表 reset 会清掉选中并发 selectionChanged** —— 勾一下「启用」走的是控制器
   整批下发、回头重填本表那条路，重填期间必须哑掉详情联动，否则会被当成「切换到
   空选中」，正在编辑的正文先被问一遍存不存、再被清掉。
2. **同一条脚本不重读正文** —— 状态回报、启停、换序都会重发清单，每次都读盘就
   等于用户改一半的内容随时会被磁盘上的旧版本覆盖。
3. **筛选期间不许拖拽** —— 看得见的行只是一部分，落点算出来的位置会骗人。

弹窗一律换成假货：这里验的是「谁在什么时候被弹出来」，弹窗自身的校验在
`test_dialogs.py`。
"""

import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qfluentwidgets import qconfig

from ferret.apps.scripts import controllers, views
from ferret.apps.scripts.controllers import ScriptsController
from ferret.apps.scripts.views import ScriptsInterface
from ferret.core.mitm import MitmFacade, MitmRuntime, ScriptState, ScriptStatus
from ferret.core.settings import CONFIG

app = QApplication.instance() or QApplication([])


class FakeButton:
    """`MessageBox` 的按钮替身：调用方只会改文案。"""

    def setText(self, text: str) -> None:
        self.text = text


class FakeDialog:
    """替掉真弹窗：只记录被构造过几次，`exec` 返回预设结果。"""

    accepted = True
    calls: ClassVar[list[tuple]] = []

    def __init__(self, *args, **kwargs):
        type(self).calls.append((args, kwargs))
        self.yesButton = FakeButton()
        self.cancelButton = FakeButton()

    def exec(self) -> int:
        return 1 if type(self).accepted else 0


class FakeNewScriptDialog(FakeDialog):
    filename = "made.py"

    def get_filename(self) -> str:
        return type(self).filename


class FakeRemoveDialog(FakeDialog):
    delete = False

    def delete_files(self) -> bool:
        return type(self).delete


class ScriptsInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.managed = self.root / "managed"
        self.managed.mkdir()
        patcher = mock.patch.object(
            controllers, "get_scripts_dir", lambda: self.managed
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, CONFIG, "file", CONFIG.file)
        qconfig.load(str(self.root / "config.json"), CONFIG)

        self.runtime = MitmRuntime()
        self.controller = ScriptsController(mitm=MitmFacade(self.runtime))
        self.view = ScriptsInterface(controller=self.controller)
        self.view.resize(900, 700)
        # 真的显示出来：`isVisible()` 对隐藏顶层窗口的子控件恒为假，而只读/可写
        # 那一组按钮的显隐正是这里要验的东西（offscreen 平台下开销可忽略）。
        self.view.show()
        self.addCleanup(self.view.deleteLater)
        self.addCleanup(self.view.hide)

        for fake in (FakeNewScriptDialog, FakeRemoveDialog):
            fake.calls = []
            fake.accepted = True
        FakeRemoveDialog.delete = False
        FakeNewScriptDialog.filename = "made.py"

    def tearDown(self) -> None:
        CONFIG.set(CONFIG.scripts, [])

    # --- 夹具 ---

    def add_import(self, name: str, text: str = "x = 1\n") -> str:
        target = self.root / name
        target.write_text(text, encoding="utf-8")
        self.controller.import_scripts([str(target)])
        app.processEvents()
        return str(target)

    def add_new(self, name: str) -> str:
        path = self.controller.create_script(name)
        app.processEvents()
        return path

    def select_row(self, row: int) -> None:
        self.view.table.selectRow(row)
        app.processEvents()

    # --- 清单 ---

    def test_an_empty_list_shows_the_empty_page(self) -> None:
        self.assertIs(self.view.content_stack.currentWidget(), self.view.empty_page)
        self.assertFalse(self.view.reload_btn.isEnabled())
        self.assertFalse(self.view.delete_btn.isEnabled())

    def test_the_first_script_switches_to_the_table(self) -> None:
        self.add_import("a.py")
        self.assertIs(self.view.content_stack.currentWidget(), self.view.table)
        self.assertEqual(self.view.source_model.rowCount(), 1)

    def test_removing_the_last_script_returns_to_the_empty_page(self) -> None:
        self.add_import("a.py")
        self.controller.remove_scripts([0])
        app.processEvents()
        self.assertIs(self.view.content_stack.currentWidget(), self.view.empty_page)
        self.assertIsNone(self.view.panel.entry)

    def test_selecting_a_row_enables_the_toolbar(self) -> None:
        self.add_import("a.py")
        self.select_row(0)
        self.assertTrue(self.view.reload_btn.isEnabled())
        self.assertTrue(self.view.delete_btn.isEnabled())

    # --- 详情联动 ---

    def test_selecting_a_row_loads_its_source(self) -> None:
        path = self.add_import("a.py", "y = 2\n")
        self.select_row(0)
        entry = self.view.panel.entry
        assert entry is not None
        self.assertEqual(entry.path, path)
        self.assertEqual(self.view.panel.editor.text(), "y = 2\n")

    def test_an_import_entry_is_read_only(self) -> None:
        self.add_import("a.py")
        self.select_row(0)
        self.assertFalse(self.view.panel.save_btn.isVisible())
        self.assertTrue(self.view.panel.hint_label.text())

    def test_a_new_entry_is_editable(self) -> None:
        self.add_new("mine.py")
        self.select_row(0)
        self.assertTrue(self.view.panel.save_btn.isVisible())
        self.assertEqual(self.view.panel.hint_label.text(), "")

    def test_a_missing_file_shows_the_unreadable_branch(self) -> None:
        path = self.add_import("a.py")
        Path(path).unlink()
        self.select_row(0)
        self.assertEqual(self.view.panel.editor.text(), "")
        self.assertTrue(self.view.panel.hint_label.text())
        self.assertTrue(self.view.panel.error_view.isVisible())

    def test_clearing_the_selection_clears_the_panel(self) -> None:
        self.add_import("a.py")
        self.select_row(0)
        self.view.table.clearSelection()
        app.processEvents()
        self.assertIsNone(self.view.panel.entry)

    def test_a_multi_selection_shows_no_single_script(self) -> None:
        self.add_import("a.py")
        self.add_import("b.py")
        self.view.table.selectAll()
        app.processEvents()
        self.assertIsNone(self.view.panel.entry)

    def test_toggling_enabled_keeps_the_panel_and_unsaved_text(self) -> None:
        """整表 reset 不能吃掉正在编辑的内容 —— 勾一下「启用」丢一段代码。"""
        self.add_new("mine.py")
        self.select_row(0)
        self.view.panel.editor.code_widget.setPlainText("# work in progress\n")
        app.processEvents()
        self.assertTrue(self.view.panel.dirty)

        self.view.source_model.setData(
            self.view.source_model.index(0, 0),
            Qt.CheckState.Unchecked.value,
            Qt.ItemDataRole.CheckStateRole,
        )
        app.processEvents()

        self.assertFalse(self.controller.scripts[0].enabled)
        entry = self.view.panel.entry
        assert entry is not None
        self.assertFalse(entry.enabled)
        self.assertEqual(self.view.panel.editor.text(), "# work in progress\n")
        self.assertTrue(self.view.panel.dirty)

    def test_status_reports_reach_both_the_table_and_the_panel(self) -> None:
        path = self.add_import("a.py")
        self.select_row(0)
        self.runtime.script_status_changed.emit(
            path, ScriptStatus(ScriptState.ERROR, "SyntaxError: boom")
        )
        app.processEvents()
        self.assertIn(
            "SyntaxError: boom",
            self.view.source_model.data(
                self.view.source_model.index(0, 3), Qt.ItemDataRole.ToolTipRole
            ),
        )
        self.assertTrue(self.view.panel.error_view.isVisible())
        self.assertIn("SyntaxError: boom", self.view.panel.error_view.text())

    def test_leaving_a_dirty_script_offers_to_save(self) -> None:
        self.add_new("mine.py")
        self.add_import("other.py")
        self.select_row(0)
        self.view.panel.editor.code_widget.setPlainText("# keep me\n")
        app.processEvents()

        with mock.patch.object(views, "MessageBox", FakeDialog):
            FakeDialog.calls = []
            FakeDialog.accepted = True
            self.select_row(1)
        self.assertEqual(len(FakeDialog.calls), 1)
        saved = (self.managed / "mine.py").read_text(encoding="utf-8")
        self.assertEqual(saved, "# keep me\n")

    def test_discarding_leaves_the_file_alone(self) -> None:
        path = self.add_new("mine.py")
        self.add_import("other.py")
        self.select_row(0)
        before = Path(path).read_text(encoding="utf-8")
        self.view.panel.editor.code_widget.setPlainText("# throw away\n")
        app.processEvents()

        with mock.patch.object(views, "MessageBox", FakeDialog):
            FakeDialog.calls = []
            FakeDialog.accepted = False
            self.select_row(1)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), before)

    def test_a_removed_script_is_never_asked_about(self) -> None:
        """条目都没了，没什么可保存的。"""
        self.add_new("mine.py")
        self.select_row(0)
        self.view.panel.editor.code_widget.setPlainText("# gone\n")
        app.processEvents()
        with mock.patch.object(views, "MessageBox", FakeDialog):
            FakeDialog.calls = []
            self.controller.remove_scripts([0])
            app.processEvents()
        self.assertEqual(FakeDialog.calls, [])
        self.assertIsNone(self.view.panel.entry)

    # --- 保存 / 另存为 ---

    def test_saving_from_the_panel_writes_and_clears_dirty(self) -> None:
        path = self.add_new("mine.py")
        self.select_row(0)
        self.view.panel.editor.code_widget.setPlainText(
            "def request(flow):\n    pass\n"
        )
        app.processEvents()
        self.view.panel.save_btn.click()
        app.processEvents()
        self.assertFalse(self.view.panel.dirty)
        self.assertEqual(
            Path(path).read_text(encoding="utf-8"), "def request(flow):\n    pass\n"
        )

    def test_save_as_copies_the_text_into_a_new_entry(self) -> None:
        """导入的脚本借「另存为」变成可编辑的副本。"""
        self.add_import("a.py", "y = 2\n")
        self.select_row(0)
        FakeNewScriptDialog.filename = "copy.py"
        with mock.patch.object(views, "NewScriptDialog", FakeNewScriptDialog):
            self.view.panel.save_as_btn.click()
            app.processEvents()
        self.assertEqual(
            (self.managed / "copy.py").read_text(encoding="utf-8"), "y = 2\n"
        )
        entry = self.view.panel.entry
        assert entry is not None
        self.assertEqual(entry.path, str(self.managed / "copy.py"))

    def test_the_new_button_selects_what_it_created(self) -> None:
        FakeNewScriptDialog.filename = "fresh.py"
        with mock.patch.object(views, "NewScriptDialog", FakeNewScriptDialog):
            self.view.new_btn.click()
            app.processEvents()
        self.assertEqual(self.view.source_model.rowCount(), 1)
        entry = self.view.panel.entry
        assert entry is not None
        self.assertEqual(entry.path, str(self.managed / "fresh.py"))

    def test_cancelling_the_new_dialog_creates_nothing(self) -> None:
        FakeNewScriptDialog.accepted = False
        with mock.patch.object(views, "NewScriptDialog", FakeNewScriptDialog):
            self.view.new_btn.click()
            app.processEvents()
        self.assertEqual(self.controller.scripts, [])

    def test_the_import_button_feeds_the_file_dialog_result(self) -> None:
        target = self.root / "picked.py"
        target.write_text("x = 1\n", encoding="utf-8")
        with mock.patch.object(
            views.QFileDialog, "getOpenFileNames", return_value=([str(target)], "")
        ):
            self.view.import_btn.click()
            app.processEvents()
        self.assertEqual([e.path for e in self.controller.scripts], [str(target)])

    # --- 移除 ---

    def test_removing_an_import_entry_asks_nothing(self) -> None:
        """移除 import 条目就是去掉引用，文件从不动，没什么要确认的。"""
        path = self.add_import("a.py")
        self.select_row(0)
        with mock.patch.object(views, "ScriptRemoveDialog", FakeRemoveDialog):
            self.view.delete_btn.click()
            app.processEvents()
        self.assertEqual(FakeRemoveDialog.calls, [])
        self.assertEqual(self.controller.scripts, [])
        self.assertTrue(Path(path).exists())

    def test_removing_a_new_entry_confirms_first(self) -> None:
        path = self.add_new("mine.py")
        self.select_row(0)
        FakeRemoveDialog.delete = True
        with mock.patch.object(views, "ScriptRemoveDialog", FakeRemoveDialog):
            self.view.delete_btn.click()
            app.processEvents()
        self.assertEqual(len(FakeRemoveDialog.calls), 1)
        self.assertEqual(self.controller.scripts, [])
        self.assertFalse(Path(path).exists())

    def test_cancelling_the_remove_dialog_keeps_everything(self) -> None:
        self.add_new("mine.py")
        self.select_row(0)
        FakeRemoveDialog.accepted = False
        with mock.patch.object(views, "ScriptRemoveDialog", FakeRemoveDialog):
            self.view.delete_btn.click()
            app.processEvents()
        self.assertEqual(len(self.controller.scripts), 1)

    def test_remove_without_a_selection_is_a_noop(self) -> None:
        self.add_import("a.py")
        self.view.table.clearSelection()
        with mock.patch.object(views, "ScriptRemoveDialog", FakeRemoveDialog):
            self.view._on_remove()
            app.processEvents()
        self.assertEqual(len(self.controller.scripts), 1)

    # --- 搜索与换序 ---

    def test_filtering_hides_rows_and_disables_dragging(self) -> None:
        self.add_import("alpha.py")
        self.add_import("beta.py")
        self.view.search_edit.setText("alpha")
        app.processEvents()
        self.assertEqual(self.view.proxy_model.rowCount(), 1)
        self.assertFalse(self.view.table.dragEnabled())
        self.view.search_edit.clear()
        app.processEvents()
        self.assertEqual(self.view.proxy_model.rowCount(), 2)
        self.assertTrue(self.view.table.dragEnabled())

    def test_selecting_a_filtered_out_row_clears_the_filter(self) -> None:
        """新建的脚本要是落在筛选结果之外就等于「消失」了。"""
        self.add_import("alpha.py")
        self.view.search_edit.setText("alpha")
        app.processEvents()
        FakeNewScriptDialog.filename = "zeta.py"
        with mock.patch.object(views, "NewScriptDialog", FakeNewScriptDialog):
            self.view.new_btn.click()
            app.processEvents()
        self.assertEqual(self.view.search_edit.text(), "")
        entry = self.view.panel.entry
        assert entry is not None
        self.assertEqual(entry.path, str(self.managed / "zeta.py"))

    def test_a_drop_reaches_the_controller_and_reorders(self) -> None:
        for name in ("a.py", "b.py"):
            self.add_import(name)
        self.view.source_model.rows_moved.emit(0, 1)
        app.processEvents()
        self.assertEqual(
            [Path(e.path).name for e in self.controller.scripts], ["b.py", "a.py"]
        )

    def test_the_selected_script_follows_a_reorder(self) -> None:
        for name in ("a.py", "b.py"):
            self.add_import(name)
        self.select_row(0)
        self.controller.move_script_to(0, 1)
        app.processEvents()
        entry = self.view.panel.entry
        assert entry is not None
        self.assertEqual(Path(entry.path).name, "a.py")
        self.assertEqual(self.view._selected_rows(), [1])


if __name__ == "__main__":
    unittest.main()
