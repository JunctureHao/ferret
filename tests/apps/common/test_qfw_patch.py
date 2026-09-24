"""阶段 1 验收：主题切换棘轮止血（qfw_patch）。

护栏断言（源码态、offscreen）：装补丁后连续切主题 6 往返，每个已登记控件的 compose
**深度恒 1**（不自我嵌套），渲染文本长度不随往返增长。若上游 qfluentwidgets 修了棘轮
或本补丁失效，此测试会在深度 > 1 处失败。

对照见 `.plans/0-MEMORY_OPTIMIZATION_PLAN.executable.md` §3。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from qfluentwidgets import PushButton, Theme, setTheme
from qfluentwidgets.common import style_sheet as qfw_style
from qfluentwidgets.common.style_sheet import StyleSheetCompose, getStyleSheet

from ferret.apps.common import qfw_patch
from ferret.apps.common.qfw_patch import install_theme_ratchet_fix


def _compose_depth(source: object) -> int:
    """StyleSheetCompose 的嵌套深度：健康态 [FluentStyleSheet, CustomStyleSheet] 记为 1，
    每被套壳一层 +1。"""
    if isinstance(source, StyleSheetCompose):
        return 1 + max((_compose_depth(s) for s in source.sources), default=0)
    return 0


class ThemeRatchetFixTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        install_theme_ratchet_fix()

    def test_compose_depth_stays_one_across_roundtrips(self) -> None:
        # PushButton 在 __init__ 里经 FluentStyleSheet.BUTTON.apply 登记自己。
        button = PushButton("t")
        self.assertIn(button, qfw_style.styleSheetManager.widgets)

        baseline_len = len(getStyleSheet(qfw_style.styleSheetManager.source(button)))
        for _ in range(6):
            setTheme(Theme.DARK)
            setTheme(Theme.LIGHT)

        source = qfw_style.styleSheetManager.source(button)
        self.assertEqual(
            _compose_depth(source),
            1,
            "补丁失效：compose 被套壳，主题切换棘轮复发",
        )
        # 回到 LIGHT（与 baseline 同主题），文本不应因往返而增长。
        self.assertEqual(len(getStyleSheet(source)), baseline_len)

    def test_module_attribute_is_patched(self) -> None:
        install_theme_ratchet_fix()  # 幂等：二次调用不改变已覆盖的引用
        self.assertIs(qfw_style.updateStyleSheet, qfw_patch._patched_update_style_sheet)


if __name__ == "__main__":
    unittest.main()
