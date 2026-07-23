"""Ferret 应用启动中心。

把程序启动前的全局初始化（DPI 缩放、配置加载、国际化、翻译器）
与应用主体（QApplication + MainWindow + 事件循环）统一封装，
让 __main__.py 只负责：import + 调用 run()。

参考常见桌面应用的 Application / Bootstrap 模式。
"""

import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QStandardPaths, Qt, QTranslator
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator, qconfig

from ferret.apps.window import MainWindow
from ferret.core import resources_rc  # noqa: F401  注册资源（图标/i18n/qm）
from ferret.core.settings import APP_NAME, CONFIG, CONFIG_NAME, Language


class Application:
    """应用启动中心：负责初始化并运行 ferret。

    :ivar app: 全局 QApplication 单例，run() 内创建后持有
    :ivar window: 主窗口 MainWindow 实例，run() 内创建后持有
    :ivar translators: 已安装翻译器的强引用，与 app 同生命周期
    """

    def __init__(self):
        self.app: QApplication | None = None
        self.window: MainWindow | None = None
        self.translators: list[QTranslator] = []

    # ------------------------------------------------------------------ #
    # 启动前准备（不依赖 QApplication 实例）
    # ------------------------------------------------------------------ #
    def _init_app_info(self):
        """设置应用级元信息。"""
        QCoreApplication.setApplicationName(APP_NAME)

    def _init_config(self):
        """确保配置目录存在并加载配置。

        :raises OSError: 当配置目录无法创建时
        """
        config_dir = Path(
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppConfigLocation
            )
        )
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / CONFIG_NAME
        qconfig.load(str(config_file), CONFIG)

    def _init_dpi(self):
        """根据配置应用高 DPI 缩放策略。

        必须在 QApplication 创建前设置环境变量。
        """
        dpi_scale = CONFIG.get(CONFIG.dpi_scale)
        if str(dpi_scale).upper() != "AUTO":
            os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
            os.environ["QT_SCALE_FACTOR"] = str(dpi_scale)
        else:
            os.environ.pop("QT_SCALE_FACTOR", None)
            os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    # ------------------------------------------------------------------ #
    # QApplication 创建与全局属性
    # ------------------------------------------------------------------ #
    def _create_qapp(self) -> QApplication:
        """创建 QApplication 单例并设置推荐属性。

        :returns: 已创建并配置好的 QApplication 实例
        """
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(sys.argv)
        # 防止原生窗口同级冲突（qfluentwidgets 推荐配置）
        app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)
        self.app = app
        return app

    # ------------------------------------------------------------------ #
    # 国际化
    # ------------------------------------------------------------------ #
    def _init_i18n(self):
        """加载 qfluentwidgets 翻译与自定义业务翻译。翻译器作为实例属性持有强引用，确保与同生命周期
        避免被 GC 回收导致翻译失效。"""
        if self.app is None:
            raise RuntimeError("QApplication 尚未创建，无法安装翻译器")

        lang_config = CONFIG.get(CONFIG.language)
        locale = (
            lang_config.value
            if isinstance(lang_config, Language)
            else QLocale(lang_config)
        )

        fluent_translator = FluentTranslator(locale)
        self.app.installTranslator(fluent_translator)
        self.translators.append(fluent_translator)

        setting_translator = QTranslator()
        if setting_translator.load(f":/i18n/{locale.name()}.qm"):
            self.app.installTranslator(setting_translator)
            self.translators.append(setting_translator)

    # ------------------------------------------------------------------ #
    # 主窗口
    # ------------------------------------------------------------------ #
    def _create_window(self):
        """创建并显示主窗口。"""
        self.window = MainWindow()
        self.window.show()

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def run(self):
        """按序执行所有初始化步骤并进入事件循环。

        :returns: 无；调用 sys.exit 退出进程
        """
        self._init_app_info()
        self._init_config()
        self._init_dpi()
        app = self._create_qapp()
        self._init_i18n()
        self._create_window()
        sys.exit(app.exec())
