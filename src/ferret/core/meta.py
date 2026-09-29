"""应用元信息常量。

REPO_URL 是 velopack 更新源的唯一事实源：上传侧（scripts/package.py 把
release 发到哪）与应用内更新客户端（GithubSource 去哪检查更新）都认它，
必须同源。收在代码常量里而不是 pyproject.toml —— 运行时 exe 读不到后者，
要读就得随包分发整个文件，不值当。

仓库改名 / 迁移时改这里；已发布 exe 里烤着的永远是旧值，GitHub 的 301
重定向能兜一阵，正式修复是发一个新版本。
"""

from __future__ import annotations

REPO_URL = "https://github.com/JunctureHao/ferret"
