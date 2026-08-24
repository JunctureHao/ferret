"""断点页两个对话框的测试：规则表单，和关窗时那个三选一确认。

规则表单是 匹配对象 + 条件 + 值 三栏，**没有阶段** —— 命中的流量请求期、响应期各停
一次，选择本身就不该存在。要守的有三条：

1. **表单里不该出现阶段** —— 预览的表达式也不带 ~q / ~s，措辞和真正下发的东西对齐。
2. **预览给的必须是真正要下发的那截表达式** —— 用户唯一能看见「忽略大小写」到底加
   没加的地方就是它。
3. **过不了原生解析器就不让保存** —— `options.update` 是原子的，一条坏表达式会把
   整批规则连坐回滚，界面上却只剩一句原生英文。

`MessageBoxBase` 会读 `parent.width()`，所以每个用例都得有一个真的宿主窗口。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.intercept.dialogs import (
    HeldFlowsChoice,
    HeldFlowsCloseDialog,
    InterceptRuleDialog,
)
from ferret.core.mitm import (
    InterceptField,
    InterceptLogic,
    InterceptRule,
)

app = QApplication.instance() or QApplication([])

_FIELDS = list(InterceptField)
_LOGICS = list(InterceptLogic)


class InterceptDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(900, 700)
        self.addCleanup(self.host.deleteLater)

    def dialog(self, rule: InterceptRule | None = None) -> InterceptRuleDialog:
        dlg = InterceptRuleDialog("测试", rule, self.host)
        self.addCleanup(dlg.deleteLater)
        return dlg

    @staticmethod
    def choose(dlg: InterceptRuleDialog, **kwargs) -> None:
        """按枚举值选中下拉项，走的是真的 `currentIndexChanged` 那条路。"""
        if "field" in kwargs:
            dlg.field_combo.setCurrentIndex(_FIELDS.index(kwargs["field"]))
        if "logic" in kwargs:
            dlg.logic_combo.setCurrentIndex(_LOGICS.index(kwargs["logic"]))

    def test_a_blank_form_cannot_be_saved(self) -> None:
        dlg = self.dialog()
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertEqual(dlg.preview_label.text(), "Match value cannot be empty")

    def test_a_filled_form_previews_the_pushed_down_expression(self) -> None:
        dlg = self.dialog()
        dlg.value_edit.setText("api.example.com")
        self.assertTrue(dlg.yesButton.isEnabled())
        # URL + 包含：一个 `~u` 加一条转义过的正则，别的什么都不加。
        self.assertIn("~u", dlg.preview_label.text())

    def test_the_preview_carries_no_phase_selector(self) -> None:
        """不加选择器就是两个时机都拦 —— 原生 addon 那两个钩子共用这一个过滤器。"""
        dlg = self.dialog()
        dlg.value_edit.setText("api.example.com")
        preview = dlg.preview_label.text()
        self.assertNotIn("~q", preview)
        self.assertNotIn("~s", preview)

    def test_the_form_offers_no_phase_choice(self) -> None:
        """表单上不该留一个阶段下拉框去暗示这件事能选。"""
        self.assertFalse(hasattr(self.dialog(), "phase_combo"))

    def test_host_and_method_are_case_insensitive(self) -> None:
        """原生这三个选择器一个都没带 IGNORECASE，写 `get` 匹配不上 `GET` 是纯坑。"""
        for field, value in (
            (InterceptField.HOST, "API.Example.com"),
            (InterceptField.METHOD, "post"),
        ):
            with self.subTest(field=field):
                dlg = self.dialog()
                self.choose(dlg, field=field)
                dlg.value_edit.setText(value)
                self.assertIn("(?i)", dlg.preview_label.text())

    def test_the_url_field_stays_case_sensitive(self) -> None:
        """路径是区分大小写的，而且要和重写页的 `re.escape` 保持同一套语义。"""
        dlg = self.dialog()
        dlg.value_edit.setText("/V1/User")
        self.assertNotIn("(?i)", dlg.preview_label.text())

    def test_a_host_equals_rule_has_no_port_suffix(self) -> None:
        r"""原生 `~d` 匹配的是不带端口的 host，补 `(?::\d+)?` 反而永远匹配不到。"""
        dlg = self.dialog()
        self.choose(dlg, field=InterceptField.HOST, logic=InterceptLogic.EQUALS)
        dlg.value_edit.setText("api.example.com")
        self.assertNotIn(r"\d+", dlg.preview_label.text())

    def test_a_broken_regex_cannot_be_saved(self) -> None:
        dlg = self.dialog()
        self.choose(dlg, logic=InterceptLogic.REGEX)
        dlg.value_edit.setText("bad(")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("Invalid match value", dlg.preview_label.text())

    def test_the_placeholder_follows_the_field_logic_pair(self) -> None:
        dlg = self.dialog()
        self.choose(dlg, field=InterceptField.METHOD, logic=InterceptLogic.REGEX)
        self.assertEqual(dlg.value_edit.placeholderText(), "POST|PUT|PATCH")

    def test_the_hint_combines_the_two_phase_note_and_the_field(self) -> None:
        dlg = self.dialog()
        self.choose(dlg, field=InterceptField.HOST)
        hint = dlg.hint_label.text()
        # 阶段说明是固定的一句，不随选择变 —— 但必须在，用户得知道会停两次。
        self.assertIn("held twice", hint)
        self.assertIn("without the port", hint)

    def test_changing_a_choice_revalidates(self) -> None:
        """换字段/条件会换掉下发的表达式，闸门和预览都得跟着重算。"""
        dlg = self.dialog()
        dlg.value_edit.setText("api.example.com")
        before = dlg.preview_label.text()
        self.choose(dlg, field=InterceptField.HOST)
        self.assertNotEqual(dlg.preview_label.text(), before)

    def test_the_value_is_stripped(self) -> None:
        dlg = self.dialog()
        dlg.value_edit.setText("  api.example.com  ")
        self.assertEqual(dlg.get_rule().value, "api.example.com")

    def test_the_enabled_flag_comes_from_the_edited_rule(self) -> None:
        """编辑一条停用的规则不该顺手把它启用。"""
        rule = InterceptRule(value="api.example.com", enabled=False)
        self.assertFalse(self.dialog(rule).get_rule().enabled)

    def test_an_existing_rule_round_trips(self) -> None:
        rule = InterceptRule(
            field=InterceptField.METHOD,
            logic=InterceptLogic.EQUALS,
            value="POST",
        )
        self.assertEqual(self.dialog(rule).get_rule(), rule)


class HeldFlowsCloseDialogTests(unittest.TestCase):
    """关掉断点窗口时那三个出口。

    结果读 `choice` 而不是 `exec()` 的真假：三个出口里有两个都算「关得掉」，光看
    accepted/rejected 分不出「放行全部」和「保持挂起」。
    """

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(900, 700)
        self.addCleanup(self.host.deleteLater)

    def dialog(self, flow_count: int = 3) -> HeldFlowsCloseDialog:
        dlg = HeldFlowsCloseDialog(flow_count, self.host)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_cancel_is_the_default(self) -> None:
        """随手关掉这个框不能变成「放行全部」—— 默认值必须是最不做事的那个。"""
        self.assertEqual(self.dialog().choice, HeldFlowsChoice.CANCEL)

    def test_the_title_says_how_many_are_still_held(self) -> None:
        self.assertIn("3", self.dialog(3).title_label.text())

    def test_release_all_is_recorded(self) -> None:
        dlg = self.dialog()
        dlg.yesButton.click()
        self.assertEqual(dlg.choice, HeldFlowsChoice.RELEASE_ALL)

    def test_keeping_them_held_is_recorded(self) -> None:
        dlg = self.dialog()
        dlg.keep_button.click()
        self.assertEqual(dlg.choice, HeldFlowsChoice.KEEP_HELD)

    def test_cancelling_records_nothing(self) -> None:
        dlg = self.dialog()
        dlg.cancelButton.click()
        self.assertEqual(dlg.choice, HeldFlowsChoice.CANCEL)

    def test_all_three_buttons_sit_in_the_button_row(self) -> None:
        """「保持挂起」是插进原生 buttonLayout 的，插错位置会顶掉原来那两个。"""
        dlg = self.dialog()
        layout = dlg.buttonLayout
        items = [layout.itemAt(i) for i in range(layout.count())]
        widgets = [item.widget() for item in items if item is not None]
        self.assertEqual(widgets, [dlg.yesButton, dlg.keep_button, dlg.cancelButton])


if __name__ == "__main__":
    unittest.main()
