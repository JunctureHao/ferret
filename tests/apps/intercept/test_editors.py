"""断点编辑器的写回测试。

钉住一条崩溃级红线：QTextDocument 会把 ``\\r\\n`` 归一成段落边界（``toPlainText``
回来只剩 ``\\n``），所以**没编辑过的体必须原样放行**，无条件 ``encode`` 写回会把
multipart 边界 / 正文签名改坏。`_body_bytes` 的「未编辑不覆盖」短路守的就是这里。
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


class BodyWritebackTests(unittest.TestCase):
    def test_unedited_crlf_body_is_passed_through_verbatim(self):
        original = b"--boundary\r\nContent-Disposition: form-data\r\n\r\nvalue\r\n--boundary--\r\n"
        panel = RequestPanel()
        panel.load(_flow_with_body(original))
        self.assertEqual(panel.edit().content, original)

    def test_edited_body_is_written_back(self):
        panel = RequestPanel()
        panel.load(_flow_with_body(b"a\r\nb\r\n"))
        # 模拟用户键入：程序化 set_text 被 _loading 闸挡着不算编辑，插入才算。
        panel.body_panel.text.code_widget.insertPlainText("x")
        edit = panel.edit()
        self.assertEqual(edit.content, panel.body_panel.plain_text().encode("utf-8"))

    def test_binary_body_is_passed_through_verbatim(self):
        original = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
        panel = RequestPanel()
        panel.load(_flow_with_body(original))
        self.assertEqual(panel.edit().content, original)


if __name__ == "__main__":
    unittest.main()
