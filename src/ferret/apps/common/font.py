from PySide6.QtGui import QFont, QFontDatabase


class FontManager:
    _registered = False

    @classmethod
    def register(cls):
        if cls._registered:
            return

        font_map = {
            "JetBrains Mono": {
                "Regular": ":/fonts/Regular.ttf",
                "Bold": ":/fonts/Bold.ttf",
                "Italic": ":/fonts/Italic.ttf",
                "BoldItalic": ":/fonts/BoldItalic.ttf",
            },
        }

        for variants in font_map.values():
            for path in variants.values():
                QFontDatabase.addApplicationFont(path)

        cls._registered = True

    @staticmethod
    def code_font(size=10):
        # 必须**单族**。QFont 只有单族才走廉价的直接查表，多族 setFamilies 会强制
        # populate 整个 QFontDatabase —— 实测启动期 +45MB WS / +20MB Private。
        # 且要和 core/application.py::_init_font 一起保持单族才有效：任何一处多族
        # 调用都独立触发整库扫描，只堵一处实测只差 2MB（噪声级）。
        # JetBrains Mono 没有中文字形，中文 body 靠 Qt 逐字回退渲染（已验证不出
        # 豆腐块），字体库的钱推迟到用户真的查看中文内容那一刻才付
        # （实测 +42MB WS / +18MB Private），启动时不付。
        font = QFont("JetBrains Mono")
        font.setPointSize(size)
        font.setFixedPitch(True)
        return font
