import subprocess
from pathlib import Path

# BASE_DIR 指向项目根目录 (Ferret)
BASE_DIR = Path(__file__).resolve().parents[1]


RESOURCES_DIR = BASE_DIR / "resources"
CONFIG_DIR = RESOURCES_DIR / "code"

I18N_DIR = RESOURCES_DIR / "i18n"
TS_DIR = I18N_DIR / "zh_CN.ts"
QM_DIR = I18N_DIR / "zh_CN.qm"


QRC_DIR = RESOURCES_DIR / "resources.qrc"
PYQRC_DIR = RESOURCES_DIR / "resources_rc.py"


def run_command(cmd, name):
    print(f"正在执行 {name}...")
    try:
        # 使用 shell=True 或者确保路径是字符串
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0:
            print(f"{name} 成功！")
            print(result.stdout)
        else:
            print(f"{name} 出错啦：\n{result.stderr}")
    except Exception as e:
        print(f"执行异常: {e}")


def pyside6_lupdate():
    # 确保保存翻译的目录存在
    TS_DIR.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pyside6-lupdate",
        "-verbose",
        "-recursive",  # 关键：递归扫描子目录（如 interface/）
        "-extensions",
        "py,ui",  # 显式指定扫描 .py 和 .ui 文件 (关键！)
        str(BASE_DIR),  # 源代码根目录
        "-ts",
        str(TS_DIR),  # 输出文件
    ]
    run_command(cmd, "lupdate (提取翻译)")


def pyside6_lrelease():
    cmd = ["pyside6-lrelease", TS_DIR, "-qm", QM_DIR]
    run_command(cmd, "lrelease(生成qm)")


def pyside6_rcc():
    cmd = [
        "pyside6-rcc",
        QRC_DIR,
        "-o",
        PYQRC_DIR,
    ]
    run_command(cmd, "rcc(qrc转译pyqrc)")
