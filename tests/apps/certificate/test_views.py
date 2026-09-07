"""Tests for `CertificateInterface`.

只验界面对状态的反应（文案 / 按钮启停 / 详情行），业务一律走替身，
所以整套用例既不生成证书也不碰系统信任库。
"""

import os
import unittest
from typing import cast

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ferret.apps.certificate.controllers import CertificateController
from ferret.apps.certificate.views import STATE_ICONS, CertificateInterface
from ferret.apps.settings.views import SettingsInterface
from ferret.core.mitm import (
    EXPORT_FORMATS,
    MitmFacade,
    MitmRuntime,
    SystemCertificateService,
    TrustState,
)
from tests.apps.certificate.test_controllers import FakeService
from tests.apps.certificate.test_models import make_info

app = QApplication.instance() or QApplication([])


class CertificateInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.controller = CertificateController(
            mitm=MitmFacade(MitmRuntime()),
            service=cast(SystemCertificateService, self.service),
        )
        self.page = CertificateInterface(controller=self.controller)
        self.addCleanup(self.page.close)

    def detail_values(self) -> list[str]:
        grid = self.page.detail_grid
        values: list[str] = []
        for row in range(grid.rowCount()):
            item = grid.itemAtPosition(row, 1)
            widget = item.widget() if item is not None else None
            if isinstance(widget, QLabel):
                values.append(widget.text())
        return values

    def drain(self, timeout_ms: int = 5000) -> None:
        waited = 0
        while self.controller.busy and waited < timeout_ms:
            app.processEvents()
            QThread.msleep(5)
            waited += 5
        app.processEvents()

    def show(self, trust: TrustState, *, info: object | None = None) -> None:
        self.service.trust = trust
        self.service.info = info
        self.controller.refresh()
        self.drain()

    def test_page_is_registered_with_a_stable_object_name(self) -> None:
        self.assertEqual(self.page.objectName(), "CertificateInterface")

    def test_every_trust_state_has_an_icon(self) -> None:
        for trust in TrustState:
            self.assertIn(trust, STATE_ICONS)

    def test_export_group_offers_one_card_per_format(self) -> None:
        self.assertEqual(len(self.page.export_cards), len(EXPORT_FORMATS))
        self.assertEqual(
            len(self.page.export_group.findChildren(QPushButton)),
            len(EXPORT_FORMATS),
        )

    def test_missing_state_hides_details_and_offers_install(self) -> None:
        self.show(TrustState.MISSING)
        self.assertFalse(self.page.detail_card.isVisibleTo(self.page))
        self.assertTrue(self.page.install_btn.isEnabled())
        self.assertFalse(self.page.uninstall_btn.isEnabled())
        self.assertEqual(self.page.status_title.text(), "尚未生成 CA 证书")

    def test_trusted_state_fills_the_detail_grid(self) -> None:
        self.show(TrustState.TRUSTED, info=make_info())
        self.assertTrue(self.page.detail_card.isVisibleTo(self.page))
        self.assertEqual(self.page.detail_grid.rowCount(), 10)
        self.assertFalse(self.page.install_btn.isEnabled())
        self.assertTrue(self.page.uninstall_btn.isEnabled())

    def test_stale_state_relabels_install_as_reinstall(self) -> None:
        self.show(TrustState.STALE, info=make_info())
        self.assertEqual(self.page.install_btn.text(), "重新安装")
        self.assertTrue(self.page.install_btn.isEnabled())
        self.assertTrue(self.page.uninstall_btn.isEnabled())

    def test_unavailable_state_disables_both_actions(self) -> None:
        self.show(TrustState.UNAVAILABLE)
        self.assertFalse(self.page.install_btn.isEnabled())
        self.assertFalse(self.page.uninstall_btn.isEnabled())

    def test_detail_grid_is_rebuilt_not_appended(self) -> None:
        self.show(TrustState.TRUSTED, info=make_info())
        self.show(TrustState.TRUSTED, info=make_info(serial_hex="ffff"))
        self.assertEqual(self.page.detail_grid.rowCount(), 10)
        self.assertIn("ffff", self.detail_values())

    def test_busy_shows_progress_and_locks_the_buttons(self) -> None:
        self.controller.busy_changed.emit(True)
        app.processEvents()
        self.assertTrue(self.page.busy_ring.isVisibleTo(self.page))
        self.assertFalse(self.page.install_btn.isEnabled())
        self.assertFalse(self.page.refresh_btn.isEnabled())
        self.assertFalse(self.page.regenerate_btn.isEnabled())

    def test_leaving_busy_respects_the_current_state(self) -> None:
        self.show(TrustState.TRUSTED, info=make_info())
        self.controller.busy_changed.emit(True)
        self.controller.busy_changed.emit(False)
        app.processEvents()
        self.assertFalse(self.page.busy_ring.isVisibleTo(self.page))
        # 已安装状态下重新可用的应该是「卸载」，不是「安装」。
        self.assertFalse(self.page.install_btn.isEnabled())
        self.assertTrue(self.page.uninstall_btn.isEnabled())

    def test_skeleton_matches_the_settings_page(self) -> None:
        """版式和设置页对齐：同样的视口边距、同样的 36px 内容边距、同样的标题位置。"""
        settings = SettingsInterface()
        self.addCleanup(settings.deleteLater)
        self.assertEqual(self.page.viewportMargins(), settings.viewportMargins())
        self.assertEqual(
            self.page.expand_layout.contentsMargins(),
            settings.expand_layout.contentsMargins(),
        )
        self.assertEqual(
            self.page.expand_layout.spacing(), settings.expand_layout.spacing()
        )
        self.assertEqual(
            self.page.certificate_label.pos(), settings.setting_label.pos()
        )
        self.assertEqual(
            self.page.horizontalScrollBarPolicy(),
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
        )

    def test_action_buttons_share_one_width(self) -> None:
        """PrimaryPushSettingCard 的按钮天生比 PushSettingCard 窄 48px，必须显式拉平。"""
        buttons = [
            self.page.install_btn,
            self.page.uninstall_btn,
            self.page.regenerate_btn,
            self.page.open_dir_btn,
            *[card.button for card in self.page.export_cards],
        ]
        self.assertEqual(len({button.minimumWidth() for button in buttons}), 1)
        self.page.show()
        app.processEvents()
        self.assertEqual(len({button.width() for button in buttons}), 1)
        self.drain()
        self.page.hide()

    def test_inner_widget_background_is_transparent(self) -> None:
        """enableTransparentBackground 必须在 setWidget 之后调，否则深色主题下露白底。"""
        inner = self.page.widget()
        assert inner is not None
        self.assertEqual(inner.styleSheet(), "QWidget{background: transparent}")

    def test_buttons_stay_inside_the_viewport_when_narrow(self) -> None:
        """侧边导航栏展开后视口会被压窄，按钮不能被挤出去（长指纹曾把整页顶宽）。"""
        self.show(TrustState.STALE, info=make_info())
        self.page.show()
        for width in (760, 628, 560):
            self.page.resize(width, 700)
            app.processEvents()
            viewport = self.page.viewport().width()
            for button in (
                self.page.install_btn,
                self.page.uninstall_btn,
                self.page.refresh_btn,
                self.page.regenerate_btn,
                self.page.open_dir_btn,
            ):
                right = button.mapTo(self.page.viewport(), button.rect().topRight())
                with self.subTest(width=width, button=button.text()):
                    self.assertLessEqual(right.x(), viewport)
        self.page.hide()

    def test_dynamic_cards_grow_to_fit_their_content(self) -> None:
        """ExpandLayout 从不改子控件高度，两张动态卡片必须自己算高。"""
        self.show(TrustState.TRUSTED, info=make_info())
        self.page.resize(700, 700)
        self.page.show()
        app.processEvents()
        grid = self.page.detail_grid
        self.assertGreaterEqual(
            self.page.detail_card.height(),
            grid.heightForWidth(self.page.detail_card.width()),
        )
        for row in range(grid.rowCount()):
            item = grid.itemAtPosition(row, 1)
            label = item.widget() if item is not None else None
            if isinstance(label, QLabel):
                with self.subTest(row=row):
                    self.assertGreaterEqual(
                        label.height(), label.heightForWidth(label.width())
                    )
        self.page.hide()

    def test_showing_the_page_triggers_a_detection(self) -> None:
        self.page.show()
        app.processEvents()
        self.assertIn("trust_state", self.service.calls)
        # 收尾等任务跑完，别把后台线程留到用例结束之后。
        self.drain()
        self.page.hide()


if __name__ == "__main__":
    unittest.main()
