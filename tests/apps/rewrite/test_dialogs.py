"""`RewriteRuleDialog` 的测试：一张表单撑八种重写类型、两种形态。

这张对话框的活儿有四件，每件都有一个只在切换类型时才露头的坑：

1. **栏位随类型切换** —— 重定向两类没有「目标」栏，体两类的「重写为」是多行编辑器
   而不是单行输入框，浏览按钮只属于 `map_local`；替换两类切到消息型形态
   （方法/路径/状态码 + 头表 + 体），字段型的目标/替换栏整体退场。
2. **切换时把内容带过去** —— 单行 ↔ 多行 ↔ 消息体是三个独立控件，
   `currentIndexChanged` 触发时下拉已经是新值了，照当前类型去读会读到那个
   还空着的新栏位。
3. **取值时按类型决定能不能 strip** —— 体正则和头值/体内容里的空白是有意义的。
4. **消息型的「至少填一项」** —— 全空的替换请求/替换响应没有可执行的语义，
   必须挡在保存之前。

合法性判定本身不在这里测（那是 `tests/core/mitm/test_rewrite.py` 的活儿），这里只
验「过不了就不让保存」这条闸门有没有真的连上。

`MessageBoxBase` 会读 `parent.width()`，所以每个用例都得有一个真的宿主窗口。
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ferret.apps.rewrite.dialogs import (
    _BODY_ROW,
    _HEADERS_ROW,
    _METHOD_ROW,
    _PATH_ROW,
    _REPLACEMENT_ROW,
    _STATUS_ROW,
    _TARGET_ROW,
    RewriteRuleDialog,
)
from ferret.core.mitm import (
    REPLACE_KINDS,
    WHOLE_BODY_PATTERN,
    RewriteKind,
    RewriteLogic,
    RewriteRule,
)

app = QApplication.instance() or QApplication([])

_KINDS = list(RewriteKind)


class DialogHost(unittest.TestCase):
    """所有子类共用一个宿主窗口：`MaskDialogBase` 拿它的尺寸铺遮罩层。"""

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.resize(900, 700)
        self.addCleanup(self.host.deleteLater)

    def dialog(self, rule: RewriteRule | None = None) -> RewriteRuleDialog:
        dlg = RewriteRuleDialog("测试", rule, self.host)
        self.addCleanup(dlg.deleteLater)
        return dlg

    def pick(self, dlg: RewriteRuleDialog, kind: RewriteKind) -> None:
        """按类型选中下拉项，走的是真的 `currentIndexChanged` 那条路。"""
        dlg.kind_combo.setCurrentIndex(_KINDS.index(kind))


class LayoutPerKindTests(DialogHost):
    def test_redirect_kinds_have_no_target_row(self) -> None:
        """重定向两类没有「目标」栏，但「重写为」行必须还在。"""
        for kind in (RewriteKind.MAP_REMOTE, RewriteKind.MAP_LOCAL):
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                self.assertFalse(dlg.form.isRowVisible(_TARGET_ROW))
                self.assertTrue(dlg.form.isRowVisible(_REPLACEMENT_ROW))

    def test_no_label_leaks_out_of_the_form(self) -> None:
        """标签不进 QFormLayout 就会浮在对话框 (0,0)（「重写为」漏到标题栏的事故）。

        八种类型逐一切换后检查：对话框的**直接**子控件里不允许出现可见的
        带文字控件 —— 进了表单的标签会被 reparent 到表单宿主链上，
        只有漏加布局的才会直接挂在对话框下、停在 (0,0)。
        """
        for kind in _KINDS:
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                leaked = [
                    w
                    for w in dlg.findChildren(QWidget)
                    if w.parent() is dlg
                    and not w.isHidden()
                    and isinstance(w, QLabel)
                    and w.text()
                ]
                self.assertEqual(leaked, [])

    def test_header_and_body_kinds_show_the_target_row(self) -> None:
        for kind in _KINDS:
            if kind in (RewriteKind.MAP_REMOTE, RewriteKind.MAP_LOCAL, *REPLACE_KINDS):
                continue
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                self.assertTrue(dlg.form.isRowVisible(_TARGET_ROW))

    def test_replace_kinds_switch_to_the_message_form(self) -> None:
        """消息型形态：目标/替换栏退场，头表 + 体常驻，方法/状态码按类二选一。"""
        for kind in REPLACE_KINDS:
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                self.assertFalse(dlg.form.isRowVisible(_TARGET_ROW))
                self.assertTrue(dlg.form.isRowVisible(_HEADERS_ROW))
                self.assertTrue(dlg.form.isRowVisible(_BODY_ROW))
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_REQUEST)
        self.assertTrue(dlg.form.isRowVisible(_METHOD_ROW))
        self.assertTrue(dlg.form.isRowVisible(_PATH_ROW))
        self.assertFalse(dlg.form.isRowVisible(_STATUS_ROW))
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        self.assertFalse(dlg.form.isRowVisible(_METHOD_ROW))
        self.assertFalse(dlg.form.isRowVisible(_PATH_ROW))
        self.assertTrue(dlg.form.isRowVisible(_STATUS_ROW))

    def test_the_status_combo_offers_common_codes_and_takes_free_input(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        self.assertIn("200", [dlg.status_combo.itemText(i) for i in range(dlg.status_combo.count())])
        dlg.status_combo.setText("599")
        self.assertEqual(dlg.get_rule().status_code, 599)

    def test_only_body_kinds_get_the_multiline_editor(self) -> None:
        """体内容常是整段 JSON，单行输入框放不下。"""
        for kind in _KINDS:
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                multiline = kind in (
                    RewriteKind.MODIFY_REQUEST_BODY,
                    RewriteKind.MODIFY_RESPONSE_BODY,
                )
                self.assertEqual(dlg.replacement_stack.currentIndex(), int(multiline))

    def test_only_map_local_gets_the_browse_button(self) -> None:
        """原生要求本地路径当下就存在，让用户选比手打少一半回滚。"""
        for kind in _KINDS:
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                self.assertEqual(
                    not dlg.browse_btn.isHidden(), kind == RewriteKind.MAP_LOCAL
                )

    def test_the_labels_follow_the_kind(self) -> None:
        cases = {
            RewriteKind.MAP_LOCAL: ("", "本地文件或目录"),
            RewriteKind.MODIFY_REQUEST_HEADER: ("头名称", "头值"),
            RewriteKind.MODIFY_RESPONSE_BODY: ("体正则", "新内容"),
        }
        for kind, (target, replacement) in cases.items():
            with self.subTest(kind=kind):
                dlg = self.dialog()
                self.pick(dlg, kind)
                self.assertEqual(dlg.replacement_label.text(), replacement)
                if target:
                    self.assertEqual(dlg.target_label.text(), target)

    def test_header_kinds_warn_about_the_at_prefix(self) -> None:
        """`@` 开头会被原生当文件路径读，用户不可能自己猜到。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_HEADER)
        self.assertIn("@", dlg.hint_label.text())
        self.assertIn("只删不加", dlg.hint_label.text())

    def test_body_kinds_spell_out_the_whole_body_pattern(self) -> None:
        """「体正则留空」到底下发了什么，只能明说 —— 否则和「还没填」分不开。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        self.assertIn(WHOLE_BODY_PATTERN, dlg.hint_label.text())

    def test_only_map_remote_talks_about_replacing(self) -> None:
        """其余五类的匹配栏只用来挑流量，措辞不能讲「替换」。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MAP_REMOTE)
        self.assertIn("把 URL 中出现的这段文本换成重写目标", dlg.hint_label.text())
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_HEADER)
        self.assertIn("URL 中出现这段文本即命中", dlg.hint_label.text())

    def test_the_value_placeholder_follows_the_logic(self) -> None:
        dlg = self.dialog()
        dlg.logic_combo.setCurrentIndex(list(RewriteLogic).index(RewriteLogic.REGEX))
        self.assertIn("^https://", dlg.value_edit.placeholderText())


