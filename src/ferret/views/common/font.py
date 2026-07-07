from PySide6.QtGui import QFontDatabase, QFont


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

        for _, variants in font_map.items():
            for path in variants.values():
                QFontDatabase.addApplicationFont(path)

        cls._registered = True

    @staticmethod
    def code_font(size=10):
        font = QFont()
        font.setFamily("JetBrains Mono")
        font.setPointSize(size)
        font.setFixedPitch(True)
        return font
