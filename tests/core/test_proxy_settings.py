"""Tests for seeding the kernel from persisted proxy settings.

config.json 是纯文本、用户随时可能手改。这里守两件事：**手改坏了不能让应用起不来**，
以及坏值必须收敛到更安全的那一边（环回 + 默认端口）。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from qfluentwidgets import qconfig

from ferret.core.network import DEFAULT_PORT, LOOPBACK_HOST
from ferret.core.runtime import ApplicationRuntime
from ferret.core.settings import CONFIG


class ProxySettingsSeedingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    ITEMS = ("listen_host", "listen_port", "block_global", "block_private")

    def setUp(self) -> None:
        # 绝不能碰用户真实的 config.json。qconfig.load 改的是全局 CONFIG，
        # 所以连值一起快照 —— 否则本文件的坏配置会漏给后面跑的用例。
        self._dir = tempfile.TemporaryDirectory()
        self._original_file = CONFIG.file
        self._original_values = {
            name: CONFIG.get(getattr(CONFIG, name)) for name in self.ITEMS
        }
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._original_values.items():
            CONFIG.set(getattr(CONFIG, name), value, save=False)
        CONFIG.file = self._original_file
        self._dir.cleanup()

    def load(self, proxy: dict) -> None:
        path = Path(self._dir.name) / "config.json"
        path.write_text(json.dumps({"Proxy": proxy}), encoding="utf-8")
        # 这一步就是 Application._init_config 干的事。
        qconfig.load(str(path), CONFIG)

    def build(self) -> ApplicationRuntime:
        runtime = ApplicationRuntime()
        self.addCleanup(runtime.mitm_runtime.stop)
        return runtime

    def test_a_hand_broken_config_still_loads(self) -> None:
        """端口故意不挂 RangeValidator：它的 correct 是 min(max(lo, v), hi)，
        字符串会抛 TypeError，而 QConfig.load 不 catch —— 启动就崩。
        """
        self.load(
            {
                "ListenHost": "10.1.2.3",
                "ListenPort": "abc",
                "BlockGlobal": "nope",
                "BlockPrivate": 7,
            }
        )
        runtime = self.build().mitm_runtime
        self.assertEqual(runtime.listen_host, LOOPBACK_HOST)
        self.assertEqual(runtime.listen_port, DEFAULT_PORT)

    def test_out_of_range_port_falls_back_to_the_default(self) -> None:
        self.load({"ListenPort": 80})
        self.assertEqual(self.build().mitm_runtime.listen_port, DEFAULT_PORT)

    def test_valid_settings_reach_the_kernel_untouched(self) -> None:
        self.load(
            {
                "ListenHost": "0.0.0.0",
                "ListenPort": 9123,
                "BlockGlobal": False,
                "BlockPrivate": True,
            }
        )
        runtime = self.build().mitm_runtime
        self.assertEqual(runtime.listen_host, "0.0.0.0")
        self.assertEqual(runtime.listen_port, 9123)
        self.assertFalse(runtime.block_global)
        self.assertTrue(runtime.block_private)

    def test_local_client_host_ignores_the_bound_address(self) -> None:
        """整个改动的核心不变量：本机接入路径与绑定地址无关。"""
        self.load({"ListenHost": "0.0.0.0", "ListenPort": 9123})
        app_runtime = self.build()
        self.assertTrue(app_runtime.mitm.is_lan_exposed)
        self.assertEqual(app_runtime.mitm.local_client_host, LOOPBACK_HOST)
        self.assertEqual(app_runtime.mitm.listen_host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
