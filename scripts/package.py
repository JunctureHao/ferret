"""Ferret 发布打包脚本：Nuitka 编译 → Velopack(vpk) 安装包，一条命令串起来。

对应 docs/packaging.md 记录的手工两步：
  1. Nuitka standalone 编译：瘦身项集中在 ``src/ferret/__main__.py`` 顶部的
     ``# nuitka-project:`` 注释，本脚本只负责发起编译，不重复维护配置。
  2. ``vpk pack``：把 ``Ferret.dist`` 打成 Setup.exe / Portable.zip /
     full.nupkg（有上一版时再出 delta），产物落在 ``build/releases/``。

用法（项目根目录）：
  uv run python scripts/package.py                  # 全量：编译 + 打包
  uv run python scripts/package.py --skip-build     # 复用 build/dist/ 最新产物，只打包
  uv run python scripts/package.py --version 1.2.3  # 临时覆盖版本号
  uv run python scripts/package.py --dry-run        # 只打印将执行的命令

注意：
  - ``velopack`` Python 包只含应用内运行时（``application.py`` 里的
    ``App().run()`` 那半）；打包 CLI 是独立工具，本机须已
    ``dotnet tool install -g vpk``。
  - vpk 靠 ``build/releases/`` 里的上一版 full.nupkg 生成增量，勿随手清空该目录。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 编译与发布产物统一收在 build/ 根下：dist/ 是 Nuitka 中间产物（可随手清），
# releases/ 是 vpk 更新历史（只增不删）；保留策略相反，只共根不分家。
DIST_DIR = ROOT / "build" / "dist"
DEFAULT_OUTPUT = ROOT / "build" / "releases"
ICON = ROOT / "src/ferret/resources/icon.ico"

PACK_ID = "Ferret"
MAIN_EXE = "Ferret.exe"
# Nuitka standalone 产物目录名（--output-folder-name=Ferret，新版 Nuitka 加 .dist 后缀）
STANDALONE_DIR = "Ferret.dist"


def find_standalone_dir() -> Path:
    """取 ``build/dist/`` 下最近一次 Nuitka 的 standalone 目录，并断言主程序存在。"""
    candidates = [p for p in DIST_DIR.glob(f"*/{STANDALONE_DIR}") if p.is_dir()]
    if not candidates:
        raise SystemExit(
            f"{DIST_DIR} 下没有 Nuitka 产物（*/{STANDALONE_DIR}），先跑一次全量构建"
        )
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    if not (latest / MAIN_EXE).is_file():
        raise SystemExit(f"{latest} 里没有 {MAIN_EXE}，产物不完整")
    print(f"使用 Nuitka 产物：{latest.relative_to(ROOT)}")
    return latest


def read_version() -> str:
    """版本号唯一事实源是 pyproject.toml（uv/pip 与安装包保持一致）。"""
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def display(path: Path) -> Path:
    """产物路径展示：项目内用相对路径，项目外退回绝对路径。"""
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def run(cmd: list[str], dry_run: bool) -> None:
    print("+", " ".join(cmd))
    if dry_run:
        return
    try:
        subprocess.run(cmd, check=True, cwd=ROOT)
    except FileNotFoundError as exc:
        raise SystemExit(
            f"找不到命令 {cmd[0]}；vpk 未安装时先 `dotnet tool install -g vpk`"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode) from None


def pack(
    standalone: Path, version: str, output_dir: Path, args: argparse.Namespace
) -> None:
    # --yes：把 vpk「同/更高版本已存在」询问的默认答案翻成 yes，真实终端里
    # 回车即可覆盖重打包；纯管道/CI 环境下 vpk 的确认框在非交互 stdio 上
    # 会直接报错，此时需换版本号或换输出目录。
    cmd = [
        "vpk",
        "--yes",
        "pack",
        "--packId",
        PACK_ID,
        "--packVersion",
        version,
        "--packDir",
        str(standalone),
        "--mainExe",
        MAIN_EXE,
        "--packTitle",
        PACK_ID,
        "--outputDir",
        str(output_dir),
    ]
    if ICON.is_file():
        cmd += ["--icon", str(ICON)]
    if args.pack_authors:
        cmd += ["--packAuthors", args.pack_authors]
    # --shortcuts 默认 Desktop,StartMenuRoot，够用；Python 应用无需 --framework。
    run(cmd, args.dry_run)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Nuitka 编译 + Velopack 打包一条龙",
    )
    parser.add_argument(
        "--version", help="覆盖版本号（默认读 pyproject.toml 的 project.version）"
    )
    parser.add_argument("--pack-authors", help="作者/公司名，写入安装包元数据")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="vpk 产物目录（默认 build/releases/）",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="跳过 Nuitka 编译，复用 build/dist/ 最新产物",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印将执行的命令，不真正执行"
    )
    args = parser.parse_args()
    # 相对输出目录统一锚到项目根（subprocess 的 cwd 也是 ROOT，两边保持一致）
    if not args.output_dir.is_absolute():
        args.output_dir = ROOT / args.output_dir

    version = args.version or read_version()
    print(f"packId={PACK_ID}  version={version}")

    if not args.skip_build:
        run([sys.executable, "-m", "nuitka", "src/ferret"], args.dry_run)
    standalone = find_standalone_dir()
    pack(standalone, version, args.output_dir, args)

    if not args.dry_run:
        print("产物清单：")
        for item in sorted(args.output_dir.glob(f"{PACK_ID}*")):
            print("  -", display(item))


if __name__ == "__main__":
    main()
