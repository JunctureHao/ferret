"""脚本页两个对话框的测试。

`NewScriptDialog` 的活儿只有一件但坑在细节：文件名要落到应用托管目录里，所以
路径分隔符、Windows 保留设备名、同名文件都得挡在「创建」之前 —— 放过去就是一个
OSError 弹窗，或者更糟：`../` 把文件写到托管目录外面。

`ScriptRemoveDialog` 要钉的是默认值：「移除条目」与「删文件」是两件事，后者不可
撤销，所以勾选框默认不勾（plans/scripts.md §3.4）。

`MessageBoxBase` 会读 `parent.width()` 铺遮罩层，所以每个用例都得有宿主窗口。
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.scripts.dialogs import NewScriptDialog, ScriptRemoveDialog

app = QApplication.instance() or QApplication([])


class DialogHost(unittest.TestCase):
    """所有子类共用一个宿主窗口：`MaskDialogBase` 拿它的尺寸铺遮罩层。"""

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(900, 700)
        self.addCleanup(self.host.deleteLater)


class NewScriptDialogTests(DialogHost):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.dialog = NewScriptDialog(self.directory, parent=self.host)
        self.addCleanup(self.dialog.deleteLater)

    def type_name(self, name: str) -> None:
        self.dialog.name_edit.setText(name)

    def test_prefills_a_usable_name(self) -> None:
        self.assertEqual(self.dialog.get_filename(), "script.py")
        self.assertTrue(self.dialog.yesButton.isEnabled())
        self.assertEqual(self.dialog.error_label.text(), "")

    def test_only_the_stem_is_preselected(self) -> None:
        """接着打字换掉名字、`.py` 留着 —— 后缀是必填校验项。"""
        self.assertEqual(self.dialog.name_edit.selectedText(), "script")

    def test_the_target_directory_is_shown(self) -> None:
        self.assertIn(str(self.directory), self.dialog.dir_label.text())

    def test_the_trust_warning_is_shown(self) -> None:
        self.assertTrue(self.dialog.warning_label.text())

    def test_a_custom_title_is_used(self) -> None:
        dialog = NewScriptDialog(self.directory, title="另存为", parent=self.host)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.title_label.text(), "另存为")

    def test_an_empty_name_is_refused(self) -> None:
        self.type_name("   ")
        self.assertFalse(self.dialog.yesButton.isEnabled())
        self.assertTrue(self.dialog.error_label.text())

    def test_a_missing_suffix_is_refused(self) -> None:
        self.type_name("script")
        self.assertFalse(self.dialog.yesButton.isEnabled())

    def test_the_suffix_check_is_case_insensitive(self) -> None:
        self.type_name("Script.PY")
        self.assertTrue(self.dialog.yesButton.isEnabled())

    def test_a_bare_suffix_is_refused(self) -> None:
        self.type_name(".py")
        self.assertFalse(self.dialog.yesButton.isEnabled())

    def test_path_separators_are_refused(self) -> None:
        """挡住「往别处写」：文件名里不该出现路径。"""
        for name in ("sub/script.py", "sub\\script.py", "../script.py"):
            with self.subTest(name=name):
                self.type_name(name)
                self.assertFalse(self.dialog.yesButton.isEnabled())

    def test_other_illegal_characters_are_refused(self) -> None:
        for name in ('a"b.py', "a*b.py", "a?b.py", "a|b.py", "a<b.py", "C:x.py"):
            with self.subTest(name=name):
                self.type_name(name)
                self.assertFalse(self.dialog.yesButton.isEnabled())

    def test_reserved_device_names_are_refused(self) -> None:
        """`con.py` 这种文件在 Windows 上建不出来，提前拦比 OSError 好看。"""
        for name in ("con.py", "COM1.py", "lpt9.py"):
            with self.subTest(name=name):
                self.type_name(name)
                self.assertFalse(self.dialog.yesButton.isEnabled())

    def test_an_existing_file_is_refused(self) -> None:
        (self.directory / "taken.py").write_text("x = 1\n", encoding="utf-8")
        self.type_name("taken.py")
        self.assertFalse(self.dialog.yesButton.isEnabled())
        self.assertTrue(self.dialog.error_label.text())

    def test_fixing_the_name_re_enables_the_button(self) -> None:
        self.type_name("bad/name.py")
        self.assertFalse(self.dialog.yesButton.isEnabled())
        self.type_name("good_name.py")
        self.assertTrue(self.dialog.yesButton.isEnabled())
        self.assertEqual(self.dialog.error_label.text(), "")

    def test_surrounding_whitespace_is_dropped(self) -> None:
        self.type_name("  spaced.py  ")
        self.assertEqual(self.dialog.get_filename(), "spaced.py")
        self.assertTrue(self.dialog.yesButton.isEnabled())


class ScriptRemoveDialogTests(DialogHost):
    def test_one_script_is_named_in_the_title(self) -> None:
        dialog = ScriptRemoveDialog(["a.py"], parent=self.host)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("a.py", dialog.title_label.text())

    def test_many_scripts_are_counted_in_the_title(self) -> None:
        dialog = ScriptRemoveDialog(["a.py", "b.py", "c.py"], parent=self.host)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("3", dialog.title_label.text())

    def test_deleting_the_file_is_opt_in(self) -> None:
        """「移除条目」与「删文件」是两件事，后者不可撤销。"""
        dialog = ScriptRemoveDialog(["a.py"], parent=self.host)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.delete_files())
        dialog.delete_check.setChecked(True)
        self.assertTrue(dialog.delete_files())


if __name__ == "__main__":
    unittest.main()
