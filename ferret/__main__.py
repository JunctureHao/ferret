import os
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QLocale, QStandardPaths, Qt, QTranslator
from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentTranslator, qconfig

# 确保资源文件被导入，这样 :/i18n/ 路径才有效
from ferret.config import resources_rc  # noqa: F401
from ferret.config.settings import APP_NAME, CONFIG, CONFIG_NAME, Language
from ferret.views.window import MainWindow


def main():
    """应用主函数"""

    # 1. 基础信息设置（建议在创建 App 前完成）
    QCoreApplication.setApplicationName(APP_NAME)
    # QCoreApplication.setOrganizationName("FerretStudio")  # 建议加上组织名
    # 适配高 DPI 缩放（放在最前面防止 QFont 报错）
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    # 2. 配置文件路径处理
    config_dir = Path(
        QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
    )
    config_dir.mkdir(parents=True, exist_ok=True)  # 确保配置文件夹存在
    config_file = config_dir / CONFIG_NAME
    qconfig.load(str(config_file), CONFIG)

    # 3. 系统缩放与环境变量配置
    dpi_scale = CONFIG.get(CONFIG.dpi_scale)
    if str(dpi_scale).upper() != "AUTO":
        # 如果手动指定了缩放，关闭自动缩放并强制指定倍率
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
        os.environ["QT_SCALE_FACTOR"] = str(dpi_scale)
    else:
        # 如果是自动模式，清理可能存在的环境变量干扰
        os.environ.pop("QT_SCALE_FACTOR", None)
        os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

    # 4. 创建 QApplication
    app = QApplication(sys.argv)

    # 防止原生窗口同级冲突（qfluentwidgets 推荐配置）
    app.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings)

    # 5. 国际化处理
    # 从配置获取 Language 枚举值，其 .value 通常是一个 QLocale 对象
    lang_config = CONFIG.get(CONFIG.language)
    locale = (
        lang_config.value if isinstance(lang_config, Language) else QLocale(lang_config)
    )

    fluent_translator = FluentTranslator(locale)  # 加载 qfluentwidgets 内部翻译
    setting_translator = QTranslator()  # 加载自定义业务翻译
    # 使用资源文件路径，locale.name() 通常返回 "zh_CN"
    if setting_translator.load(f":/i18n/{locale.name()}.qm"):
        app.installTranslator(setting_translator)

    app.installTranslator(fluent_translator)

    # 6. 主窗口启动
    w = MainWindow()
    w.show()

    # 执行事件循环
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