class KindSwitchTests(DialogHost):
    """单行 ↔ 多行换栏时内容必须跟着走：改错类型不该让人重打一遍。"""

    def test_single_line_content_survives_a_switch_to_a_body_kind(self) -> None:
        dlg = self.dialog()
        dlg.replacement_edit.setText('{"ok": true}')
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        self.assertEqual(dlg.replacement_text.text(), '{"ok": true}')
        self.assertEqual(dlg.get_rule().replacement, '{"ok": true}')

    def test_multiline_content_survives_a_switch_back(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        dlg.replacement_text.set_text("no-cache")
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_HEADER)
        self.assertEqual(dlg.replacement_edit.text(), "no-cache")
        self.assertEqual(dlg.get_rule().replacement, "no-cache")

    def test_content_survives_two_switches_in_a_row(self) -> None:
        """`_last_kind` 记的是**切换前**那个类型；记错一次内容就在中途丢了。"""
        dlg = self.dialog()
        dlg.replacement_edit.setText("carried")
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_BODY)
        self.pick(dlg, RewriteKind.MAP_LOCAL)
        self.assertEqual(dlg.get_rule().replacement, "carried")

    def test_switching_kinds_revalidates(self) -> None:
        """换类型会换掉需要填的栏位，闸门必须跟着重算。"""
        with tempfile.TemporaryDirectory() as tmp:
            dlg = self.dialog(
                RewriteRule(
                    kind=RewriteKind.MAP_LOCAL,
                    logic=RewriteLogic.CONTAINS,
                    value="api.example.com",
                    replacement=tmp,
                )
            )
            self.assertTrue(dlg.yesButton.isEnabled())
            # 头类型要的是头名称，而这条规则的「目标」栏是空的。
            self.pick(dlg, RewriteKind.MODIFY_REQUEST_HEADER)
            self.assertFalse(dlg.yesButton.isEnabled())


