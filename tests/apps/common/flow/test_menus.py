"""导出确认弹窗（.plans/1-cut-flow-size.md §3.3）：截断流的导出门。

静默导出半截 body 是抓包工具投诉重灾区，所以选中集里只要有正文被截断的流，
导出前必须明示一次。这里钉三件事：

* 计数只算被 `ferret.body_truncated` 标记的流（复用 `truncated_body_count`）；
* 计数 > 0 才弹确认，用户取消则整个导出中止（`getSaveFileName` 都不该开）；
* 计数 == 0（没有截断流）时确认弹窗根本不出现，正常导出不被打扰。

翻译器**故意不装**（理由同 `test_marks.py`）：断言的是行为与计数，不是文案。
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.common.flow.menus import FlowContextMenu
from ferret.apps.common.flow.protocols import CAPTURE_CAPABILITIES
from ferret.core.mitm import BODY_TRUNCATED_KEY


class TruncatedExportConfirmTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()
        self.controller = MagicMock()
        self.menu = FlowContextMenu(self.parent, self.controller, CAPTURE_CAPABILITIES)
        self.export = self.menu.export_menu

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    @staticmethod
    def _flows(*truncated: bool) -> list:
        flows = []
        for is_cut in truncated:
            flow = tflow.tflow(resp=True)
            if is_cut:
                flow.metadata[BODY_TRUNCATED_KEY] = True
            flows.append(flow)
        return flows

    def _select(self, flows: list) -> None:
        self.menu.update_context(0, {"id": flows[0].id}, flows)

    def _export(self) -> None:
        # 走公开动作触发导出（确认门装在 __export_file 开头），不碰名字修饰入口。
        self.export.save_flows_action.trigger()

    def _confirm(self, accepted: bool):
        """替身确认弹窗：拦住真的 MessageBox.exec()，回放用户的选择。"""
        return patch.object(
            self.export,
            "_FlowExportMenu__confirm_truncated_export",
            return_value=accepted,
        )

    def test_no_truncated_flow_skips_the_confirmation(self) -> None:
        self._select(self._flows(False, False))
        with (
            self._confirm(True) as confirm,
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                return_value=("", ""),
            ),
        ):
            self._export()
        confirm.assert_not_called()

    def test_truncated_flow_prompts_with_the_real_counts(self) -> None:
        """选中 3 条、其中 2 条截断 → 弹窗拿到 (总数=3, 截断=2)。"""
        self._select(self._flows(True, False, True))
        with (
            self._confirm(True) as confirm,
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                return_value=("", ""),
            ),
        ):
            self._export()
        confirm.assert_called_once_with(3, 2)

    def test_cancelling_the_confirmation_aborts_before_the_save_dialog(self) -> None:
        self._select(self._flows(True))
        with (
            self._confirm(False),
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName"
            ) as save_dialog,
        ):
            self._export()
        # 用户取消：连保存对话框都不该弹，控制器更不该被调。
        save_dialog.assert_not_called()
        self.controller.save_flows.assert_not_called()

    def test_confirming_lets_the_export_proceed(self) -> None:
        self._select(self._flows(True))
        with (
            self._confirm(True),
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName"
            ) as save_dialog,
        ):
            save_dialog.return_value = ("", "")  # 保存对话框照常打开（用户随后取消）
            self._export()
        save_dialog.assert_called_once()


if __name__ == "__main__":
    unittest.main()
