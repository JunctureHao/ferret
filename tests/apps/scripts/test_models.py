"""脚本页模型与控制器的测试。

两处语义值得单独钉住，它们都不是「照抄重写页」能得到的：

1. **行序＝执行序** —— `FerretScriptAddon.addons` 按清单序返回脚本 ns，所以拖拽
   换序必须真的换序；而 `dropMimeData` 又刻意返回 False（数据的唯一权威副本在
   控制器手上，返回 True 会让 view 按 `InternalMove` 的约定把源行再删一遍）。
2. **删文件只对 new 条目生效** —— import 条目引用的是用户自己的文件，混选时勾了
   「同时删除文件」也不许碰（plans/scripts.md §3.4）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QModelIndex, Qt
from PySide6.QtWidgets import QApplication
from qfluentwidgets import qconfig

from ferret.apps.scripts import controllers
from ferret.apps.scripts.controllers import SCRIPT_TEMPLATE, ScriptsController
from ferret.apps.scripts.models import (
    ROW_MIME_TYPE,
    ScriptFilterProxyModel,
    ScriptTableModel,
    origin_label,
    script_name,
    state_label,
    status_summary,
    trust_warning,
)
from ferret.core.mitm import (
    SCRIPT_ORIGIN_IMPORT,
    SCRIPT_ORIGIN_NEW,
    MitmFacade,
    MitmRuntime,
    ScriptEntry,
    ScriptState,
    ScriptStatus,
)
from ferret.core.settings import CONFIG

app = QApplication.instance() or QApplication([])


def drag_payload(row: int) -> QMimeData:
    data = QMimeData()
    data.setData(ROW_MIME_TYPE, str(row).encode("ascii"))
    return data


class LabelTests(unittest.TestCase):
    def test_pending_is_its_own_state(self) -> None:
        """内核没跑时条目是「待装载」，不能拿「文件缺失」顶替。"""
        self.assertNotEqual(
            state_label(None), state_label(ScriptStatus(ScriptState.MISSING))
        )

    def test_every_state_has_a_label(self) -> None:
        for state in ScriptState:
            self.assertTrue(state_label(ScriptStatus(state)))

    def test_every_origin_has_a_label(self) -> None:
        for origin in (SCRIPT_ORIGIN_IMPORT, SCRIPT_ORIGIN_NEW):
            self.assertTrue(origin_label(origin))

    def test_unknown_origin_falls_back_to_itself(self) -> None:
        self.assertEqual(origin_label("weird"), "weird")

    def test_script_name_is_the_file_name_on_both_separators(self) -> None:
        self.assertEqual(script_name(ScriptEntry(path="C:\\tmp\\a.py")), "a.py")
        self.assertEqual(script_name(ScriptEntry(path="/tmp/b.py")), "b.py")

    def test_status_summary_prefers_the_last_traceback_line(self) -> None:
        status = ScriptStatus(ScriptState.ERROR, "Traceback\nSyntaxError: boom")
        summary = status_summary(ScriptEntry(path="a.py"), status)
        self.assertIn("SyntaxError: boom", summary)

    def test_status_summary_without_error_shows_the_path(self) -> None:
        self.assertIn("/tmp/a.py", status_summary(ScriptEntry(path="/tmp/a.py"), None))

    def test_trust_warning_is_resolved_not_a_marker(self) -> None:
        self.assertTrue(trust_warning())


class ScriptTableModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ScriptTableModel()
        self.model.set_scripts(
            [
                ScriptEntry(path="/tmp/a.py", origin=SCRIPT_ORIGIN_NEW),
                ScriptEntry(path="/tmp/b.py", enabled=False),
            ]
        )

    def test_row_and_column_counts(self) -> None:
        self.assertEqual(self.model.rowCount(), 2)
        self.assertEqual(self.model.columnCount(), 5)

    def test_check_state_reflects_enabled(self) -> None:
        role = Qt.ItemDataRole.CheckStateRole
        self.assertEqual(
            self.model.data(self.model.index(0, 0), role), Qt.CheckState.Checked
        )
        self.assertEqual(
            self.model.data(self.model.index(1, 0), role), Qt.CheckState.Unchecked
        )

    def test_display_columns(self) -> None:
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(self.model.data(self.model.index(0, 1), role), "a.py")
        self.assertEqual(
            self.model.data(self.model.index(0, 2), role),
            origin_label(SCRIPT_ORIGIN_NEW),
        )
        self.assertEqual(
            self.model.data(self.model.index(0, 3), role), state_label(None)
        )
        self.assertEqual(self.model.data(self.model.index(0, 4), role), "/tmp/a.py")

    def test_statuses_land_in_the_state_column(self) -> None:
        self.model.set_statuses({"/tmp/a.py": ScriptStatus(ScriptState.LOADED)})
        role = Qt.ItemDataRole.DisplayRole
        self.assertEqual(
            self.model.data(self.model.index(0, 3), role),
            state_label(ScriptStatus(ScriptState.LOADED)),
        )
        # 没回报过的那条仍是「待装载」。
        self.assertEqual(
            self.model.data(self.model.index(1, 3), role), state_label(None)
        )

    def test_set_statuses_repaints_only_the_state_column(self) -> None:
        """整表 reset 会吃掉选中行，所以状态回报只能走 dataChanged。"""
        seen: list[tuple[int, int]] = []
        self.model.dataChanged.connect(
            lambda top, bottom, roles: seen.append((top.column(), bottom.column()))
        )
        self.model.set_statuses({"/tmp/a.py": ScriptStatus(ScriptState.LOADED)})
        self.assertEqual(seen, [(3, 3)])

    def test_set_statuses_on_an_empty_model_is_quiet(self) -> None:
        empty = ScriptTableModel()
        seen: list[object] = []
        empty.dataChanged.connect(lambda *args: seen.append(args))
        empty.set_statuses({"/tmp/a.py": ScriptStatus(ScriptState.LOADED)})
        self.assertEqual(seen, [])

    def test_user_role_returns_the_entry(self) -> None:
        entry = self.model.data(self.model.index(1, 0), Qt.ItemDataRole.UserRole)
        self.assertEqual(entry, ScriptEntry(path="/tmp/b.py", enabled=False))

    def test_entry_at_out_of_range_is_none(self) -> None:
        self.assertIsNone(self.model.entry_at(9))

    def test_only_the_first_column_is_checkable(self) -> None:
        self.assertTrue(
            self.model.flags(self.model.index(0, 0)) & Qt.ItemFlag.ItemIsUserCheckable
        )
        self.assertFalse(
            self.model.flags(self.model.index(0, 1)) & Qt.ItemFlag.ItemIsUserCheckable
        )

    def test_rows_drag_and_blank_space_accepts_drops(self) -> None:
        self.assertTrue(
            self.model.flags(self.model.index(0, 0)) & Qt.ItemFlag.ItemIsDragEnabled
        )
        # 拖到最后一行下面（无效 index）＝移到末尾，所以空白处也要收拖拽。
        self.assertTrue(self.model.flags(QModelIndex()) & Qt.ItemFlag.ItemIsDropEnabled)

    def test_set_data_defers_the_toggle_signal(self) -> None:
        seen: list[tuple[int, bool]] = []
        self.model.enabled_toggled.connect(lambda row, on: seen.append((row, on)))
        ok = self.model.setData(
            self.model.index(1, 0),
            Qt.CheckState.Checked.value,
            Qt.ItemDataRole.CheckStateRole,
        )
        self.assertTrue(ok)
        # 控制器回头会 reset 本模型，落在 setData 里就是重入，所以推到下一轮。
        self.assertEqual(seen, [])
        app.processEvents()
        self.assertEqual(seen, [(1, True)])

    def test_set_data_ignores_other_roles_columns_and_no_ops(self) -> None:
        self.assertFalse(
            self.model.setData(self.model.index(0, 1), "x", Qt.ItemDataRole.EditRole)
        )
        self.assertFalse(
            self.model.setData(
                self.model.index(0, 1),
                Qt.CheckState.Checked.value,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        # 已经是勾选态，再勾一次什么都不发。
        self.assertFalse(
            self.model.setData(
                self.model.index(0, 0),
                Qt.CheckState.Checked.value,
                Qt.ItemDataRole.CheckStateRole,
            )
        )
        self.assertFalse(
            self.model.setData(
                self.model.index(9, 0),
                Qt.CheckState.Checked.value,
                Qt.ItemDataRole.CheckStateRole,
            )
        )

    def test_drop_never_lets_the_view_move_rows(self) -> None:
        """返回 True 会让 `startDrag` 按 InternalMove 的约定再删一遍源行。"""
        moved: list[tuple[int, int]] = []
        self.model.rows_moved.connect(lambda src, dst: moved.append((src, dst)))
        accepted = self.model.dropMimeData(
            drag_payload(0), Qt.DropAction.MoveAction, 2, 0, QModelIndex()
        )
        self.assertFalse(accepted)
        app.processEvents()
        # 落在末尾：插入位 2 是「搬走之前」的下标，往下拖要减掉自己占的那一格。
        self.assertEqual(moved, [(0, 1)])

    def test_drop_onto_a_row_uses_that_row(self) -> None:
        moved: list[tuple[int, int]] = []
        self.model.rows_moved.connect(lambda src, dst: moved.append((src, dst)))
        self.model.dropMimeData(
            drag_payload(1), Qt.DropAction.MoveAction, -1, -1, self.model.index(0, 0)
        )
        app.processEvents()
        self.assertEqual(moved, [(1, 0)])

    def test_drop_on_itself_emits_nothing(self) -> None:
        moved: list[tuple[int, int]] = []
        self.model.rows_moved.connect(lambda src, dst: moved.append((src, dst)))
        self.model.dropMimeData(
            drag_payload(0), Qt.DropAction.MoveAction, -1, -1, self.model.index(0, 0)
        )
        app.processEvents()
        self.assertEqual(moved, [])

    def test_drop_rejects_wrong_action_format_and_payload(self) -> None:
        moved: list[tuple[int, int]] = []
        self.model.rows_moved.connect(lambda src, dst: moved.append((src, dst)))
        self.assertFalse(
            self.model.dropMimeData(
                drag_payload(0), Qt.DropAction.CopyAction, 1, 0, QModelIndex()
            )
        )
        self.assertFalse(
            self.model.dropMimeData(
                QMimeData(), Qt.DropAction.MoveAction, 1, 0, QModelIndex()
            )
        )
        bad = QMimeData()
        bad.setData(ROW_MIME_TYPE, b"not-a-row")
        self.assertFalse(
            self.model.dropMimeData(bad, Qt.DropAction.MoveAction, 1, 0, QModelIndex())
        )
        self.assertFalse(
            self.model.dropMimeData(
                drag_payload(9), Qt.DropAction.MoveAction, 1, 0, QModelIndex()
            )
        )
        app.processEvents()
        self.assertEqual(moved, [])

    def test_mime_data_carries_the_first_selected_row(self) -> None:
        data = self.model.mimeData([self.model.index(1, 0), self.model.index(1, 2)])
        self.assertEqual(data.data(ROW_MIME_TYPE).data(), b"1")
        self.assertEqual(self.model.mimeTypes(), [ROW_MIME_TYPE])

    def test_headers_are_translated_strings(self) -> None:
        for col in range(self.model.columnCount()):
            self.assertTrue(
                self.model.headerData(
                    col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole
                )
            )
        self.assertIsNone(
            self.model.headerData(
                0, Qt.Orientation.Vertical, Qt.ItemDataRole.DisplayRole
            )
        )


class ScriptFilterProxyModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ScriptTableModel()
        self.source.set_scripts(
            [
                ScriptEntry(path="/tmp/alpha.py", origin=SCRIPT_ORIGIN_NEW),
                ScriptEntry(path="/opt/beta.py"),
            ]
        )
        self.proxy = ScriptFilterProxyModel()
        self.proxy.setSourceModel(self.source)

    def test_empty_filter_keeps_every_row(self) -> None:
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_filter_matches_the_name(self) -> None:
        self.proxy.set_filter_text("alpha")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_directory(self) -> None:
        self.proxy.set_filter_text("/opt/")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_matches_the_localized_origin(self) -> None:
        self.proxy.set_filter_text(origin_label(SCRIPT_ORIGIN_NEW))
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_filter_is_case_insensitive(self) -> None:
        self.proxy.set_filter_text("ALPHA")
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_no_match_yields_no_rows(self) -> None:
        self.proxy.set_filter_text("nothing-here")
        self.assertEqual(self.proxy.rowCount(), 0)


class ScriptsControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        # 配置与托管目录都改到临时目录，别碰用户真实的 config.json / scripts/。
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

    def tearDown(self) -> None:
        # 先清空再交还 CONFIG.file，免得临时目录删掉后配置里还留着坏路径。
        CONFIG.set(CONFIG.scripts, [])

    def write(self, name: str, text: str = "x = 1\n") -> str:
        target = self.root / name
        target.write_text(text, encoding="utf-8")
        return str(target)

    def test_starts_empty_and_pushes_to_the_runtime(self) -> None:
        self.assertEqual(self.controller.scripts, [])
        self.assertEqual(self.runtime.scripts, [])

    def test_scripts_dir_is_the_managed_directory(self) -> None:
        self.assertEqual(self.controller.scripts_dir, self.managed)

    def test_import_persists_and_pushes_down(self) -> None:
        path = self.write("a.py")
        self.assertTrue(self.controller.import_scripts([path]))
        self.assertEqual([e.path for e in self.controller.scripts], [path])
        self.assertEqual(self.runtime.scripts[0].path, path)
        self.assertEqual(
            CONFIG.get(CONFIG.scripts), [self.controller.scripts[0].to_dict()]
        )
        # 导入的条目保持 import 来源：它引用的是用户自己的文件，不复制、不改写。
        self.assertEqual(self.controller.scripts[0].origin, SCRIPT_ORIGIN_IMPORT)

    def test_import_skips_duplicates(self) -> None:
        path = self.write("a.py")
        self.assertTrue(self.controller.import_scripts([path]))
        self.assertFalse(self.controller.import_scripts([path]))
        self.assertEqual(len(self.controller.scripts), 1)

    def test_import_keeps_the_new_ones_when_some_are_duplicates(self) -> None:
        first, second = self.write("a.py"), self.write("b.py")
        self.controller.import_scripts([first])
        self.assertTrue(self.controller.import_scripts([first, second]))
        self.assertEqual([e.path for e in self.controller.scripts], [first, second])

    def test_create_script_writes_the_template_and_marks_it_new(self) -> None:
        path = self.controller.create_script("fresh.py")
        self.assertTrue(path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), SCRIPT_TEMPLATE)
        self.assertEqual(self.controller.scripts[0].origin, SCRIPT_ORIGIN_NEW)
        self.assertEqual(Path(path).parent, self.managed)

    def test_create_script_can_carry_given_text(self) -> None:
        """「另存为」＝以当前正文新建一条 new 条目。"""
        path = self.controller.create_script(
            "copy.py", "def request(flow):\n    pass\n"
        )
        self.assertIn("def request", Path(path).read_text(encoding="utf-8"))

    def test_create_script_reports_write_failures(self) -> None:
        failures: list[tuple[str, str]] = []
        self.controller.operation_failed.connect(
            lambda title, detail: failures.append((title, detail))
        )
        # 同名目录挡住落盘 —— 必然 OSError，正好走报错分支。
        (self.managed / "taken.py").mkdir()
        self.assertEqual(self.controller.create_script("taken.py"), "")
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.controller.scripts, [])

    def test_remove_drops_the_given_rows_and_keeps_files(self) -> None:
        for name in ("a.py", "b.py", "c.py"):
            self.controller.import_scripts([self.write(name)])
        self.assertTrue(self.controller.remove_scripts([0, 2, 99]))
        self.assertEqual([script_name(e) for e in self.controller.scripts], ["b.py"])
        self.assertTrue((self.root / "a.py").exists())

    def test_remove_with_no_valid_row_is_a_noop(self) -> None:
        self.assertFalse(self.controller.remove_scripts([7]))

    def test_delete_files_only_touches_new_entries(self) -> None:
        """import 条目指向用户自己的文件，混选时勾了删除也不许碰（§3.4）。"""
        imported = self.write("mine.py")
        self.controller.import_scripts([imported])
        created = self.controller.create_script("ours.py")
        self.assertTrue(self.controller.remove_scripts([0, 1], delete_files=True))
        self.assertTrue(Path(imported).exists())
        self.assertFalse(Path(created).exists())

    def test_delete_files_survives_an_already_gone_file(self) -> None:
        created = self.controller.create_script("gone.py")
        Path(created).unlink()
        self.assertTrue(self.controller.remove_scripts([0], delete_files=True))

    def test_set_enabled_toggles_and_persists(self) -> None:
        self.controller.import_scripts([self.write("a.py")])
        self.assertTrue(self.controller.set_enabled(0, False))
        self.assertFalse(self.controller.scripts[0].enabled)
        self.assertFalse(self.runtime.scripts[0].enabled)
        self.assertFalse(CONFIG.get(CONFIG.scripts)[0]["enabled"])
        self.assertFalse(self.controller.set_enabled(0, False))
        self.assertFalse(self.controller.set_enabled(9, True))

    def test_batch_enable_commits_once(self) -> None:
        for name in ("a.py", "b.py"):
            self.controller.import_scripts([self.write(name)])
        rounds: list[list[ScriptEntry]] = []
        self.controller.scripts_changed.connect(rounds.append)
        self.assertTrue(self.controller.set_scripts_enabled([0, 1, 9], False))
        self.assertEqual(len(rounds), 1)
        self.assertEqual([e.enabled for e in self.controller.scripts], [False, False])
        self.assertFalse(self.controller.set_scripts_enabled([0, 1], False))

    def test_move_script_reorders_because_order_is_execution_order(self) -> None:
        for name in ("a.py", "b.py", "c.py"):
            self.controller.import_scripts([self.write(name)])
        self.assertTrue(self.controller.move_script(2, -1))
        self.assertEqual(
            [script_name(e) for e in self.controller.scripts], ["a.py", "c.py", "b.py"]
        )
        self.assertTrue(self.controller.move_script_to(0, 2))
        self.assertEqual(
            [script_name(e) for e in self.controller.scripts], ["c.py", "b.py", "a.py"]
        )
        self.assertEqual(
            [Path(e.path).name for e in self.runtime.scripts],
            ["c.py", "b.py", "a.py"],
        )

    def test_move_past_either_end_is_a_noop(self) -> None:
        self.controller.import_scripts([self.write("a.py")])
        self.assertFalse(self.controller.move_script(0, -1))
        self.assertFalse(self.controller.move_script(0, 1))
        self.assertFalse(self.controller.move_script_to(5, 0))
        self.assertFalse(self.controller.move_script_to(0, 0))

    def test_index_of_and_script_at(self) -> None:
        path = self.write("a.py")
        self.controller.import_scripts([path])
        self.assertEqual(self.controller.index_of(path), 0)
        self.assertEqual(self.controller.index_of("nope.py"), -1)
        self.assertIsNotNone(self.controller.script_at(0))
        self.assertIsNone(self.controller.script_at(3))

    def test_read_script_returns_the_text(self) -> None:
        path = self.write("a.py", "y = 2\n")
        self.assertEqual(self.controller.read_script(path), "y = 2\n")

    def test_read_script_raises_for_a_missing_file(self) -> None:
        with self.assertRaises(OSError):
            self.controller.read_script(str(self.root / "ghost.py"))

    def test_save_script_writes_and_announces(self) -> None:
        path = self.controller.create_script("a.py")
        messages: list[str] = []
        self.controller.operation_succeeded.connect(messages.append)
        self.assertTrue(self.controller.save_script(path, "z = 3\n"))
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "z = 3\n")
        self.assertEqual(len(messages), 1)

    def test_save_script_reports_failures(self) -> None:
        failures: list[tuple[str, str]] = []
        self.controller.operation_failed.connect(
            lambda title, detail: failures.append((title, detail))
        )
        self.assertFalse(self.controller.save_script(str(self.managed), "x = 1\n"))
        self.assertEqual(len(failures), 1)

    def test_reload_is_a_noop_while_the_kernel_is_down(self) -> None:
        path = self.controller.create_script("a.py")
        self.assertTrue(self.controller.reload_script(path))

    def test_statuses_arrive_from_the_runtime_signal(self) -> None:
        path = self.write("a.py")
        self.controller.import_scripts([path])
        seen: list[dict] = []
        self.controller.statuses_changed.connect(seen.append)
        self.runtime.script_status_changed.emit(path, ScriptStatus(ScriptState.LOADED))
        status = self.controller.status_of(path)
        assert status is not None
        self.assertEqual(status.state, ScriptState.LOADED)
        self.assertEqual(len(seen), 1)

    def test_a_non_status_payload_is_ignored(self) -> None:
        self.runtime.script_status_changed.emit("/tmp/a.py", "nonsense")
        self.assertEqual(self.controller.statuses, {})

    def test_removing_an_entry_drops_its_stale_status(self) -> None:
        """同一路径重新加回来时不该读到上一轮的结果。"""
        path = self.write("a.py")
        self.controller.import_scripts([path])
        self.runtime.script_status_changed.emit(
            path, ScriptStatus(ScriptState.ERROR, "boom")
        )
        self.controller.remove_scripts([0])
        self.assertEqual(self.controller.statuses, {})
        self.controller.import_scripts([path])
        self.assertIsNone(self.controller.status_of(path))

    def test_kernel_stop_clears_statuses_back_to_pending(self) -> None:
        path = self.write("a.py")
        self.controller.import_scripts([path])
        self.runtime.script_status_changed.emit(path, ScriptStatus(ScriptState.LOADED))
        seen: list[dict] = []
        self.controller.statuses_changed.connect(seen.append)
        self.runtime.stopped.emit()
        self.assertEqual(self.controller.statuses, {})
        self.assertEqual(seen, [{}])
        # 已经空了就不再重复广播。
        self.runtime.stopped.emit()
        self.assertEqual(len(seen), 1)

    def test_scripts_changed_carries_a_copy(self) -> None:
        seen: list[list[ScriptEntry]] = []
        self.controller.scripts_changed.connect(seen.append)
        self.controller.import_scripts([self.write("a.py")])
        self.assertEqual(len(seen), 1)
        seen[0].clear()
        self.assertEqual(len(self.controller.scripts), 1)

    def test_an_unusable_persisted_entry_is_dropped(self) -> None:
        """坏条目留着就等于每次下发都抛 —— `apply_scripts` 是整批校验的。"""
        CONFIG.set(
            CONFIG.scripts,
            [
                {"path": "/tmp/not-python.txt", "enabled": True},
                {"path": "/tmp/fine.py", "enabled": True},
            ],
        )
        # 丢条目要留一行日志；用 assertLogs 拦住，不让 warning 冒到根 logger
        # （同进程跑完全套时根 logger 上可能挂着指向死循环的 LegacyLogEvents，
        # 冒上去就成了 RuntimeError，见 test_detail.py 同款处置）。
        with self.assertLogs("ferret.scripts", "WARNING"):
            controller = ScriptsController(mitm=MitmFacade(MitmRuntime()))
        self.assertEqual([e.path for e in controller.scripts], ["/tmp/fine.py"])

    def test_persisted_entries_are_pushed_down_on_construction(self) -> None:
        """控制器在 `runtime.start()` 之前就建好，构造时就得把清单交给 facade。"""
        CONFIG.set(CONFIG.scripts, [{"path": "/tmp/fine.py", "enabled": False}])
        runtime = MitmRuntime()
        controller = ScriptsController(mitm=MitmFacade(runtime))
        self.assertEqual(len(controller.scripts), 1)
        self.assertEqual(runtime.scripts[0].path, "/tmp/fine.py")
        self.assertFalse(runtime.scripts[0].enabled)


if __name__ == "__main__":
    unittest.main()
