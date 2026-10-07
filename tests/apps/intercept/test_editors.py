"""断点编辑器的写回测试。

钉住两条保真契约：

- QTextDocument 会把 ``\\r\\n`` 归一成段落边界（``toPlainText`` 回来只剩 ``\\n``），
  所以**没编辑过的体必须原样放行**，无条件 ``encode`` 写回会把 multipart 边界 /
  正文签名改坏。`_body_bytes` 的「未编辑不覆盖」短路守的就是这里 —— 未编辑时
  ``content`` 是 ``None``，内核侧整段跳过、原字节与 Content-Length 原样保留
  （HEAD/304 的长度语义不被清零，issues #89）。
- URL 与参数页都没动过时整串原样返回，不走 parse_qsl/urlencode 的重编码路径
  （issues #78）。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication, QLineEdit

from ferret.apps.intercept.editors import RequestPanel, ResponsePanel
from ferret.core.mitm.intercept import apply_request_edit, apply_response_edit

app = QApplication.instance() or QApplication([])


def _flow_with_body(content: bytes):
    flow = tflow.tflow()
    flow.request.content = content
    return flow


def _flow_with_query(url: str):
    flow = tflow.tflow()
    flow.request.url = url
    return flow


class BodyWritebackTests(unittest.TestCase):
    def test_unedited_body_is_left_unwritten(self):
        original = b"--boundary\r\nContent-Disposition: form-data\r\n\r\nvalue\r\n--boundary--\r\n"
        panel = RequestPanel()
        panel.load(_flow_with_body(original))
        # None = 体没动过：写回整段跳过，原字节（含 CRLF）绝不经过编辑器。
        self.assertIsNone(panel.edit().content)

    def test_edited_body_is_written_back(self):
        panel = RequestPanel()
        panel.load(_flow_with_body(b"a\r\nb\r\n"))
        # 模拟用户键入：程序化 set_text 被 _loading 闸挡着不算编辑，插入才算。
        panel.body_panel.text.code_widget.insertPlainText("x")
        edit = panel.edit()
        self.assertEqual(edit.content, panel.body_panel.plain_text().encode("utf-8"))

    def test_edited_back_to_original_is_also_left_unwritten(self):
        """改回原文与没改等价：跳过写回。（含 CRLF 的原文在编辑器里改不回来 ——
        Qt 一插入就归一成 \\n —— 能逐字节对上的只有无 \\r 的体。）"""
        panel = RequestPanel()
        panel.load(_flow_with_body(b"keep me"))
        panel.body_panel.text.code_widget.insertPlainText("x")
        panel.body_panel.text.code_widget.setPlainText("keep me")
        self.assertIsNone(panel.edit().content)

    def test_binary_body_is_left_unwritten(self):
        original = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        panel = RequestPanel()
        panel.load(_flow_with_body(original))
        # 二进制体只读锁着，None = 跳过：线上原字节免走解压→回压的往返。
        self.assertIsNone(panel.edit().content)


class UrlFidelityTests(unittest.TestCase):
    def test_collecting_commits_the_active_parameter_editor(self):
        panel = RequestPanel()
        self.addCleanup(panel.deleteLater)
        panel.load(_flow_with_query("https://api.test/q?a=old"))
        panel.params_panel._show_table_page()
        table = panel.params_panel.table._table_widget
        item = table.item(0, 1)
        assert item is not None
        table.editItem(item)
        editor = table.viewport().focusWidget()
        assert isinstance(editor, QLineEdit)
        editor.setText("new")
        self.assertFalse(panel._params_dirty)
        self.assertEqual(panel.edit().url, "https://api.test/q?a=new")

    def test_unedited_url_is_passed_through_verbatim(self):
        """未编辑保真（issues #78）：parse_qsl/urlencode 会把 `a%20b` 重排成
        `a+b`、丢掉无值键 —— 没动过的 URL 必须整串原样写回。"""
        panel = RequestPanel()
        panel.load(_flow_with_query("https://api.test/q?q=a%20b&flag&x=%2f"))
        self.assertEqual(panel.edit().url, "https://api.test/q?q=a%20b&flag&x=%2f")

    def test_edited_params_merge_into_the_url(self):
        """参数页动过才合并（用户编辑路径：changed 信号）。"""
        panel = RequestPanel()
        panel.load(_flow_with_query("https://api.test/q?keep=1"))
        panel.params_panel.set_items([("page", "2"), ("tag", "a b")])
        panel.params_panel.changed.emit()
        self.assertEqual(panel.edit().url, "https://api.test/q?page=2&tag=a+b")

    def test_edited_url_keeps_params_page_authoritative(self):
        """URL 栏动过走合并路径；参数页仍是权威源 —— 只在 URL 栏里敲的参数
        会被参数页覆盖（与 compose 页同一契约）。"""
        panel = RequestPanel()
        panel.load(_flow_with_query("https://api.test/q?keep=1"))
        panel.url_edit.setText("https://api.test/q?keep=1&new=1")
        self.assertEqual(panel.edit().url, "https://api.test/q?keep=1")


class HeaderFidelityTests(unittest.TestCase):
    def test_new_duplicate_header_does_not_inherit_original_raw_bytes(self):
        for at_start in (False, True):
            with self.subTest(at_start=at_start):
                panel = RequestPanel()
                self.addCleanup(panel.deleteLater)
                flow = tflow.tflow()
                flow.request.headers.fields = ((b"X-Bytes", b"caf\xe9"),)
                panel.load(flow)
                cursor = panel.headers_panel.text.code_widget.textCursor()
                cursor.movePosition(
                    QTextCursor.MoveOperation.Start
                    if at_start
                    else QTextCursor.MoveOperation.End
                )
                cursor.insertText("X-Bytes: caf\n" if at_start else "\nX-Bytes: caf")
                apply_request_edit(flow, panel.edit())
                expected = (
                    ((b"X-Bytes", b"caf"), (b"X-Bytes", b"caf\xe9"))
                    if at_start
                    else ((b"X-Bytes", b"caf\xe9"), (b"X-Bytes", b"caf"))
                )
                self.assertEqual(flow.request.headers.fields, expected)

    def test_deleting_identical_display_row_preserves_surviving_header_bytes(self):
        for mode in ("table", "text"):
            with self.subTest(mode=mode):
                panel = RequestPanel()
                self.addCleanup(panel.deleteLater)
                flow = tflow.tflow()
                flow.request.headers.fields = (
                    (b"X-Bytes", b"caf\xe9"),
                    (b"X-Bytes", b"caf\xff"),
                )
                panel.load(flow)
                if mode == "table":
                    panel.headers_panel._show_table_page()
                    panel.headers_panel.table._table_widget.removeRow(0)
                else:
                    cursor = panel.headers_panel.text.code_widget.textCursor()
                    cursor.movePosition(QTextCursor.MoveOperation.Start)
                    cursor.movePosition(
                        QTextCursor.MoveOperation.NextBlock,
                        QTextCursor.MoveMode.KeepAnchor,
                    )
                    cursor.removeSelectedText()
                apply_request_edit(flow, panel.edit())
                self.assertEqual(
                    flow.request.headers.fields, ((b"X-Bytes", b"caf\xff"),)
                )

    def test_untouched_request_and_response_headers_keep_obs_text(self):
        for panel_type, attr in (
            (RequestPanel, "request"),
            (ResponsePanel, "response"),
        ):
            with self.subTest(attr=attr):
                panel = panel_type()
                self.addCleanup(panel.deleteLater)
                flow = tflow.tflow(resp=True)
                message = getattr(flow, attr)
                original = ((b"X-Bytes", b"abc\xffxyz"), (b"X-Bytes", b"caf\xe9"))
                message.headers.fields = original
                panel.load(flow)
                if isinstance(panel, RequestPanel):
                    apply_request_edit(flow, panel.edit())
                else:
                    apply_response_edit(flow, panel.edit())
                self.assertEqual(message.headers.fields, original)

    def test_editing_another_header_keeps_the_unedited_raw_value(self):
        panel = RequestPanel()
        self.addCleanup(panel.deleteLater)
        flow = tflow.tflow()
        flow.request.headers.fields = ((b"X-Bytes", b"caf\xe9"), (b"X-Edit", b"old"))
        panel.load(flow)
        panel.headers_panel._show_table_page()
        cell = panel.headers_panel.table._table_widget.item(1, 1)
        assert cell is not None
        cell.setText("新值")
        apply_request_edit(flow, panel.edit())
        self.assertEqual(
            flow.request.headers.fields,
            ((b"X-Bytes", b"caf\xe9"), (b"X-Edit", "新值".encode())),
        )

    def test_editing_one_duplicate_does_not_substitute_the_other_raw_value(self):
        panel = RequestPanel()
        self.addCleanup(panel.deleteLater)
        flow = tflow.tflow()
        flow.request.headers.fields = (
            (b"X-Bytes", b"caf\xe9"),
            (b"X-Bytes", b"caf\xff"),
        )
        panel.load(flow)
        panel.headers_panel._show_table_page()
        cell = panel.headers_panel.table._table_widget.item(0, 1)
        assert cell is not None
        cell.setText("new")
        apply_request_edit(flow, panel.edit())
        self.assertEqual(
            flow.request.headers.fields,
            ((b"X-Bytes", b"new"), (b"X-Bytes", b"caf\xff")),
        )


class StatusCodeParsingTests(unittest.TestCase):
    """状态码判定须与 int() 同边界：isdigit 对 "²" 这类上标也 True，int() 却抛
    ValueError，异常从放行槽逃逸就是「点了放行没反应」。非十进制一律折 0，
    交给内核侧统一报「状态码必须是……」。"""

    def _panel_with_code(self, text: str) -> ResponsePanel:
        panel = ResponsePanel()
        self.addCleanup(panel.deleteLater)
        panel.load(tflow.tflow(resp=True))
        panel.code_edit.setText(text)
        return panel

    def test_superscript_digit_folds_to_zero_instead_of_raising(self):
        # 回归：int("²") 抛 ValueError，构造 ResponseEdit 时就炸在槽里。
        self.assertEqual(self._panel_with_code("²").edit().status_code, 0)

    def test_subscript_digits_folds_to_zero(self):
        self.assertEqual(self._panel_with_code("₂₀₀").edit().status_code, 0)

    def test_plain_decimal_digits_are_parsed(self):
        # isdecimal 对阿拉伯-印度数字也是 True，int() 同样收（"٤٠٤" -> 404）。
        self.assertEqual(self._panel_with_code("404").edit().status_code, 404)

    def test_non_digit_folds_to_zero(self):
        self.assertEqual(self._panel_with_code("").edit().status_code, 0)
        self.assertEqual(self._panel_with_code("abc").edit().status_code, 0)


if __name__ == "__main__":
    unittest.main()
