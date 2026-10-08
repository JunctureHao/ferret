"""图片响应的原文 / 图片双视图，只消费内核交付的正文快照。"""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtWidgets import QSizePolicy, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import FluentIcon, ImageLabel, SimpleCardWidget, SubtitleLabel

from ferret.apps.common.button import TransparentTooltipButton
from ferret.apps.common.edit import Language, ToolPlainTextEdit, ToolWidget
from ferret.apps.common.icon import BaseIcon

# 压缩图片即便只有几 KB，展开后也可能占用数 GB；解码前先限制像素数。
MAX_IMAGE_PIXELS = 16 * 1024 * 1024


class ImagePreview(QWidget):
    """按可用空间等比缩小，原图尺寸不参与详情分栏的最小尺寸计算。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.label = ImageLabel(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignCenter)

    def set_image(self, image: QImage) -> None:
        self.label.setImage(image)
        self._fit_image()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image()

    def _fit_image(self) -> None:
        image = self.label.image
        if not isinstance(image, QImage) or image.isNull():
            return
        available = QSize(max(1, self.width() - 24), max(1, self.height() - 24))
        size = image.size()
        if size.width() > available.width() or size.height() > available.height():
            size.scale(available, Qt.AspectRatioMode.KeepAspectRatio)
        self.label.setScaledSize(size)


class ImageBodyPanel(QStackedWidget):
    """沿用文本 / 表格面板的顶部工具栏切换，图片页默认打开。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.text = ToolPlainTextEdit(self)
        self.text.set_read_only(True)
        self.addWidget(self.text)

        self.image_page = SimpleCardWidget(self)
        toolbar = ToolWidget(self.image_page)
        self.preview = ImagePreview(self.image_page)
        self.placeholder = SubtitleLabel(self.image_page)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setTextFormat(Qt.TextFormat.PlainText)
        self.image_stack = QStackedWidget(self.image_page)
        self.image_stack.addWidget(self.preview)
        self.image_stack.addWidget(self.placeholder)
        layout = QVBoxLayout(self.image_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.image_stack, stretch=1)
        self.addWidget(self.image_page)

        self._btn_text = TransparentTooltipButton(BaseIcon.CONVERT_TO_TEXT, self)
        self._btn_text.setToolTip(self.tr("原文"))
        self._btn_text.setAccessibleName(self.tr("原文"))
        toolbar.left_layout.addWidget(self._btn_text)
        self._btn_image = TransparentTooltipButton(FluentIcon.PHOTO, self)
        self._btn_image.setToolTip(self.tr("图片"))
        self._btn_image.setAccessibleName(self.tr("图片"))
        self.text.tool_layout.addWidget(self._btn_image)
        self._btn_text.clicked.connect(lambda: self.setCurrentWidget(self.text))
        self._btn_image.clicked.connect(lambda: self.setCurrentWidget(self.image_page))
        self.currentChanged.connect(self.updateGeometry)
        self.setCurrentWidget(self.image_page)

    def clear(self) -> None:
        self.text.set_text("")
        self.preview.set_image(QImage())
        self.placeholder.clear()
        self.image_stack.setCurrentWidget(self.preview)
        self.setCurrentWidget(self.image_page)

    def set_data(self, data: dict) -> None:
        # 原文使用解码后的正文文本，不用原生图片 contentview 的元信息摘要。
        content_type = str(data.get("Response Content-Type", "")).lower()
        self.text.set_text(
            str(data.get("Response Body Text") or ""),
            lang=Language.XML if "svg" in content_type else Language.TEXT,
            notice=str(data.get("Response Body Notice") or ""),
        )
        self.preview.set_image(QImage())
        raw = data.get("Response Body Image")
        notice = str(data.get("Response Body Image Notice") or "")
        if isinstance(raw, bytes) and raw:
            image, notice = self._read_image(raw)
            if not image.isNull():
                self.preview.set_image(image)
                self.placeholder.clear()
                self.image_stack.setCurrentWidget(self.preview)
                return
        self.placeholder.setText(
            notice or self.tr("无法预览此图片，可切换原文或导出响应体查看。")
        )
        self.image_stack.setCurrentWidget(self.placeholder)

    def _read_image(self, raw: bytes) -> tuple[QImage, str]:
        buffer = QBuffer()
        buffer.setData(raw)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.width() * size.height() > MAX_IMAGE_PIXELS:
            return QImage(), self.tr("图片尺寸超过预览限制，请导出完整响应体查看。")
        if not size.isValid():
            return QImage(), ""
        return reader.read(), ""
