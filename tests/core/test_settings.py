"""bool 配置项的坏值回落测试（issues #88）。

qfw 的 ``BoolValidator`` 是 ``OptionsValidator([True, False])``，`correct` 把不在
options 里的值修成 ``options[0]`` —— 恰好是 True：JSON 里的 null、``"false"``、0
加载时会把默认关闭的开关顶开（ssl_insecure 关掉上游 TLS 校验、scripts_enabled
直接装载脚本）。`BoolConfigItem` 收紧为「坏值回各项默认」，这里用真实
`QConfig.load` 链路钉住（``deserializeFrom`` → value setter → validator.correct）。
"""

import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.core.settings import BoolConfigItem, Config

app = QApplication.instance() or QApplication([])


class BoolConfigItemLoadTests(unittest.TestCase):
    def _load(self, overrides: dict) -> Config:
        config = Config()
        handle, path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        self.addCleanup(os.unlink, path)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(overrides, stream)
        config.load(path)
        return config

    def test_null_falls_back_to_each_items_default(self) -> None:
        config = self._load(
            {"Proxy": {"SslInsecure": None, "SystemProxyEnabled": None}}
        )
        self.assertIs(config.ssl_insecure.value, False)
        # 默认开的项坏值回 True（各项默认），不是一刀切 False。
        self.assertIs(config.system_proxy_enabled.value, True)

    def test_wrong_typed_values_fall_back_to_defaults(self) -> None:
        config = self._load(
            {
                "Proxy": {"LocalEnabled": "false", "BlockPrivate": 0},
                "Scripts": {"Enabled": []},
            }
        )
        self.assertIs(config.local_enabled.value, False)
        self.assertIs(config.block_private.value, False)
        self.assertIs(config.scripts_enabled.value, False)

    def test_legal_bool_values_pass_through(self) -> None:
        config = self._load({"Proxy": {"SslInsecure": True}, "Mock": {"Reuse": False}})
        self.assertIs(config.ssl_insecure.value, True)
        self.assertIs(config.mock_reuse.value, False)

    def test_every_bool_item_declares_a_bool_default(self) -> None:
        """防止再有人用 `ConfigItem` + 裸 validator 定义 bool 项绕过收紧。"""
        for name in dir(Config):
            item = getattr(Config, name)
            if not isinstance(item, BoolConfigItem):
                continue
            with self.subTest(item=name):
                self.assertIsInstance(item.defaultValue, bool)
