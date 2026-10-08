"""真实概览在窄面板中完整折行，复制按钮与原文始终可用。"""

from __future__ import annotations

import math
import os
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt, QTranslator
from PySide6.QtGui import QFont, QPalette, QWheelEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QTextEdit
from qfluentwidgets import (
    ColorConfigItem,
    OptionsConfigItem,
    OptionsValidator,
    QConfig,
    TextEdit,
    Theme,
    qconfig,
    setTheme,
    setThemeColor,
)

from ferret.apps.common.flow.fields import FieldCard, OverviewPane
from ferret.apps.common.flow.timing import TimingPane
from tests.core.mitm._qt import wait_until

_QM_PATH = Path(__file__).resolve().parents[4] / "src/ferret/resources/i18n/en_GB.qm"
_LONG_URL = "https://example.test/" + "a" * 512
_FINGERPRINT = "0123456789abcdef" * 4
_FLOW_ID = "01234567-89ab-cdef-0123-456789abcdef"
_COMMENT = "<b>literal & untouched</b>\n" + "备注 & <tag> " * 24


def _detail() -> dict:
    return {
        "state": "complete",
        "Method": "GET",
        "URL": _LONG_URL,
        "Status Code": 200,
        "Reason": "OK",
        "Fingerprint SHA256": _FINGERPRINT,
        "Flow ID": _FLOW_ID,
        "comment": _COMMENT,
        "Back Connection ID": "server-connection",
        "Back Address": "example.com:443",
        "Front Connection Start": 100.0,
        "Front TLS Handshake": 100.0124,
        "Back Connection Start": 100.0,
        "Back TCP Handshake": 100.0861,
        "Back TLS Handshake": 100.1501,
        "req_time": 100.2030,
        "req_timestamp_end": 100.2042,
        "res_timestamp_start": 100.5832,
        "res_time": 100.6150,
        "duration_ms": 412.0,
    }


class OverviewLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @contextmanager
    def language(self, english: bool) -> Generator[None]:
        translator = QTranslator()
        if english:
            self.assertTrue(translator.load(str(_QM_PATH)))
            self.assertTrue(self.app.installTranslator(translator))
        try:
            yield
        finally:
            if english:
                self.app.removeTranslator(translator)

    @contextmanager
    def overview(self) -> Generator[tuple[OverviewPane, TimingPane]]:
        timing = TimingPane()
        pane = OverviewPane(lead_after="时序", lead=timing)
        try:
            yield pane, timing
        finally:
            pane.close()
            pane.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def settle(self, pane: OverviewPane) -> None:
        """等布局连续三轮稳定，避免只泵一轮遗漏下一轮的 LayoutRequest。"""
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        previous = None
        stable = 0

        def settled() -> bool:
            nonlocal previous, stable
            geometry = (
                pane.size(),
                pane.viewport().size(),
                pane.container.size(),
                tuple(card.geometry() for card in pane.visible_cards()),
                tuple(
                    (text.geometry(), text.document().size())
                    for text in pane.findChildren(QTextEdit)
                    if text.isVisible()
                ),
            )
            stable = stable + 1 if geometry == previous else 0
            previous = geometry
            return stable >= 3

        self.assertTrue(wait_until(settled, timeout_ms=2000), "概览布局未稳定")

    def assert_fits(self, pane: OverviewPane) -> None:
        viewport = pane.viewport()
        self.assertLessEqual(pane.container.width(), viewport.width())
        self.assertEqual(pane.horizontalScrollBar().maximum(), 0)
        self.assertTrue(pane.visible_cards())
        for card in pane.visible_cards():
            with self.subTest(section=card.section.title):
                self.assertTrue(card.copy_button.isVisible())
                left = card.copy_button.mapTo(viewport, QPoint(0, 0)).x()
                self.assertGreaterEqual(left, 0)
                self.assertLessEqual(left + card.copy_button.width(), viewport.width())
                if not card.is_expanded():
                    continue
                for index, row in enumerate(card.rows()):
                    if row.heading:
                        continue
                    for column, expected in enumerate((row.label, row.value)):
                        item = card.grid.itemAtPosition(index, column)
                        assert item is not None
                        text = item.widget()
                        assert isinstance(text, QTextEdit)
                        with self.subTest(label=row.label, column=column):
                            self.assertEqual(text.toPlainText(), expected)
                            self.assertLessEqual(
                                math.ceil(text.document().size().height()),
                                text.viewport().height(),
                            )
                            self.assertEqual(text.horizontalScrollBar().maximum(), 0)
                            self.assertEqual(text.verticalScrollBar().maximum(), 0)

        for label in pane.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                with self.subTest(label=label.text()):
                    self.assertGreaterEqual(
                        label.height(), label.heightForWidth(label.width())
                    )

    def test_resize_collapse_and_replace_flow_in_both_languages(self) -> None:
        for english in (False, True):
            with (
                self.subTest(english=english),
                self.language(english),
                self.overview() as (
                    pane,
                    timing,
                ),
            ):
                pane.set_data(_detail())
                timing.set_data(_detail())
                for card in pane.cards:
                    card.set_expanded(True)
                pane.show()
                if english:
                    self.assertNotEqual(pane.cards[0].title_label.text(), "概要")
                else:
                    self.assertEqual(pane.cards[0].title_label.text(), "概要")
                for width in (480, 360, 280, 480):
                    with self.subTest(width=width):
                        pane.resize(width, 640)
                        self.settle(pane)
                        self.assertEqual(pane.width(), width)
                        self.assertTrue(timing.isVisible())
                        self.assertTrue(timing.pre_rows.isVisible())
                        self.assert_fits(pane)
                        for expanded in (False, True):
                            for card in pane.cards:
                                card.set_expanded(expanded)
                            self.settle(pane)
                            self.assert_fits(pane)

                for data in ({"URL": "https://example.test/short"}, _detail()):
                    pane.set_data(data)
                    timing.set_data(data)
                    pane.resize(280, 640)
                    self.settle(pane)
                    self.assertEqual(timing.isVisible(), "req_time" in data)
                    self.assert_fits(pane)

    def value_widget(
        self, pane: OverviewPane, value: str
    ) -> tuple[FieldCard, QTextEdit]:
        for card in pane.visible_cards():
            for index, row in enumerate(card.rows()):
                if not row.heading and row.value == value:
                    item = card.grid.itemAtPosition(index, 1)
                    assert item is not None
                    widget = item.widget()
                    assert isinstance(widget, QTextEdit)
                    return card, widget
        self.fail(f"未找到字段值 {value!r}")

    def test_wrapped_values_retain_the_full_plain_text_for_both_copy_actions(
        self,
    ) -> None:
        with self.overview() as (pane, timing):
            pane.set_data(_detail())
            timing.set_data(_detail())
            for card in pane.cards:
                card.set_expanded(True)
            pane.resize(280, 640)
            pane.show()
            self.settle(pane)
            self.assert_fits(pane)
            _, url = self.value_widget(pane, _LONG_URL)
            narrow_height = url.document().size().height()
            self.assertGreater(narrow_height, url.fontMetrics().height())
            for value in (_LONG_URL, _FINGERPRINT, _FLOW_ID, _COMMENT):
                with self.subTest(value=value[:32]):
                    card, text = self.value_widget(pane, value)
                    self.assertTrue(text.isReadOnly())
                    text.selectAll()
                    text.copy()
                    self.assertEqual(QApplication.clipboard().text(), value)
                    card.copy_button.click()
                    self.assertIn(value, QApplication.clipboard().text())

            pane.resize(480, 640)
            self.settle(pane)
            self.assert_fits(pane)
            self.assertLess(url.document().size().height(), narrow_height)
            self.assertEqual(url.toPlainText(), _LONG_URL)

    def test_focused_value_reflows_without_losing_selection_or_consuming_wheel(
        self,
    ) -> None:
        original_theme = qconfig.get(qconfig.themeMode)
        try:
            with self.overview() as (pane, timing):
                pane.set_data(_detail())
                timing.set_data(_detail())
                for card in pane.cards:
                    card.set_expanded(True)
                pane.resize(360, 640)
                pane.show()
                pane.activateWindow()
                self.settle(pane)
                _, text = self.value_widget(pane, _LONG_URL)
                text.setFocus(Qt.FocusReason.OtherFocusReason)
                self.assertTrue(wait_until(text.hasFocus, timeout_ms=1000))
                text.selectAll()
                selection = (text.textCursor().anchor(), text.textCursor().position())

                for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
                    with self.subTest(theme=theme):
                        setTheme(theme, save=False)
                        self.settle(pane)
                        self.assert_fits(pane)
                        self.assertTrue(text.hasFocus())
                        self.assertEqual(
                            (text.textCursor().anchor(), text.textCursor().position()),
                            selection,
                        )
                        text.copy()
                        self.assertEqual(QApplication.clipboard().text(), _LONG_URL)

                original_font = QFont(text.font())
                original_height = text.document().size().height()
                larger_font = QFont(original_font)
                larger_font.setPixelSize(text.fontMetrics().height() + 8)
                for font in (larger_font, original_font):
                    text.setFont(font)
                    self.settle(pane)
                    self.assert_fits(pane)
                    self.assertTrue(text.hasFocus())
                    self.assertEqual(
                        (text.textCursor().anchor(), text.textCursor().position()),
                        selection,
                    )
                    text.copy()
                    self.assertEqual(QApplication.clipboard().text(), _LONG_URL)
                    if font == larger_font:
                        self.assertGreater(
                            text.document().size().height(), original_height
                        )
                    else:
                        self.assertEqual(
                            text.document().size().height(), original_height
                        )

                scroll = pane.verticalScrollBar()
                self.assertGreater(scroll.maximum(), 0)
                scroll.setValue(0)
                target = text.viewport()
                position = QPoint(2, 2)
                wheel = QWheelEvent(
                    QPointF(position),
                    QPointF(target.mapToGlobal(position)),
                    QPoint(),
                    QPoint(0, -120),
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.NoScrollPhase,
                    False,
                )
                wheel.setAccepted(True)
                self.app.sendEvent(target, wheel)
                self.assertTrue(
                    wait_until(
                        lambda: scroll.value() > 0 or not wheel.isAccepted(),
                        timeout_ms=1000,
                    ),
                    "字段吞掉了滚轮，概览也没有滚动",
                )
                self.assertEqual(text.verticalScrollBar().maximum(), 0)
        finally:
            setTheme(original_theme, save=False)

    def test_native_text_colors_follow_theme_and_accent_with_owned_config(self) -> None:
        config = QConfig()
        # 独立条目避免修改 QConfig 的共享类属性；仅转发真实应用转发的主题信号。
        config.themeMode = OptionsConfigItem(
            "QFluentWidgets", "ThemeMode", Theme.LIGHT, OptionsValidator(Theme)
        )
        config.themeColor = ColorConfigItem("QFluentWidgets", "ThemeColor", "#009faa")
        config.themeChanged.connect(qconfig.themeChanged)
        try:
            with (
                patch.object(qconfig, "_cfg", config),
                patch.object(qconfig, "themeMode", config.themeMode),
                patch.object(qconfig, "themeColor", config.themeColor),
                self.overview() as (pane, timing),
            ):
                reference = TextEdit()
                try:
                    reference.setReadOnly(True)
                    reference.setPlainText(_LONG_URL)
                    reference.resize(360, 100)
                    reference.show()
                    pane.set_data(_detail())
                    timing.set_data(_detail())
                    for card in pane.cards:
                        card.set_expanded(True)
                    pane.resize(280, 640)
                    pane.show()
                    _, text = self.value_widget(pane, _LONG_URL)
                    forwarded_accents = QSignalSpy(qconfig.themeColorChanged)
                    owned_accents = QSignalSpy(config.themeColorChanged)
                    roles = (
                        QPalette.ColorRole.Text,
                        QPalette.ColorRole.Highlight,
                        QPalette.ColorRole.HighlightedText,
                    )
                    for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
                        setTheme(theme, save=False)
                        previous_highlight = None
                        for accent in ("#1266b3", "#be3c61"):
                            with self.subTest(theme=theme, accent=accent):
                                setThemeColor(accent, save=False)
                                self.settle(pane)
                                self.assert_fits(pane)
                                self.assertEqual(
                                    text.frameShape(), QFrame.Shape.NoFrame
                                )
                                self.assertEqual(text.frameWidth(), 0)
                                self.assertFalse(text.viewport().autoFillBackground())
                                for group in (
                                    QPalette.ColorGroup.Active,
                                    QPalette.ColorGroup.Inactive,
                                ):
                                    for role in roles:
                                        with self.subTest(group=group, role=role):
                                            self.assertEqual(
                                                text.palette()
                                                .color(group, role)
                                                .rgba(),
                                                reference.palette()
                                                .color(group, role)
                                                .rgba(),
                                            )
                                highlight = text.palette().color(roles[1]).rgba()
                                if previous_highlight is not None:
                                    self.assertNotEqual(highlight, previous_highlight)
                                previous_highlight = highlight

                    self.assertEqual(owned_accents.count(), 6)
                    self.assertEqual(forwarded_accents.count(), 0)
                finally:
                    reference.close()
                    reference.deleteLater()
                    self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        finally:
            config.themeChanged.disconnect(qconfig.themeChanged)
            # patch 已恢复原配置引用；同步存活控件的调色板及 QFW 原生样式。
            qconfig.themeChanged.emit(qconfig.theme)
            setTheme(qconfig.get(qconfig.themeMode), save=False)


if __name__ == "__main__":
    unittest.main()
