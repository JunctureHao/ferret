"""流列表列布局纯逻辑（.plans/0-flow-list-columns.md §6.1）。

归一化与布局对象不碰 Qt / mitmproxy（`columns.py` 只 import QtCore 级翻译标记），
所以这些用例**不起 QApplication**，直接钉纯逻辑：默认布局、必需列强制、未知/缺失
key 迁移、非法宽度回落、错误版本整体回落、翻译不影响 key。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.apps.common.flow.columns import (
    DEFAULT_ORDER,
    REQUIRED_KEYS,
    SCHEMA_VERSION,
    default_layout,
    normalize,
)


class DefaultLayoutTests(unittest.TestCase):
    def test_default_order_is_canonical(self) -> None:
        layout = default_layout()
        self.assertEqual(
            layout.order,
            ("index", "mark", "method", "url", "status", "type", "size", "time"),
        )
        self.assertEqual(layout.order, DEFAULT_ORDER)

    def test_default_all_visible(self) -> None:
        layout = default_layout()
        self.assertEqual(set(layout.order), layout.visible)

    def test_default_widths(self) -> None:
        layout = default_layout()
        self.assertEqual(layout.width("index"), 80)
        self.assertEqual(layout.width("url"), 420)
        self.assertEqual(layout.width("status"), 65)

    def test_mark_width_not_stored(self) -> None:
        # mark 固定宽：不入 widths 表，取值回落默认。
        layout = default_layout()
        self.assertNotIn("mark", dict(layout.widths))
        self.assertEqual(layout.width("mark"), 64)

    def test_roundtrip_through_dict(self) -> None:
        layout = default_layout()
        self.assertEqual(normalize(layout.to_dict()), layout)


class FallbackTests(unittest.TestCase):
    def test_none_falls_back_to_default(self) -> None:
        self.assertEqual(normalize(None), default_layout())

    def test_empty_dict_falls_back(self) -> None:
        self.assertEqual(normalize({}), default_layout())

    def test_wrong_version_falls_back_whole(self) -> None:
        raw = default_layout().to_dict()
        raw["version"] = 999
        raw["order"] = ["url", "index"]
        self.assertEqual(normalize(raw), default_layout())

    def test_non_dict_falls_back(self) -> None:
        self.assertEqual(normalize(["nonsense"]), default_layout())


class RequiredColumnTests(unittest.TestCase):
    def test_hiding_optional_keeps_required(self) -> None:
        raw = default_layout().to_dict()
        raw["visible"] = ["index", "method", "url"]  # 隐藏所有可选列
        layout = normalize(raw)
        for key in REQUIRED_KEYS:
            self.assertTrue(layout.is_visible(key))

    def test_required_forced_visible_even_if_omitted(self) -> None:
        # 用户配置里把必需列从 visible 拿掉（脏配置）——归一化强制加回。
        raw = default_layout().to_dict()
        raw["visible"] = ["mark", "status"]
        layout = normalize(raw)
        self.assertTrue(layout.is_visible("index"))
        self.assertTrue(layout.is_visible("method"))
        self.assertTrue(layout.is_visible("url"))

    def test_never_empty_layout(self) -> None:
        raw = default_layout().to_dict()
        raw["visible"] = []
        layout = normalize(raw)
        self.assertTrue(layout.visible)


class OrderTests(unittest.TestCase):
    def test_index_forced_first(self) -> None:
        raw = default_layout().to_dict()
        raw["order"] = ["url", "method", "index", "mark", "status", "type", "size", "time"]
        layout = normalize(raw)
        self.assertEqual(layout.order[0], "index")

    def test_dedupe_order(self) -> None:
        raw = default_layout().to_dict()
        raw["order"] = ["index", "url", "url", "method"]
        layout = normalize(raw)
        self.assertEqual(len(layout.order), len(set(layout.order)))
        self.assertEqual(set(layout.order), set(DEFAULT_ORDER))

    def test_unknown_keys_ignored(self) -> None:
        raw = default_layout().to_dict()
        raw["order"] = ["index", "bogus", "url", "method"]
        layout = normalize(raw)
        self.assertNotIn("bogus", layout.order)
        self.assertEqual(set(layout.order), set(DEFAULT_ORDER))

    def test_missing_columns_appended_not_reordered(self) -> None:
        # 用户旧配置只排了前四列；新/缺列按默认顺序追加到末尾，不打乱已排部分。
        raw = default_layout().to_dict()
        raw["order"] = ["index", "url", "method", "mark"]
        raw["visible"] = ["index", "url", "method", "mark"]
        layout = normalize(raw)
        self.assertEqual(layout.order[:4], ("index", "url", "method", "mark"))
        self.assertEqual(set(layout.order), set(DEFAULT_ORDER))

    def test_new_column_uses_default_visibility(self) -> None:
        # 旧配置不认识 'time'（缺列），迁移时按默认可见性追加，不被旧 visible 误判隐藏。
        raw = {
            "version": SCHEMA_VERSION,
            "order": ["index", "mark", "method", "url", "status", "type", "size"],
            "visible": ["index", "mark", "method", "url", "status", "type", "size"],
            "widths": {},
        }
        layout = normalize(raw)
        self.assertIn("time", layout.order)
        self.assertTrue(layout.is_visible("time"))


class WidthTests(unittest.TestCase):
    def test_bad_width_falls_back_to_default(self) -> None:
        raw = default_layout().to_dict()
        raw["widths"] = {"url": "wide", "status": None}
        layout = normalize(raw)
        self.assertEqual(layout.width("url"), 420)
        self.assertEqual(layout.width("status"), 65)

    def test_too_small_width_falls_back(self) -> None:
        raw = default_layout().to_dict()
        raw["widths"] = {"url": 5}  # < _MIN_WIDTH
        layout = normalize(raw)
        self.assertEqual(layout.width("url"), 420)

    def test_valid_width_preserved(self) -> None:
        raw = default_layout().to_dict()
        raw["widths"] = {"url": 300}
        layout = normalize(raw)
        self.assertEqual(layout.width("url"), 300)

    def test_mark_width_ignored(self) -> None:
        raw = default_layout().to_dict()
        raw["widths"] = {"mark": 999}
        layout = normalize(raw)
        self.assertNotIn("mark", dict(layout.widths))
        self.assertEqual(layout.width("mark"), 64)


class MutatorTests(unittest.TestCase):
    def test_with_visible_hides_optional(self) -> None:
        layout = default_layout().with_visible("status", False)
        self.assertFalse(layout.is_visible("status"))

    def test_with_visible_cannot_hide_required(self) -> None:
        layout = default_layout().with_visible("url", False)
        self.assertTrue(layout.is_visible("url"))  # 必需列不可隐藏

    def test_with_width_updates_one_column(self) -> None:
        layout = default_layout().with_width("url", 250)
        self.assertEqual(layout.width("url"), 250)
        self.assertEqual(layout.width("status"), 65)  # 其余不动

    def test_with_order_moves_column(self) -> None:
        order = ["index", "url", "method", "mark", "status", "type", "size", "time"]
        layout = default_layout().with_order(order)
        self.assertEqual(layout.order, tuple(order))

    def test_frozen_returns_new_object(self) -> None:
        base = default_layout()
        changed = base.with_width("url", 250)
        self.assertIsNot(base, changed)
        self.assertEqual(base.width("url"), 420)  # 原对象不被 mutate


class TitleIndependenceTests(unittest.TestCase):
    def test_stable_keys_not_titles(self) -> None:
        # 配置只存稳定 key，标题翻译变化不影响归一化（§3.2 迁移不依赖中文标题）。
        layout = default_layout()
        for key in layout.order:
            self.assertIsInstance(key, str)
        # order/visible 里全是 key，绝不含显示标题（如 "标记" / "Mark"）。
        self.assertNotIn("标记", layout.order)
        self.assertNotIn("Mark", layout.order)


if __name__ == "__main__":
    unittest.main()