class GetRuleTests(DialogHost):
    def test_the_url_value_is_always_stripped(self) -> None:
        dlg = self.dialog()
        dlg.value_edit.setText("  api.example.com  ")
        self.assertEqual(dlg.get_rule().value, "api.example.com")

    def test_a_header_name_is_stripped(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_HEADER)
        dlg.target_edit.setText("  User-Agent  ")
        self.assertEqual(dlg.get_rule().target, "User-Agent")

    def test_a_body_regex_is_not_stripped(self) -> None:
        """正则里的空白是有意义的，`"code": ` 后面那个空格删不得。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        dlg.target_edit.setText('"code": ')
        self.assertEqual(dlg.get_rule().target, '"code": ')

    def test_a_local_path_is_stripped(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MAP_LOCAL)
        dlg.replacement_edit.setText("  C:/tmp/a.json  ")
        self.assertEqual(dlg.get_rule().replacement, "C:/tmp/a.json")

    def test_a_header_value_is_not_stripped(self) -> None:
        """尾随空白原样下发才是用户要的（原生自己也不 strip）。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_HEADER)
        dlg.replacement_edit.setText(" no-cache ")
        self.assertEqual(dlg.get_rule().replacement, " no-cache ")

    def test_body_content_keeps_its_trailing_newline(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_BODY)
        dlg.replacement_text.set_text('{"a": 1}\n')
        self.assertEqual(dlg.get_rule().replacement, '{"a": 1}\n')

    def test_the_enabled_flag_comes_from_the_edited_rule(self) -> None:
        """编辑一条停用的规则不该顺手把它启用。"""
        rule = RewriteRule(value="a.com", replacement="http://b.com/", enabled=False)
        self.assertFalse(self.dialog(rule).get_rule().enabled)

    def test_an_existing_rule_round_trips(self) -> None:
        rule = RewriteRule(
            kind=RewriteKind.MODIFY_RESPONSE_BODY,
            logic=RewriteLogic.REGEX,
            value=r"^https://api\.example\.com/.*",
            target=r'"code":\s*\d+',
            replacement='"code": 0',
        )
        self.assertEqual(self.dialog(rule).get_rule(), rule)


