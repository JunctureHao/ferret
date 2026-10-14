"""更新弹窗的动作与任务中途恢复；关闭不会阻断下载或唤回托盘窗口。"""

from __future__ import annotations

import os
import unittest
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.update.dialogs import UpdateDialog
from ferret.core.settings import CONFIG
from ferret.core.update import UpdateBrief
from tests.core.mitm._qt import wait_until


def _brief(notes: str = "") -> UpdateBrief:
    return UpdateBrief("1.0.0", "1.0.1", 128, notes, "https://example.test/releases")


class UpdateDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.addCleanup(
            QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete
        )
        self.window = QWidget()
        self.window.resize(960, 780)
        self.addCleanup(self.window.deleteLater)
        self.window.show()
        self.dialog = UpdateDialog(
            current_version="1.0.0", supported=True, parent=self.window
        )
        self.dialog.show()

    def render(
        self,
        *,
        status: str,
        brief: UpdateBrief | None = None,
        ready: bool = False,
        busy: bool = False,
        error: str = "",
        progress: int = 0,
    ) -> None:
        self.dialog.render_state(
            status=status,
            brief=brief,
            last_checked=datetime(2026, 10, 11, 8, 30).astimezone(),
            error=error,
            progress=progress,
            busy=busy,
            ready=ready,
        )

    def test_compact_layout_hides_empty_metadata_and_keeps_help_accessible(
        self,
    ) -> None:
        self.assertTrue(self.dialog.status_card.isVisible())
        self.assertTrue(self.dialog.last_checked_label.isHidden())
        self.assertTrue(self.dialog.auto_update_card.toolTip())
        self.assertEqual(
            self.dialog.version_label.accessibleDescription(), self.dialog.tr("安装版")
        )

        self.render(status="latest")
        self.assertTrue(self.dialog.last_checked_label.isVisible())

    def test_manual_actions_keep_the_dialog_open(self) -> None:
        checked: list[bool] = []
        downloaded: list[bool] = []
        self.dialog.check_requested.connect(lambda: checked.append(True))
        self.dialog.download_requested.connect(lambda: downloaded.append(True))
        self.dialog.yesButton.click()
        self.assertEqual(checked, [True])
        self.assertTrue(self.dialog.isVisible())
        self.assertIs(self.dialog.auto_update_card.configItem, CONFIG.auto_check_update)

        self.render(status="available", brief=_brief())
        self.dialog.check_button.click()
        self.dialog.yesButton.click()
        self.assertEqual(checked, [True, True])
        self.assertEqual(downloaded, [True])
        self.assertTrue(self.dialog.isVisible())

    def test_restored_download_progress_allows_closing_but_no_duplicate_task(
        self,
    ) -> None:
        downloads: list[bool] = []
        self.dialog.download_requested.connect(lambda: downloads.append(True))
        self.render(status="downloading", brief=_brief(), busy=True, progress=42)
        self.assertEqual(self.dialog.progress_bar.value(), 42)
        self.assertFalse(self.dialog.yesButton.isEnabled())
        self.assertFalse(self.dialog.check_button.isEnabled())
        self.assertTrue(self.dialog.cancelButton.isEnabled())
        self.dialog.yesButton.click()
        self.assertEqual(downloads, [])
        self.dialog.cancelButton.click()
        self.assertTrue(wait_until(lambda: not self.dialog.isVisible()))

    def test_ready_does_not_reveal_a_tray_hidden_window_and_can_retry_apply(
        self,
    ) -> None:
        restarts: list[bool] = []
        self.dialog.restart_requested.connect(lambda: restarts.append(True))
        self.window.hide()
        self.render(status="ready", brief=_brief(), ready=True, progress=100)
        self.assertTrue(self.dialog.ready)
        self.assertFalse(self.window.isVisible())

        self.window.show()
        self.render(
            status="apply_failed", brief=_brief(), ready=True, error="apply failed"
        )
        self.dialog.yesButton.click()
        self.assertEqual(restarts, [True])
        self.assertTrue(self.dialog.isVisible())

    def test_download_failure_keeps_a_retry_action(self) -> None:
        downloads: list[bool] = []
        self.dialog.download_requested.connect(lambda: downloads.append(True))
        self.render(status="download_failed", brief=_brief(), error="network lost")
        self.dialog.yesButton.click()
        self.assertEqual(downloads, [True])
        self.assertEqual(self.dialog.error_label.text(), "network lost")

    def test_release_notes_are_bounded_plain_text(self) -> None:
        self.render(status="available", brief=_brief("<img src='file:///x'>" * 1000))
        self.assertLessEqual(len(self.dialog.notes_label.text()), 601)
        self.assertEqual(self.dialog.notes_label.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("2026-10-11 08:30:00", self.dialog.last_checked_label.text())

    def test_unsupported_installation_keeps_a_release_page_action(self) -> None:
        dialog = UpdateDialog(
            current_version="1.0.0", supported=False, parent=self.window
        )
        checks: list[bool] = []
        dialog.check_requested.connect(lambda: checks.append(True))
        self.assertFalse(dialog.auto_update_card.isEnabled())
        self.assertEqual(dialog.yesButton.text(), "打开发布页")
        dialog.yesButton.click()
        self.assertEqual(checks, [True])


if __name__ == "__main__":
    unittest.main()
