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
from PySide6.QtWidgets import QApplication

from ferret.apps.intercept.editors import RequestPanel

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


if __name__ == "__main__":
    unittest.main()
