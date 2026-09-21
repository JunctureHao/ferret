"""CSV 字段抽取（mitmproxy cut 的 GUI 等效物，规格 .plans/cut-csv-export.md）。

纯函数 `build_csv` 不碰 Qt，先独立钉；对话框与菜单接线需要 QApplication，
在 import PySide6 前设 offscreen（AGENTS §1）。
"""

import csv
import io
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class BuildCsvTests(unittest.TestCase):
    """dict 列表 + 选中 key → CSV 文本。"""

    def setUp(self) -> None:
        from ferret.apps.common.flow.csv_export import build_csv, field_label

        self.build_csv = build_csv
        self.field_label = field_label

    def _rows(self, text: str) -> list[list[str]]:
        return list(csv.reader(io.StringIO(text)))

    def test_header_uses_field_labels_in_order(self) -> None:
        text = self.build_csv([], ["Method", "Host", "Status Code"])
        rows = self._rows(text)
        self.assertEqual(
            rows[0],
            [
                self.field_label("Method"),
                self.field_label("Host"),
                self.field_label("Status Code"),
            ],
        )

    def test_one_row_per_detail_in_key_order(self) -> None:
        details = [
            {"Method": "GET", "Host": "a.com", "Status Code": 200},
            {"Method": "POST", "Host": "b.com", "Status Code": 201},
        ]
        rows = self._rows(self.build_csv(details, ["Method", "Host", "Status Code"]))
        self.assertEqual(rows[1], ["GET", "a.com", "200"])
        self.assertEqual(rows[2], ["POST", "b.com", "201"])
        self.assertEqual(len(rows), 3)  # 表头 + 两行

    def test_missing_field_becomes_empty_cell(self) -> None:
        """未完成流量缺 duration_ms/Status Code —— 空串，不报错（cut 的 missing 语义）。"""
        details = [{"Method": "GET"}]  # 没有 Status Code / duration_ms
        rows = self._rows(
            self.build_csv(details, ["Method", "Status Code", "duration_ms"])
        )
        self.assertEqual(rows[1], ["GET", "", ""])

    def test_float_duration_drops_trailing_zero(self) -> None:
        details = [{"duration_ms": 142.0}, {"duration_ms": 142.7}]
        rows = self._rows(self.build_csv(details, ["duration_ms"]))
        self.assertEqual(rows[1], ["142"])
        self.assertEqual(rows[2], ["142.7"])

    def test_comma_in_value_is_quoted_not_split(self) -> None:
        details = [{"URL": "https://a.com/x?a=1,2,3"}]
        rows = self._rows(self.build_csv(details, ["URL"]))
        self.assertEqual(rows[1], ["https://a.com/x?a=1,2,3"])


class SelectedKeysConfigTests(unittest.TestCase):
    """勾选记忆：只认已知 key，空/全失效回落默认。"""

    def setUp(self) -> None:
        from ferret.apps.common.flow import csv_export

        self.csv_export = csv_export

    def test_unknown_keys_are_filtered(self) -> None:
        from unittest.mock import patch

        with patch.object(
            self.csv_export.CONFIG, "get", return_value=["Method", "bogus"]
        ):
            self.assertEqual(self.csv_export.load_selected_keys(), ["Method"])

    def test_all_invalid_falls_back_to_default(self) -> None:
        from unittest.mock import patch

        with patch.object(self.csv_export.CONFIG, "get", return_value=["bogus"]):
            keys = self.csv_export.load_selected_keys()
            self.assertTrue(keys)  # 非空
            self.assertTrue(
                all(k in {f.key for f in self.csv_export.CSV_FIELDS} for k in keys)
            )

    def test_non_list_falls_back_to_default(self) -> None:
        from unittest.mock import patch

        with patch.object(self.csv_export.CONFIG, "get", return_value=None):
            self.assertTrue(self.csv_export.load_selected_keys())


