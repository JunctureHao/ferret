"""图片响应由内核正文快照预览，切换原文与换流量时不串内容。"""

from __future__ import annotations

import base64
import gzip
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QSize
from PySide6.QtGui import QColor, QImage, QImageWriter
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.detail import BodyPane, ResponsePane
from ferret.apps.common.flow.image import ImageBodyPanel
from ferret.core.mitm import HTTPFlow, build_flow_body, build_flow_summary
from tests.core.mitm._qt import wait_until

_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="16">\n'
    b'  <rect width="24" height="16" fill="red"/>\n'
    b"</svg>"
)
_GIF = base64.b64decode("R0lGODlhAQABAIAAAP8AAAAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==")


def _flow(content: bytes, content_type: str) -> HTTPFlow:
    flow = tflow.tflow(resp=True)
    assert flow.response is not None
    flow.response.headers["Content-Type"] = content_type
    flow.response.content = content
    return flow


def _encoded_image(format: str, size: QSize | None = None) -> bytes:
    image = QImage(size or QSize(24, 16), QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not QImageWriter(buffer, format.encode("ascii")).write(image):
        raise AssertionError(f"Qt could not encode the {format} test fixture")
    return bytes(buffer.data().data())


class ImageBodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.pane = BodyPane(allow_form=True)

    def tearDown(self) -> None:
        self.pane.close()
        self.pane.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def show_image(
        self, content: bytes = _SVG, content_type: str = "image/svg+xml"
    ) -> ImageBodyPanel:
        data = build_flow_body(_flow(content, content_type), "Response")
        self.pane.set_data(data, "Response")
        panel = self.pane.image_panel
        assert panel is not None
        return panel

    def test_common_image_formats_render_from_real_body_snapshots(self) -> None:
        cases = (
            ("image/png", _encoded_image("PNG"), QSize(24, 16)),
            ("image/jpeg", _encoded_image("JPEG"), QSize(24, 16)),
            ("image/webp", _encoded_image("WEBP"), QSize(24, 16)),
            ("image/gif", _GIF, QSize(1, 1)),
            ("IMAGE/SVG+XML; charset=utf-8", _SVG, QSize(24, 16)),
        )
        for content_type, content, size in cases:
            with self.subTest(content_type=content_type):
                panel = self.show_image(content, content_type)
                self.assertIs(self.pane.currentWidget(), panel)
                self.assertIs(panel.currentWidget(), panel.image_page)
                self.assertIs(panel.image_stack.currentWidget(), panel.preview)
                image = panel.preview.label.image
                assert isinstance(image, QImage)
                self.assertEqual(image.size(), size)
                self.assertGreater(image.pixelColor(0, 0).red(), 240)

    def test_compressed_image_is_decoded_before_display(self) -> None:
        content = _encoded_image("PNG")
        flow = _flow(content, "image/png")
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(content)
        snapshot = build_flow_body(flow, "Response")
        self.pane.set_data(snapshot, "Response")
        panel = self.pane.image_panel
        assert panel is not None
        self.assertEqual(snapshot["Response Body Image"], content)
        image = panel.preview.label.image
        assert isinstance(image, QImage)
        self.assertEqual(image.size(), QSize(24, 16))

    def test_toolbar_switches_preserve_original_text_in_read_only_editor(self) -> None:
        snapshot = build_flow_body(_flow(_SVG, "image/svg+xml"), "Response")
        snapshot["Response Body Pretty"] = "Image metadata summary"
        self.pane.set_data(snapshot, "Response")
        panel = self.pane.image_panel
        assert panel is not None
        self.assertIs(panel.currentWidget(), panel.image_page)
        self.assertTrue(panel.text.is_read_only())

        for _ in range(2):
            panel._btn_text.click()
            self.assertIs(panel.currentWidget(), panel.text)
            self.assertEqual(panel.text.text(), _SVG.decode())
            panel._btn_image.click()
            self.assertIs(panel.currentWidget(), panel.image_page)
            self.assertFalse(panel.preview.label.isNull())

    def test_request_images_and_non_image_responses_keep_existing_text_view(
        self,
    ) -> None:
        flow = _flow(b'{"ok":true}', "application/json")
        self.pane.set_data(build_flow_body(flow, "Response"), "Response")
        self.assertIsNone(self.pane.image_panel)
        self.assertIs(self.pane.currentWidget(), self.pane.json_panel)
        self.assertIn('"ok"', self.pane.json_panel.plain_text())

        flow.request.headers["Content-Type"] = "image/svg+xml"
        flow.request.content = _SVG
        self.pane.set_data(build_flow_body(flow, "Request"), "Request")
        self.assertIsNone(self.pane.image_panel)
        self.assertIs(self.pane.currentWidget(), self.pane.json_panel)

    def test_leaving_images_releases_pixels_and_hidden_original_text(self) -> None:
        for replacement in ("text", "empty", "clear"):
            with self.subTest(replacement=replacement):
                panel = self.show_image()
                panel._btn_text.click()
                if replacement == "clear":
                    self.pane.clear()
                else:
                    data = (
                        build_flow_body(_flow(b"next", "text/plain"), "Response")
                        if replacement == "text"
                        else {}
                    )
                    self.pane.set_data(data, "Response")
                self.assertTrue(panel.preview.label.isNull())
                self.assertEqual(panel.text.text(), "")
                self.assertIsNot(self.pane.currentWidget(), panel)
                self.assertIs(panel.currentWidget(), panel.image_page)

    def test_bad_or_empty_images_replace_previous_pixels_with_a_notice(self) -> None:
        for content in (b"not an image", b""):
            with self.subTest(content=content):
                panel = self.show_image()
                self.show_image(content, "image/png")
                self.assertTrue(panel.preview.label.isNull())
                self.assertIs(panel.image_stack.currentWidget(), panel.placeholder)
                self.assertTrue(panel.placeholder.text())
                panel._btn_text.click()
                self.assertEqual(panel.text.text(), content.decode())

    def test_oversized_dimensions_are_rejected_before_pixel_allocation(self) -> None:
        content = _SVG.replace(b'width="24" height="16"', b'width="5000" height="5000"')
        with patch("ferret.apps.common.flow.image.QImageReader.read") as read:
            panel = self.show_image(content)
        read.assert_not_called()
        self.assertTrue(panel.preview.label.isNull())
        self.assertIs(panel.image_stack.currentWidget(), panel.placeholder)
        self.assertIn("尺寸", panel.placeholder.text())

    def test_core_preview_notice_is_shown_and_raw_text_remains_available(self) -> None:
        snapshot = build_flow_body(_flow(_SVG, "image/svg+xml"), "Response")
        snapshot["Response Body Image"] = None
        snapshot["Response Body Image Notice"] = "图片超过预览大小限制"
        self.pane.set_data(snapshot, "Response")
        panel = self.pane.image_panel
        assert panel is not None
        self.assertEqual(
            panel.placeholder.text(), snapshot["Response Body Image Notice"]
        )
        panel._btn_text.click()
        self.assertEqual(panel.text.text(), _SVG.decode())

    def test_image_scales_to_narrow_and_wide_windows_without_distortion(self) -> None:
        panel = self.show_image(_encoded_image("PNG", QSize(1200, 600)), "image/png")
        self.pane.show()
        for size in (QSize(360, 400), QSize(800, 300)):
            with self.subTest(size=size):
                self.pane.resize(size)

                def fits() -> bool:
                    label = panel.preview.label
                    return (
                        label.width() <= panel.preview.width() - 24
                        and label.height() <= panel.preview.height() - 24
                    )

                self.assertTrue(wait_until(fits, timeout_ms=2000))
                label = panel.preview.label
                self.assertAlmostEqual(label.width() / label.height(), 2, delta=0.02)
                self.assertLess(label.width(), 1200)
                image = label.image
                assert isinstance(image, QImage)
                self.assertEqual(image.size(), QSize(1200, 600))

    def test_switching_flows_clears_hidden_preview_and_defaults_new_image_to_image(
        self,
    ) -> None:
        response = ResponsePane(self.pane, with_raw=False)
        first = _flow(_SVG, "image/svg+xml")
        response.set_data(
            build_flow_summary(first) | build_flow_body(first, "Response")
        )
        self.assertIsNone(response.body_pane)
        response.setCurrentTab("Body")
        body = response.body_pane
        assert body is not None and body.image_panel is not None
        panel = body.image_panel
        panel._btn_text.click()
        response.setCurrentTab("Headers")

        second = _flow(_encoded_image("PNG", QSize(10, 20)), "image/png")
        response.set_data(
            build_flow_summary(second) | build_flow_body(second, "Response")
        )
        self.assertTrue(panel.preview.label.isNull())
        self.assertEqual(panel.text.text(), "")
        response.setCurrentTab("Body")
        self.assertIs(panel.currentWidget(), panel.image_page)
        image = panel.preview.label.image
        assert isinstance(image, QImage)
        self.assertEqual(image.size(), QSize(10, 20))


if __name__ == "__main__":
    unittest.main()