class ValidationGateTests(DialogHost):
    """`options.update` 是原子的，一条坏 spec 会让整批规则回滚 —— 所以不让保存。"""

    def test_a_blank_form_cannot_be_saved(self) -> None:
        dlg = self.dialog()
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertEqual(dlg.preview_label.text(), "匹配值不能为空")

    def test_a_complete_map_remote_rule_previews_both_halves(self) -> None:
        dlg = self.dialog()
        dlg.logic_combo.setCurrentIndex(list(RewriteLogic).index(RewriteLogic.EQUALS))
        dlg.value_edit.setText("https://api.example.com/v1/user")
        dlg.replacement_edit.setText("http://127.0.0.1:8000/v1/user")
        self.assertTrue(dlg.yesButton.isEnabled())
        self.assertIn("匹配正则：", dlg.preview_label.text())
        self.assertIn("替换为：", dlg.preview_label.text())

    def test_map_remote_needs_a_full_url_when_matching_the_whole_url(self) -> None:
        """否则是 `request.url` 的 setter 在钩子里对着真实流量抛。"""
        dlg = self.dialog()
        dlg.logic_combo.setCurrentIndex(list(RewriteLogic).index(RewriteLogic.EQUALS))
        dlg.value_edit.setText("https://api.example.com/v1/user")
        dlg.replacement_edit.setText("127.0.0.1:8000")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("必须是带协议和主机名的完整 URL", dlg.preview_label.text())

    def test_map_local_rejects_a_path_that_is_not_there_yet(self) -> None:
        """原生 `parse_map_local_spec` 用 `resolve(strict=True)`，路径必须当下存在。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MAP_LOCAL)
        dlg.value_edit.setText("api.example.com")
        dlg.replacement_edit.setText("D:/definitely/not/here.json")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("本地路径不存在或不可访问", dlg.preview_label.text())

    def test_map_local_accepts_a_real_file_and_previews_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "canned.json"
            path.write_text("{}", encoding="utf-8")
            dlg = self.dialog()
            self.pick(dlg, RewriteKind.MAP_LOCAL)
            dlg.value_edit.setText("api.example.com")
            dlg.replacement_edit.setText(str(path))
            self.assertTrue(dlg.yesButton.isEnabled())
            self.assertIn("本地路径：", dlg.preview_label.text())

    def test_a_header_rule_needs_a_name_but_not_a_value(self) -> None:
        """头值留空 = 只删不加，是合法且有意义的（原生 pop 完不 add 回去）。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_REQUEST_HEADER)
        dlg.value_edit.setText("api.example.com")
        self.assertFalse(dlg.yesButton.isEnabled())
        dlg.target_edit.setText("User-Agent")
        self.assertTrue(dlg.yesButton.isEnabled())
        self.assertIn("（删除该头）", dlg.preview_label.text())

    def test_a_body_rule_needs_neither_column_filled(self) -> None:
        """空正则 = 整体替换，空内容 = 清空 body。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        dlg.value_edit.setText("api.example.com")
        self.assertTrue(dlg.yesButton.isEnabled())
        self.assertIn(WHOLE_BODY_PATTERN, dlg.preview_label.text())

    def test_a_broken_url_regex_blames_the_url_column(self) -> None:
        """错怪另一栏比不报错更难查。"""
        dlg = self.dialog()
        dlg.logic_combo.setCurrentIndex(list(RewriteLogic).index(RewriteLogic.REGEX))
        dlg.value_edit.setText("bad(")
        dlg.replacement_edit.setText("http://127.0.0.1:8000/x")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("无效的匹配值", dlg.preview_label.text())

    def test_a_broken_backreference_blames_the_replacement(self) -> None:
        """原生从不校验 replacement，坏反向引用要等钩子里那句 `re.sub` 才炸。"""
        dlg = self.dialog()
        dlg.logic_combo.setCurrentIndex(list(RewriteLogic).index(RewriteLogic.REGEX))
        dlg.value_edit.setText(r"^https://api\.example\.com/(.*)")
        dlg.replacement_edit.setText(r"http://127.0.0.1:8000/\9")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("无效的重写目标", dlg.preview_label.text())

    def test_typing_in_the_multiline_editor_revalidates(self) -> None:
        """多行编辑器发的是自己的 `changed`，不是 `textChanged` —— 得单独连。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.MODIFY_RESPONSE_BODY)
        dlg.value_edit.setText("api.example.com")
        before = dlg.preview_label.text()
        dlg.replacement_text.code_widget.insertPlainText('{"code": 0}')
        self.assertNotEqual(dlg.preview_label.text(), before)
        self.assertIn('{"code": 0}', dlg.preview_label.text())


