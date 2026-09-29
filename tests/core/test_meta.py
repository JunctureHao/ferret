"""core/meta.REPO_URL 的守卫：更新源地址必须保持「可发布」的形态。

vpk 上传侧与应用内更新源都认这一个常量；被改成空串、占位符或带 .git
后缀会让 release 发错地方 / 客户端更新静默失效，这里钉住基本形态。
"""

import unittest
from urllib.parse import urlparse

from ferret.core.meta import REPO_URL


class RepoUrlTests(unittest.TestCase):
    def test_repo_url_is_a_publishable_github_repo_url(self) -> None:
        """https://github.com/<owner>/<repo>，不带 .git 后缀与多余路径。"""
        parsed = urlparse(REPO_URL)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "github.com")
        parts = parsed.path.strip("/").split("/")
        self.assertEqual(len(parts), 2, "应为 owner/repo 两段，不带子路径")
        self.assertFalse(parts[1].endswith(".git"), "统一不带 .git 后缀")


if __name__ == "__main__":
    unittest.main()
