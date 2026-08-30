"""sysproxy 包接线冒烟：ferret 作为宿主的三条接缝没断。

服务自身的单元测试随包迁到了 `packages/sysproxy/tests/`；这里只验证宿主侧
—— journal 路径注入、包异常常量 → 界面文案的翻译映射是否对得上账。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sysproxy
from PySide6.QtWidgets import QApplication
from sysproxy import ERR_SET_FAILED

from ferret.apps.capture.controllers import _SYSTEM_PROXY_ERRORS, CaptureController
from ferret.core.settings import get_config_dir


class WiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_the_fallback_service_journals_into_the_app_config_dir(self) -> None:
        """包刻意不带默认目录；宿主没注入等于 journal 落错地方。"""
        controller = CaptureController()
        self.assertEqual(
            controller._system_proxy._journal_path,
            get_config_dir() / "system-proxy-state.json",
        )
        controller.deleteLater()

    def test_the_error_mapping_covers_every_package_constant(self) -> None:
        """包的对账常量缺一条，界面上那条错误就会原样冒英文。"""
        constants = {
            sysproxy.ERR_INVALID_ADDRESS,
            sysproxy.ERR_RESTORE_FAILED,
            sysproxy.ERR_SET_FAILED,
        }
        self.assertEqual(constants, set(_SYSTEM_PROXY_ERRORS))

    def test_a_package_error_is_lookable_up_in_the_mapping(self) -> None:
        """未装翻译器时 `resolve_marker` 原样回英文源文本 —— 映射表键值同文。"""
        from ferret.utils.i18n import resolve_marker

        resolved = resolve_marker(
            _SYSTEM_PROXY_ERRORS, ERR_SET_FAILED, "CaptureController", fallback=""
        )
        self.assertEqual(resolved, ERR_SET_FAILED)


if __name__ == "__main__":
    unittest.main()