class CsvFieldDialogTests(unittest.TestCase):
    """对话框：勾选顺序稳定、空选禁用按钮、两条出口记 action。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from PySide6.QtWidgets import QWidget

        from ferret.apps.common.flow.csv_export import CsvFieldDialog

        self.CsvFieldDialog = CsvFieldDialog
        self.parent = QWidget()

    def tearDown(self) -> None:
        self.parent.deleteLater()
        self.app.processEvents()

    def test_selected_keys_follow_field_order_not_click_order(self) -> None:
        dialog = self.CsvFieldDialog(3, ["Host", "Method"], self.parent)
        # 声明顺序里 Method 在 Host 前，返回也该如此。
        self.assertEqual(dialog.selected_keys(), ["Method", "Host"])

    def test_buttons_disabled_when_nothing_checked(self) -> None:
        dialog = self.CsvFieldDialog(3, [], self.parent)
        self.assertFalse(dialog.yesButton.isEnabled())
        self.assertFalse(dialog.clip_button.isEnabled())

    def test_buttons_enabled_with_a_selection(self) -> None:
        dialog = self.CsvFieldDialog(3, ["Method"], self.parent)
        self.assertTrue(dialog.yesButton.isEnabled())
        self.assertTrue(dialog.clip_button.isEnabled())

    def test_clip_button_records_action(self) -> None:
        dialog = self.CsvFieldDialog(3, ["Method"], self.parent)
        dialog.clip_button.click()
        self.assertEqual(dialog.result_action, "clip")

    def test_save_button_records_action(self) -> None:
        dialog = self.CsvFieldDialog(3, ["Method"], self.parent)
        dialog.yesButton.click()
        self.assertEqual(dialog.result_action, "save")


class ExportCsvMenuTests(unittest.TestCase):
    """端到端接线：FlowExportMenu 的 CSV 出口（文件写入 / 剪贴板）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from PySide6.QtWidgets import QWidget

        from ferret.apps.common.flow.protocols import CAPTURE_CAPABILITIES
        from ferret.apps.common.flow.views import FlowContextMenu

        self.parent = QWidget()
        self.parent.show()
        self.app.processEvents()

        from mitmproxy.test import tflow

        # 真 flow：__default_file_name 会读 request / timestamp_created，thin stub 不够。
        self.flows = [tflow.tflow(resp=True), tflow.tflow(resp=True)]
        details = {
            self.flows[0].id: {"Method": "GET", "Host": "a.com", "Status Code": 200},
            self.flows[1].id: {"Method": "POST", "Host": "b.com", "Status Code": 201},
        }

        class StubController:
            def flow_detail(self, flow_id):
                return details.get(flow_id, {})

        self.controller = StubController()
        self.menu = FlowContextMenu(self.parent, self.controller, CAPTURE_CAPABILITIES)
        self.menu.update_context(0, {"id": self.flows[0].id}, self.flows)

    def tearDown(self) -> None:
        self.parent.close()
        self.parent.deleteLater()
        self.app.processEvents()

    def _patch_dialog(self, action: str, keys: list[str]):
        from unittest.mock import MagicMock, patch

        dialog = MagicMock()
        dialog.exec.return_value = True
        dialog.result_action = action
        dialog.selected_keys.return_value = keys
        return patch(
            "ferret.apps.common.flow.menus.CsvFieldDialog", return_value=dialog
        )

    def test_csv_action_present_and_labelled_by_count(self) -> None:
        texts = [a.text() for a in self.menu.export_menu.actions() if a.text()]
        # 两条选区 → 文案带条数。
        self.assertIn("导出 2 条字段为 CSV…", texts)

    def test_save_writes_csv_with_header_and_rows(self) -> None:
        import csv as _csv
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "out.csv")
            with (
                self._patch_dialog("save", ["Method", "Host", "Status Code"]),
                patch(
                    "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName",
                    return_value=(target, ""),
                ),
                patch("ferret.apps.common.flow.menus.save_selected_keys"),
            ):
                self.menu.export_menu.csv_action.trigger()
                self.app.processEvents()

            with open(target, encoding="utf-8-sig", newline="") as fh:
                rows = list(_csv.reader(fh))
            self.assertEqual(len(rows), 3)  # 表头 + 两条流量
            self.assertEqual(len(rows[0]), 3)  # 三列
            self.assertEqual(rows[1], ["GET", "a.com", "200"])
            self.assertEqual(rows[2], ["POST", "b.com", "201"])

    def test_clip_copies_csv_to_clipboard(self) -> None:
        from unittest.mock import patch

        from PySide6.QtWidgets import QApplication

        with (
            self._patch_dialog("clip", ["Method", "Host"]),
            patch("ferret.apps.common.flow.menus.save_selected_keys"),
        ):
            self.menu.export_menu.csv_action.trigger()
            self.app.processEvents()

        text = QApplication.clipboard().text()
        self.assertIn("GET", text)
        self.assertIn("a.com", text)
        self.assertIn("POST", text)

    def test_cancelling_dialog_writes_nothing(self) -> None:
        import tempfile
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        dialog = MagicMock()
        dialog.exec.return_value = False  # 用户取消
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("ferret.apps.common.flow.menus.CsvFieldDialog", return_value=dialog),
            patch(
                "ferret.apps.common.flow.menus.QFileDialog.getSaveFileName"
            ) as file_dialog,
        ):
            self.menu.export_menu.csv_action.trigger()
            self.app.processEvents()
            file_dialog.assert_not_called()
            self.assertEqual(list(Path(tmp).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