class MessageFormTests(DialogHost):
    """消息型两类型（替换请求/替换响应）的取值与闸门。"""

    def test_a_replace_response_round_trips(self) -> None:
        rule = RewriteRule(
            kind=RewriteKind.REPLACE_RESPONSE,
            logic=RewriteLogic.REGEX,
            value=r"^https://api\.example\.com/v1/login",
            status_code=404,
            headers=(("Content-Type", "application/json"), ("X-A", "1")),
            replacement='{"error": "not found"}',
        )
        self.assertEqual(self.dialog(rule).get_rule(), rule)

    def test_a_replace_request_round_trips(self) -> None:
        rule = RewriteRule(
            kind=RewriteKind.REPLACE_REQUEST,
            logic=RewriteLogic.CONTAINS,
            value="api.example.com",
            method="POST",
            path="/v1/login",
            headers=(("X-A", "1"),),
            replacement="payload",
        )
        self.assertEqual(self.dialog(rule).get_rule(), rule)

    def test_the_status_combo_defaults_to_200(self) -> None:
        """状态码预填默认值 200（§5 契约「状态码（默认 200）」）；清空则执行期兜底。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        dlg.value_edit.setText("api.example.com")
        dlg.message_body.set_text("ok")
        self.assertEqual(dlg.get_rule().status_code, 200)
        dlg.status_combo.setText("")
        self.assertIsNone(dlg.get_rule().status_code)
        self.assertTrue(dlg.yesButton.isEnabled())

    def test_an_empty_replace_request_cannot_be_saved(self) -> None:
        """至少填一项：全空没有可执行的语义（§5 契约）。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_REQUEST)
        dlg.value_edit.setText("api.example.com")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("至少", dlg.preview_label.text())

    def test_an_empty_replace_response_cannot_be_saved(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        dlg.value_edit.setText("api.example.com")
        dlg.status_combo.setText("")
        self.assertFalse(dlg.yesButton.isEnabled())

    def test_filling_any_one_field_opens_the_gate(self) -> None:
        # 表格页的程序化 set_items 不发 changed（只有用户编辑才发），
        # 所以先灌头表、再动一个会触发校验的栏位 —— 和真实交互同序。
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_REQUEST)
        dlg.headers_panel.set_items([("X-A", "1")])
        dlg.value_edit.setText("api.example.com")
        self.assertTrue(dlg.yesButton.isEnabled())
        self.assertIn("1 个头", dlg.preview_label.text())

    def test_a_bad_status_code_blames_the_status_row(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        dlg.value_edit.setText("api.example.com")
        dlg.status_combo.setText("abc")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("状态码必须是整数", dlg.preview_label.text())

    def test_a_method_with_whitespace_is_rejected(self) -> None:
        """「GET /x」会顺着 method 写进报文行，必须挡住。"""
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_REQUEST)
        dlg.value_edit.setText("api.example.com")
        dlg.method_edit.setText("GET /x")
        self.assertFalse(dlg.yesButton.isEnabled())
        self.assertIn("请求方法不能含空白字符", dlg.preview_label.text())

    def test_body_content_keeps_its_trailing_newline(self) -> None:
        dlg = self.dialog()
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        dlg.value_edit.setText("api.example.com")
        dlg.message_body.set_text('{"a": 1}\n')
        self.assertEqual(dlg.get_rule().replacement, '{"a": 1}\n')

    def test_field_content_is_carried_into_the_message_body(self) -> None:
        """改错类型不必重打：字段型的「重写为」内容切到替换类还在。"""
        dlg = self.dialog()
        dlg.replacement_edit.setText('{"ok": true}')
        self.pick(dlg, RewriteKind.REPLACE_RESPONSE)
        self.assertEqual(dlg.get_rule().replacement, '{"ok": true}')


if __name__ == "__main__":
    unittest.main()
