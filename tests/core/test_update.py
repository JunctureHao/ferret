"""core/update.py 的单元测试：velopack 是原生 pyd 且依赖安装目录，一律用
``sys.modules`` 注入的替身（docs/design.md#update）。不碰真实网络。

替身注入后必须 ``importlib.reload(update_core)`` —— 模块顶层 ``import velopack``
在首次导入时绑定，reload 才会拾取替身；tearDown 恢复真身同样靠 reload。
"""

from __future__ import annotations

import importlib
import os
import sys
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make_fake_velopack(
    *, info=None, raises: Exception | None = None, portable=False, delta_sizes=()
):
    """造一个 velopack 替身模块：check 返回 info、抛 raises、或报便携版。

    ``delta_sizes`` 非空时填充 ``DeltasToTarget``（镜像真实 velopack 的增量列表），
    用来覆盖 check() 里「按增量总和预估下载大小」的分支。
    """
    fake = types.ModuleType("velopack")

    class FakeAsset:
        Version = "1.2.3"
        Size = 1024
        NotesMarkdown = ""

    class FakeDelta:
        def __init__(self, size):
            self.Size = size

    deltas = [FakeDelta(size) for size in delta_sizes]

    class FakeInfo:
        TargetFullRelease = FakeAsset()
        DeltasToTarget = deltas

    class FakeManager:
        def __init__(self, source):
            if isinstance(raises, Exception) and str(raises) == "not installed":
                raise raises

        def get_is_portable(self):
            return portable

        def get_current_version(self):
            return "1.0.0"

        def check_for_updates(self):
            if isinstance(raises, Exception) and str(raises) != "not installed":
                raise raises
            if info == "none":
                return None
            return info if info is not None else FakeInfo()

        def download_updates(self, update_info, progress_callback=None):
            if progress_callback:
                for pct in (0, 50, 100):
                    progress_callback(pct)

        def apply_updates_and_restart(self, update):
            pass

    fake.UpdateManager = FakeManager  # ty: ignore[unresolved-attribute]
    fake.GithubSource = lambda repo_url: ("github", repo_url)  # ty: ignore[unresolved-attribute]
    return fake


class _FakeModuleTestBase(unittest.TestCase):
    """注入/恢复 velopack 替身 + reload update_core 的脚手架。"""

    def setUp(self) -> None:
        self._real_velopack = sys.modules.get("velopack")
        import ferret.core.update as update_core

        self.update_core = update_core

    def inject(self, fake) -> None:
        sys.modules["velopack"] = fake
        importlib.reload(self.update_core)

    def tearDown(self) -> None:
        if self._real_velopack is not None:
            sys.modules["velopack"] = self._real_velopack
        else:
            sys.modules.pop("velopack", None)
        importlib.reload(self.update_core)


class UpdateSupportedTests(_FakeModuleTestBase):
    def test_dev_mode_is_not_supported(self) -> None:
        """测试进程未编译，__compiled__ 不在模块全局里 → 恒 False。"""
        self.inject(_make_fake_velopack())
        self.assertFalse(self.update_core.update_supported())

    def test_installed_build_is_supported(self) -> None:
        self.inject(_make_fake_velopack())
        self.update_core.__dict__["__compiled__"] = True
        try:
            self.assertTrue(self.update_core.update_supported())
        finally:
            del self.update_core.__dict__["__compiled__"]

    def test_portable_build_is_not_supported(self) -> None:
        self.inject(_make_fake_velopack(portable=True))
        self.update_core.__dict__["__compiled__"] = True
        try:
            self.assertFalse(self.update_core.update_supported())
        finally:
            del self.update_core.__dict__["__compiled__"]

    def test_uninstalled_build_is_not_supported(self) -> None:
        """编译产物但不在安装目录里（UpdateManager 构造即抛）→ False。"""
        self.inject(_make_fake_velopack(raises=RuntimeError("not installed")))
        self.update_core.__dict__["__compiled__"] = True
        try:
            self.assertFalse(self.update_core.update_supported())
        finally:
            del self.update_core.__dict__["__compiled__"]


class CheckTests(_FakeModuleTestBase):
    def test_no_update_returns_none(self) -> None:
        self.inject(_make_fake_velopack(info="none"))
        self.assertIsNone(self.update_core.check())

    def test_update_returns_handle_and_brief(self) -> None:
        self.inject(_make_fake_velopack())
        result = self.update_core.check()
        assert result is not None
        info, brief = result
        self.assertEqual(brief.current, "1.0.0")
        self.assertEqual(brief.target, "1.2.3")
        self.assertEqual(brief.size, 1024)
        self.assertTrue(brief.release_page.endswith("/releases/tag/v1.2.3"))
        # 句柄不透明但能喂回 download / apply（同一次检查的产物）。
        self.update_core.download(info)
        self.update_core.apply_and_restart(info)

    def test_delta_download_size_prefers_delta_total(self) -> None:
        """有可用增量包时，size 取增量总和而非全量包大小。"""
        self.inject(_make_fake_velopack(delta_sizes=(100, 150)))
        result = self.update_core.check()
        assert result is not None
        _info, brief = result
        self.assertEqual(brief.size, 250)

    def test_check_failure_is_wrapped_as_update_error(self) -> None:
        self.inject(_make_fake_velopack(raises=RuntimeError("network down")))
        with self.assertRaises(self.update_core.UpdateError) as ctx:
            self.update_core.check()
        self.assertIn("network down", str(ctx.exception))

    def test_download_progress_callback_receives_percentages(self) -> None:
        self.inject(_make_fake_velopack())
        result = self.update_core.check()
        assert result is not None
        info, _brief = result
        seen: list[int] = []
        self.update_core.download(info, seen.append)
        self.assertEqual(seen, [0, 50, 100])


if __name__ == "__main__":
    unittest.main()
