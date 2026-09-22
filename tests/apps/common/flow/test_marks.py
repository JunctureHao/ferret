"""流量标记：emoji 字典 → 显示字符、选择对话框、右键菜单的批量写回（`.plans/flow-mark.md` §5）。

四层各钉一件事：

* T-A 数据源 —— 上游 `emoji.emoji` 整本字典是唯一合法取值集，结构变了整本静默丢空
  是最坏情况，所以给条数设下限；
* T-B 选择器 —— 搜索只认短码子串、`current` 定位选中、取消零副作用、双击 = 确定；
* T-C 写回 —— 菜单「标记…」接受后 `set_flow_marked` 收到正确短码，多选逐条各调一次，
  「清除标记」送空串，选区全未标记时置灰；
* T-D 门控 —— `can_mark=False` 时两个动作都不存在（会话页天然没有标记入口）。

表格 Mark 列与概览卡格式器分别钉在 `test_models.py` / `test_fields.py`（那是它们的主场）。
翻译器**故意不装**，理由同 `test_fields.py`。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import ListView

from ferret.apps.common.flow.marks import (
    _GLYPH_ROLE,
    _SHORTCODE_ROLE,
    FALLBACK_GLYPH,
    MarkerPickerDialog,
    _marker_entries,
    marker_glyph,
    strip_shortcode,
)
from ferret.apps.common.flow.menus import FlowContextMenu
from ferret.apps.common.flow.protocols import (
    CAPTURE_CAPABILITIES,
    READONLY_CAPABILITIES,
)
from ferret.core.mitm import MARKER_DEFAULT, emoji


class EmojiTableTests(unittest.TestCase):
    """T-A：数据源是上游钉死的短码表，界面只是把它摆出来。"""

    def test_the_table_is_the_full_upstream_dictionary(self) -> None:
        """条数下限：字典结构一变（比如换成嵌套），`.items()` 会静默变成空表。"""
        self.assertGreater(len(emoji.emoji), 1500)
        self.assertEqual(len(_marker_entries()), len(emoji.emoji))

    def test_every_entry_maps_a_shortcode_to_a_non_empty_glyph(self) -> None:
        for shortcode, glyph in _marker_entries():
            with self.subTest(shortcode=shortcode):
                self.assertIsInstance(shortcode, str)
                self.assertTrue(shortcode)
                self.assertIsInstance(glyph, str)
                self.assertTrue(glyph)

    def test_emoji_shortcodes_come_first_and_bare_letters_last(self) -> None:
        """上游字典尾部还有 62 个单字母 / 数字（`"a"` → `"a"`），也是合法标记；
        它们垫底，网格一打开看到的是 emoji 而不是一排 0-9。两段各自按字典序。"""
        keys = [key for key, _ in _marker_entries()]
        shortcodes = [key for key in keys if key.startswith(":")]
        bare = [key for key in keys if not key.startswith(":")]
        self.assertEqual(keys, shortcodes + bare)
        self.assertEqual(shortcodes, sorted(shortcodes))
        self.assertEqual(bare, sorted(bare))
        self.assertIn("a", bare)


class MarkerGlyphTests(unittest.TestCase):
    """`marker_glyph` 是表格 Mark 列与概览卡共用的唯一翻译函数。"""

    def test_a_known_shortcode_renders_its_emoji(self) -> None:
        self.assertEqual(marker_glyph(":bug:"), emoji.emoji[":bug:"])

    def test_nothing_renders_as_nothing(self) -> None:
        """未标记是九成流量的常态，那一格必须是空的，不是一个兜底符号。"""
        self.assertEqual(marker_glyph(""), "")

    def test_the_old_toggles_marker_still_renders_as_it_always_did(self) -> None:
        """`:default:` 是旧开关写死的值，它**在**字典里（映射到 `"●"`）—— 走的是
        正常查表那条路，不是兜底。已经标过的流量升级后看上去不该变样。"""
        self.assertEqual(marker_glyph(MARKER_DEFAULT), emoji.emoji[MARKER_DEFAULT])
        self.assertEqual(marker_glyph(MARKER_DEFAULT), FALLBACK_GLYPH)

    def test_unknown_values_fall_back_to_the_native_symbol(self) -> None:
        """别人存的 .flow 文件、手写脚本里什么都可能有；不该抛，也不该原样把
        短码铺进那一格。兜底字符与原生 console 的 SYMBOL_MARK 同一个。"""
        self.assertEqual(marker_glyph(":no-such-emoji:"), FALLBACK_GLYPH)
        self.assertEqual(marker_glyph("free text"), FALLBACK_GLYPH)

    def test_flags_drop_the_upstream_injected_zwj(self) -> None:
        """上游在国旗两个区域指示符间塞了 ZWJ，恰恰破坏旗面合成；显示层剥掉，
        还原成合法国旗序列（两个相邻区域指示符），字体才能合成旗面。"""
        glyph = marker_glyph(":us:")
        self.assertNotIn("‍", glyph)
        self.assertEqual(glyph, "\U0001f1fa\U0001f1f8")

    def test_composite_emoji_keep_their_joiner(self) -> None:
        """家庭 / 职业合成 emoji（`:astronaut:` = 🧑‍🚀）的 ZWJ 是必需的合成粘合剂，
        不含区域指示符，绝不能跟着国旗一起被剥。"""
        self.assertIn("‍", marker_glyph(":astronaut:"))


class StripShortcodeTests(unittest.TestCase):
    """caption 层剥冒号纯函数（`:laptop_computer:` → `laptop_computer`）。"""

    def test_a_bracketed_shortcode_loses_both_colons(self) -> None:
        self.assertEqual(strip_shortcode(":laptop_computer:"), "laptop_computer")
        self.assertEqual(strip_shortcode(":bug:"), "bug")

    def test_bare_letters_are_left_alone(self) -> None:
        """字典尾部的裸字母 / 数字（`"a"`）没有冒号可剥，原样返回。"""
        self.assertEqual(strip_shortcode("a"), "a")
        self.assertEqual(strip_shortcode("1"), "1")

    def test_degenerate_inputs_do_not_crash(self) -> None:
        self.assertEqual(strip_shortcode(""), "")
        self.assertEqual(strip_shortcode(":"), ":")
        self.assertEqual(strip_shortcode("::"), "::")


class MarkerPickerDialogTests(unittest.TestCase):
    """T-B：搜索框 + 全量网格，双击或「确定」生效，取消零副作用。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def _visible_shortcodes(self, dialog: MarkerPickerDialog) -> list[str]:
        proxy = dialog.grid.model()
        assert proxy is not None
        return [
            proxy.index(row, 0).data(Qt.ItemDataRole.ToolTipRole)
            for row in range(proxy.rowCount())
        ]

    def test_the_grid_shows_the_whole_table_by_default(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        self.assertEqual(len(self._visible_shortcodes(dialog)), len(emoji.emoji))

    def test_the_grid_is_a_themed_list_view_in_icon_mode(self) -> None:
        """IconMode 方块网格：qfluentwidgets `ListView` + 全自绘方块委托，一屏看到
        更多候选。自绘 drawText 绕开 IconMode 视图刷新层缺陷（见方案 §4.1）。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        self.assertIsInstance(dialog.grid, ListView)
        self.assertEqual(dialog.grid.viewMode(), dialog.grid.ViewMode.IconMode)
        self.assertTrue(dialog.grid.isWrapping())

    def test_cells_carry_the_emoji_font(self) -> None:
        """字形经 FontRole 递给 delegate（`option.font = data(FontRole) or …`）；
        emoji-first 字体让文本态符号也画成彩色，行内拉丁短码由 YaHei 兜底。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        proxy = dialog.grid.model()
        assert proxy is not None
        font = proxy.index(0, 0).data(Qt.ItemDataRole.FontRole)
        self.assertIsNotNone(font)
        self.assertEqual(font.families()[0], "Segoe UI Emoji")

    def test_each_row_shows_the_glyph_beside_its_shortcode(self) -> None:
        """每行「glyph  短码」：认不出图形也能读名字；tooltip / 内部角色仍是纯短码
        （搜索与写回都只认短码，不能掺 glyph）。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        proxy = dialog.grid.model()
        assert proxy is not None
        first = proxy.index(0, 0)
        shortcode, glyph = _marker_entries()[0]
        display = first.data(Qt.ItemDataRole.DisplayRole)
        self.assertIn(glyph, display)
        self.assertIn(shortcode, display)
        self.assertEqual(first.data(Qt.ItemDataRole.ToolTipRole), shortcode)

    def test_the_row_carries_glyph_and_shortcode_on_separate_roles(self) -> None:
        """两级列表项自绘从 `_GLYPH_ROLE` / `_SHORTCODE_ROLE` 分两路取数
        （`.plans/0-mark-filter-polish.md` §4.1）；DisplayRole 仍留完整拼串供读屏。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        proxy = dialog.grid.model()
        assert proxy is not None
        first = proxy.index(0, 0)
        shortcode, glyph = _marker_entries()[0]
        self.assertEqual(first.data(_GLYPH_ROLE), glyph)
        self.assertEqual(first.data(_SHORTCODE_ROLE), shortcode)

    def test_the_empty_state_hides_until_a_search_finds_nothing(self) -> None:
        """首开（needle 空）恒不显示；搜到 0 行才盖出「没有匹配的标记」，清空回列表。

        用 `isHidden()`（显式隐藏标志）而非 `isVisible()`：对话框没 exec，祖先未显示时
        `isVisible()` 恒 False，测不出「有没有被要求显示」。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        self.assertTrue(dialog._empty_label.isHidden())

        dialog.search_edit.setText("zzz-no-such-marker")
        self.assertEqual(dialog.grid.model().rowCount(), 0)
        self.assertFalse(dialog._empty_label.isHidden())

        dialog.search_edit.setText("")
        self.assertTrue(dialog._empty_label.isHidden())

    def test_a_matching_search_never_shows_the_empty_state(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        dialog.search_edit.setText("bug")
        self.assertGreater(dialog.grid.model().rowCount(), 0)
        self.assertTrue(dialog._empty_label.isHidden())

    def test_searching_keeps_only_shortcodes_containing_the_text(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        dialog.search_edit.setText("bug")
        visible = self._visible_shortcodes(dialog)
        self.assertIn(":bug:", visible)
        self.assertTrue(visible)
        for shortcode in visible:
            with self.subTest(shortcode=shortcode):
                self.assertIn("bug", shortcode)

    def test_search_is_case_insensitive_and_ignores_surrounding_blanks(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        dialog.search_edit.setText("  BUG ")
        self.assertIn(":bug:", self._visible_shortcodes(dialog))

    def test_search_treats_regex_metacharacters_literally(self) -> None:
        """短码里有 `+1` / `*` 这类字符；按正则解释会要么炸要么匹配错。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        dialog.search_edit.setText("+1")
        visible = self._visible_shortcodes(dialog)
        self.assertTrue(visible)
        for shortcode in visible:
            with self.subTest(shortcode=shortcode):
                self.assertIn("+1", shortcode)

    def test_clearing_the_search_restores_everything(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        dialog.search_edit.setText("bug")
        dialog.search_edit.setText("")
        self.assertEqual(len(self._visible_shortcodes(dialog)), len(emoji.emoji))

    def test_the_current_marker_opens_selected(self) -> None:
        dialog = MarkerPickerDialog(current=":bug:", parent=self.parent)
        current = dialog.grid.currentIndex()
        self.assertTrue(current.isValid())
        self.assertEqual(current.data(Qt.ItemDataRole.ToolTipRole), ":bug:")
        self.assertTrue(dialog.yesButton.isEnabled())

    def test_an_unlocatable_current_marker_opens_with_nothing_selected(self) -> None:
        """别人存的 .flow 文件里的野短码在网格里没有位置 —— 不抛，也不乱选一个。"""
        dialog = MarkerPickerDialog(current=":no-such-emoji:", parent=self.parent)
        self.assertFalse(dialog.grid.currentIndex().isValid())
        self.assertFalse(dialog.yesButton.isEnabled())

    def test_the_old_toggles_marker_is_just_another_cell(self) -> None:
        """`:default:` 在字典里，所以旧开关标过的流量打开选择器照样定位得到。"""
        dialog = MarkerPickerDialog(current=MARKER_DEFAULT, parent=self.parent)
        current = dialog.grid.currentIndex()
        self.assertEqual(current.data(Qt.ItemDataRole.ToolTipRole), MARKER_DEFAULT)

    def test_ok_is_dead_until_something_is_picked(self) -> None:
        dialog = MarkerPickerDialog(parent=self.parent)
        self.assertFalse(dialog.yesButton.isEnabled())
        proxy = dialog.grid.model()
        assert proxy is not None
        dialog.grid.setCurrentIndex(proxy.index(3, 0))
        self.assertTrue(dialog.yesButton.isEnabled())

    def test_ok_delivers_the_picked_shortcode(self) -> None:
        dialog = MarkerPickerDialog(current=":bug:", parent=self.parent)
        dialog.yesButton.click()
        self.assertEqual(dialog.selected, ":bug:")

    def test_double_click_is_the_same_as_ok(self) -> None:
        """双击即定 —— 交付短码，并且真的把对话框按「接受」关掉。

        `accepted` 不是同步发的：`MaskDialogBase.done` 先跑 100 ms 淡出动画，
        动画结束才 `QDialog.done`。所以泵事件循环等动画收尾，不是直接断言。
        用 `QTest.qWait` 轮询而非 `QSignalSpy.wait()`：后者在整套跑下来、事件循环
        被前面用例（编辑器的 LineNumberArea 计时器等）搅过之后会零星等不到信号，
        `qWait` 持续泵循环则稳。"""
        dialog = MarkerPickerDialog(parent=self.parent)
        proxy = dialog.grid.model()
        assert proxy is not None
        accepted = QSignalSpy(dialog.accepted)
        index = proxy.index(5, 0)
        dialog.grid.doubleClicked.emit(index)

        self.assertEqual(dialog.selected, index.data(Qt.ItemDataRole.ToolTipRole))
        for _ in range(40):
            if accepted.count():
                break
            QTest.qWait(50)
        self.assertTrue(accepted.count())
        self.assertEqual(dialog.result(), int(dialog.DialogCode.Accepted))

    def test_cancel_delivers_nothing(self) -> None:
        dialog = MarkerPickerDialog(current=":bug:", parent=self.parent)
        dialog.cancelButton.click()
        self.assertIsNone(dialog.selected)


class _MarkStub:
    """只认标记这一件事的控制器替身；`fail` 模拟内核没在跑（每条都记 attempts）。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.attempts: list[str] = []
        self.fail = fail

    def set_flow_marked(self, flow_id: str, marked: str) -> None:
        self.attempts.append(flow_id)
        if self.fail:
            raise RuntimeError("kernel is not running")
        self.calls.append((flow_id, marked))


class ContextMenuMarkTests(unittest.TestCase):
    """T-C / T-D：右键菜单是标记唯一的写入端。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()
        self.controller = _MarkStub()
        self.menu = FlowContextMenu(self.parent, self.controller, CAPTURE_CAPABILITIES)

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    @staticmethod
    def _flows(*marks: str) -> list:
        flows = []
        for mark in marks:
            flow = tflow.tflow(resp=True)
            flow.marked = mark
            flows.append(flow)
        return flows

    def _select(self, flows: list) -> None:
        self.menu.update_context(0, {"id": flows[0].id}, flows)

    def _picker(self, selected: str | None, accepted: bool = True):
        dialog = MagicMock()
        dialog.exec.return_value = accepted
        dialog.selected = selected
        return patch(
            "ferret.apps.common.flow.menus.MarkerPickerDialog", return_value=dialog
        )

    def _view_texts(self, menu) -> list[str]:
        """qfw RoundMenu 把动作与子菜单一起按序摆进 `view`（子菜单不进 `actions()`），
        取带序的展示文本。"""
        return [menu.view.item(i).text().strip() for i in range(menu.view.count())]

    def test_the_mark_submenu_sits_right_before_the_comment_entry(self) -> None:
        """两个平铺项收进「标记」浮动子菜单（`.plans/0-mark-filter-polish.md` §3）；
        标记 / 备注是人肉标注两件套，挨着放。"""
        self.assertIn(self.menu.mark_menu, self.menu._subMenus)
        texts = self._view_texts(self.menu)
        self.assertEqual(texts[texts.index("标记") :][:2], ["标记", "备注..."])

    def test_the_submenu_holds_set_toggle_and_clear(self) -> None:
        submenu = self.menu.mark_menu
        texts = [a.text() for a in submenu.actions() if a.text()]
        self.assertEqual(texts, ["设置标记…", "切换标记", "清除标记"])

    def test_readonly_capabilities_leave_no_mark_entry_at_all(self) -> None:
        """会话页那批流量是死对象，写回无处可去 —— 入口整个不出现，不是置灰。"""
        menu = FlowContextMenu(self.parent, _MarkStub(), READONLY_CAPABILITIES)
        self.assertNotIn(menu.mark_menu, menu._subMenus)
        self.assertNotIn("标记", self._view_texts(menu))

    def test_picking_a_marker_writes_it_to_the_single_selected_flow(self) -> None:
        flows = self._flows("")
        self._select(flows)
        with self._picker(":bug:") as factory:
            self.menu.mark_menu.set_action.trigger()
        # 未标记的流量打开选择器时没有可定位的当前项。
        self.assertEqual(factory.call_args.kwargs["current"], "")
        self.assertEqual(self.controller.calls, [(flows[0].id, ":bug:")])

    def test_the_picker_opens_on_the_first_selected_flows_marker(self) -> None:
        flows = self._flows(":skull:", "")
        self._select(flows)
        with self._picker(None, accepted=False) as factory:
            self.menu.mark_menu.set_action.trigger()
        self.assertEqual(factory.call_args.kwargs["current"], ":skull:")

    def test_a_multi_selection_gets_the_same_marker_flow_by_flow(self) -> None:
        flows = self._flows("", ":skull:", "")
        self._select(flows)
        with self._picker(":fire:"):
            self.menu.mark_menu.set_action.trigger()
        self.assertEqual(self.controller.calls, [(flow.id, ":fire:") for flow in flows])

    def test_cancelling_the_picker_writes_nothing(self) -> None:
        self._select(self._flows(""))
        with self._picker(None, accepted=False):
            self.menu.mark_menu.set_action.trigger()
        self.assertEqual(self.controller.calls, [])

    def test_accepting_without_a_pick_writes_nothing(self) -> None:
        """「确定」在无选中时是灰的；旁路进来的 accept 也得是空转。"""
        self._select(self._flows(""))
        with self._picker(None, accepted=True):
            self.menu.mark_menu.set_action.trigger()
        self.assertEqual(self.controller.calls, [])

    def test_clearing_sends_the_empty_string_to_every_selected_flow(self) -> None:
        flows = self._flows(":bug:", ":fire:")
        self._select(flows)
        self.menu.mark_menu.clear_action.trigger()
        self.assertEqual(self.controller.calls, [(flow.id, "") for flow in flows])

    def test_clear_is_dead_when_nothing_in_the_selection_is_marked(self) -> None:
        self._select(self._flows("", ""))
        self.assertFalse(self.menu.mark_menu.clear_action.isEnabled())

    def test_clear_is_live_when_any_selected_flow_is_marked(self) -> None:
        self._select(self._flows("", ":bug:"))
        self.assertTrue(self.menu.mark_menu.clear_action.isEnabled())
        self._select(self._flows(":bug:"))
        self.assertTrue(self.menu.mark_menu.clear_action.isEnabled())

    def test_set_and_toggle_are_always_enabled(self) -> None:
        """「设置」「切换」恒可用；只有「清除」随选区标记态置灰。"""
        for marks in ([""], ["", ""], [":bug:"], [":bug:", ""]):
            with self.subTest(marks=marks):
                self._select(self._flows(*marks))
                self.assertTrue(self.menu.mark_menu.set_action.isEnabled())
                self.assertTrue(self.menu.mark_menu.toggle_action.isEnabled())

    def test_toggle_marks_the_unmarked_and_clears_the_marked_flow_by_flow(self) -> None:
        """逐 flow 翻转：无标记→ `:default:`、有标记→空串（原生 mark.toggle 语义）。"""
        flows = self._flows("", ":bug:", "")
        self._select(flows)
        self.menu.mark_menu.toggle_action.trigger()
        self.assertEqual(
            self.controller.calls,
            [(flows[0].id, ":default:"), (flows[1].id, ""), (flows[2].id, ":default:")],
        )

    def test_a_failed_single_write_reports_the_reason(self) -> None:
        self.menu.controller = _MarkStub(fail=True)
        self._select(self._flows(""))
        with (
            self._picker(":bug:"),
            patch("ferret.apps.common.flow.menus.show_warning") as warning,
        ):
            self.menu.mark_menu.set_action.trigger()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "kernel is not running")

    def test_a_failed_batch_keeps_going_and_reports_the_tally(self) -> None:
        """半截失败不该拖死整批：每条都试，最后汇总一句。"""
        stub = _MarkStub(fail=True)
        self.menu.controller = stub
        flows = self._flows("", "", "")
        self._select(flows)
        with (
            self._picker(":bug:"),
            patch("ferret.apps.common.flow.menus.show_warning") as warning,
        ):
            self.menu.mark_menu.set_action.trigger()
        self.assertEqual(stub.attempts, [flow.id for flow in flows])
        warning.assert_called_once()
        self.assertIn("3", warning.call_args.args[1])
        self.assertIn("kernel is not running", warning.call_args.args[1])

    def test_the_submenu_icons_are_distinct_within_the_popup_path(self) -> None:
        """同一弹出路径内图标语义一对一：清除标记避开 DELETE（撞删除流量）、切换
        避开 SYNC（撞重发），设置保持 TAG（`.plans/0-mark-filter-polish.md` §4.3）。"""
        from qfluentwidgets import FluentIcon

        submenu = self.menu.mark_menu
        icons = {
            submenu.set_action.icon().cacheKey(),
            submenu.toggle_action.icon().cacheKey(),
            submenu.clear_action.icon().cacheKey(),
        }
        self.assertEqual(len(icons), 3)  # 三个动作图标互不相同
        delete_key = FluentIcon.DELETE.icon().cacheKey()
        sync_key = FluentIcon.SYNC.icon().cacheKey()
        self.assertNotIn(submenu.clear_action.icon().cacheKey(), {delete_key})
        self.assertNotIn(submenu.toggle_action.icon().cacheKey(), {sync_key})


if __name__ == "__main__":
    unittest.main()
