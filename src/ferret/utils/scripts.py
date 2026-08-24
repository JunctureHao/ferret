"""i18n 与资源的三步流水线：lupdate 提词 → lrelease 编 qm → rcc 编进 Python 模块。

三处踩过的坑，动这个文件前先读：

* **lupdate 不能 `-recursive` 整个包。** `core/resources_rc.py` 是 rcc 生成的几 MB 单文件
  （一整块 bytes 字面量），喂给 lupdate 会让它以 `0xC0000409`
  （STATUS_STACK_BUFFER_OVERRUN）直接崩掉，连一行报错都不留 —— 历史上 `zh_CN.ts`
  停更在 `view/` 时代就是因为这个。所以这里显式列文件并把它排除。
* **rcc 的输出必须落在 `core/resources_rc.py`。** 应用导入的是 `ferret.core.resources_rc`
  （见 `core/application.py`），写到别处等于白编：qm 进了 qrc，程序还在读旧的那份。
* **源语言是英文。** 代码里的界面文案一律写英文，中文由 `zh_CN.ts` 提供。新增文案后必须
  重跑这条流水线，否则界面上就是原样的英文源文本（`tr()` 查不到时的行为）。

模块级的字面量表只能用 `QT_TRANSLATE_NOOP` 做标记、到使用点再
`QCoreApplication.translate` 求值：`application.py` 顶层就 import 了 `MainWindow`，
等 `_init_i18n()` 装翻译器时所有界面模块早已导入完毕，模块级求值会永久冻结成英文。

用法：`uv run python -m ferret.utils.scripts`（或装好后的 `ferret-resources`）。
"""

import subprocess
import sys
from pathlib import Path

# BASE_DIR 指向包根目录 (src/ferret)
BASE_DIR = Path(__file__).resolve().parents[1]

RESOURCES_DIR = BASE_DIR / "resources"
CONFIG_DIR = RESOURCES_DIR / "code"

I18N_DIR = RESOURCES_DIR / "i18n"
TS_DIR = I18N_DIR / "zh_CN.ts"
QM_DIR = I18N_DIR / "zh_CN.qm"

QRC_DIR = RESOURCES_DIR / "resources.qrc"
# 必须是 core/ 下这一份：application.py 导入的就是 ferret.core.resources_rc。
PYQRC_DIR = BASE_DIR / "core" / "resources_rc.py"

#: 喂给 lupdate 会让它崩的生成文件，按文件名排除。
LUPDATE_EXCLUDE = {"resources_rc.py"}

SOURCE_LANGUAGE = "en_GB"
TARGET_LANGUAGE = "zh_CN"


def run_command(cmd, name) -> bool:
    print(f"正在执行 {name}...")
    try:
        result = subprocess.run(
            [str(part) for part in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if result.returncode == 0:
            print(f"{name} 成功！")
            print(result.stdout)
            return True
        print(f"{name} 出错啦（退出码 {result.returncode}）：\n{result.stderr}")
    except (OSError, UnicodeDecodeError) as e:
        print(f"执行异常: {e}")
    return False


def translatable_sources() -> list[Path]:
    """交给 lupdate 的源文件清单（已排除会让它崩的生成文件）。"""
    return sorted(p for p in BASE_DIR.rglob("*.py") if p.name not in LUPDATE_EXCLUDE)


def pyside6_lupdate() -> bool:
    TS_DIR.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pyside6-lupdate",
        # 不用 -recursive：那会把 resources_rc.py 一起吃进去，见模块 docstring。
        *translatable_sources(),
        "-no-obsolete",  # 清掉改名/删掉的旧词条，别让化石堆积
        "-source-language",
        SOURCE_LANGUAGE,
        "-target-language",
        TARGET_LANGUAGE,
        "-ts",
        TS_DIR,
    ]
    return run_command(cmd, "lupdate (提取翻译)")


def pyside6_lrelease() -> bool:
    cmd = ["pyside6-lrelease", TS_DIR, "-qm", QM_DIR]
    return run_command(cmd, "lrelease(生成qm)")


def pyside6_rcc() -> bool:
    cmd = ["pyside6-rcc", QRC_DIR, "-o", PYQRC_DIR]
    return run_command(cmd, "rcc(qrc转译pyqrc)")


def main() -> int:
    """按顺序跑完三步；任一步失败就停下，别让下游拿着旧产物继续。"""
    for step in (pyside6_lupdate, pyside6_lrelease, pyside6_rcc):
        if not step():
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
