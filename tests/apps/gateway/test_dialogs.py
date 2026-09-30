"""网关规则对话框的表单往返测试（issues #85 / #65）。

两个都是「打开不动就变值」的形状：

- #85：首次填充时下拉还是空的（currentIndex=-1），`_current_field()` 会把它折成
  本层第 0 项 —— METHOD=POST 的规则打开不动就变 HOST=POST；
- #65：下拉只有五个预设状态码，模型允许的 429 打开不动、get_rule 会折成首个
  预设 403。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.gateway.dialogs import GatewayRuleDialog
from ferret.core.mitm import (
    GATEWAY_STATUS_CLOSE,
    GatewayField,
    GatewayLayer,
    GatewayLogic,
    GatewayPolicy,
    GatewayRule,
)

app = QApplication.instance() or QApplication([])


def rule(**kwargs) -> GatewayRule:
    fields: dict = {
        "layer": GatewayLayer.L7,
        "policy": GatewayPolicy.BLOCK_OUT,
        "field": GatewayField.HOST,
        "logic": GatewayLogic.CONTAINS,
        "value": "example.com",
        "status_code": 403,
    }
    fields.update(kwargs)
    return GatewayRule(**fields)


class GatewayRuleDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        # 注册得最早 → LIFO 里跑得最晚：等挂起事件先落地再退出用例
        # （满载下 processEvents 摸到已销毁控件会炸出 RuntimeError）。
        self.addCleanup(app.processEvents)
        # MessageBoxBase 把自己铺满 parent、parent=None 直接炸，给个壳。
        self.host = QWidget()

    def _dialog(self, *args, **kwargs) -> GatewayRuleDialog:
        dialog = GatewayRuleDialog(*args, parent=self.host, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_method_rule_keeps_its_field_on_first_fill(self) -> None:
        """首次填充以规则自带的 field 为准，不把空下拉的 -1 折成 HOST（#85）。"""
        dialog = self._dialog("编辑", rule=rule(field=GatewayField.METHOD))
        self.assertEqual(dialog.get_rule().field, GatewayField.METHOD)
        self.assertEqual(dialog.get_rule().value, "example.com")

    def test_a_new_rule_still_defaults_to_host(self) -> None:
        dialog = self._dialog("新增", rule=rule())
        self.assertEqual(dialog.get_rule().field, GatewayField.HOST)

    def test_switching_layers_keeps_the_current_choice(self) -> None:
        """切层保留控件当前选择（既有行为）：L4 只有主机，切过去收成 HOST。"""
        dialog = self._dialog("编辑", rule=rule(field=GatewayField.METHOD))
        dialog.layer_pivot.setCurrentItem(str(GatewayLayer.L4))
        self.assertEqual(dialog.get_rule().field, GatewayField.HOST)

    def test_a_non_preset_status_code_round_trips(self) -> None:
        """模型允许、下拉没有的 429（多半来自手改配置）：打开不动原值往返，
        不许折成首个预设 403（#65）。"""
        dialog = self._dialog("编辑", rule=rule(status_code=429))
        self.assertEqual(dialog.get_rule().status_code, 429)

    def test_a_preset_status_code_still_round_trips(self) -> None:
        for status in (403, 451, GATEWAY_STATUS_CLOSE):
            with self.subTest(status=status):
                dialog = self._dialog("编辑", rule=rule(status_code=status))
                self.assertEqual(dialog.get_rule().status_code, status)
