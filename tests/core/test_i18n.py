"""翻译目录的守卫：漏译、化石条目、忘跑 rcc，这三件事都不报错，只会静默变中文。

源语言是**简体中文** —— 代码里每个 `tr()` 字面量本身就是中文，查不到译文时 Qt
原样返回源文本。所以漏一条译文的表现是「英文界面突然冒出一个中文词」，而测试、
ruff、ty 全绿。这个文件是为了让下一次同类事故变成一条红色断言。

三层各挡一件事：

* `CatalogTests` —— 代码里的字面量与 `en_GB.ts` **双向**对齐（漏译 + 化石），外加
  `<location>` 必须指向真实文件（重构完忘跑 lupdate 的现场）、`#:` 注释不许漏成
  译者说明；
* `CompiledCatalogTests` —— `.ts` 里每一条都能从 `:/i18n/en_GB.qm` 原样读回来，专抓
  「改了 ts 但没跑 lrelease / rcc」；
* `ExtractableTests` —— 写法本身得是 lupdate 认的形式（f-string、`tr(变量)` 提取不到）。

改完文案的标准动作是 `uv run python -m ferret.utils.scripts`（lupdate → lrelease → rcc）。
"""

import ast
import os
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTranslator
from PySide6.QtWidgets import QApplication

from ferret.core import resources_rc  # noqa: F401  注册 :/i18n/*.qm

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src" / "ferret"
TS_PATH = SRC_DIR / "resources" / "i18n" / "en_GB.ts"

#: 这个生成物喂给 lupdate 会让它以 0xC0000409 崩掉（3.6 MB），流水线里也是排除它的。
GENERATED = {"resources_rc.py"}

#: `resolve_marker` 是所有标记表共用的查表器，context 只能由调用方传进来 —— 它是
#: 「context 必须是字面量」这条规则唯一的例外。
MARKER_HELPER = "src/ferret/utils/i18n.py"


def _string(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _calls():
    """遍历 `src/ferret` 下所有 `tr()` / `translate()` / `QT_TRANSLATE_NOOP()` 调用。

    产出 ``(出处, 函数名, 源文本节点, context 节点)``。三种写法的参数位置不同：`tr()`
    只有源文本，另两个第 1 个是 context、源文本排第 2（`tr()` 的 context 由 lupdate 按
    所在类推断，拿不到节点，给 ``None``）。
    """
    for py in sorted(SRC_DIR.rglob("*.py")):
        if py.name in GENERATED:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), str(py))
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name == "tr":
                index = 0
            elif name in ("translate", "QT_TRANSLATE_NOOP"):
                index = 1
            else:
                continue
            if len(node.args) > index:
                context = node.args[0] if index else None
                yield f"{rel}:{node.lineno}", name, node.args[index], context


def source_literals() -> list[tuple[str, str]]:
    """所有能被提取的源文本，附带出处 —— 断言失败时能直接跳到那一行。"""
    return sorted(
        (text, where)
        for where, _name, node, _ctx in _calls()
        if (text := _string(node)) is not None
    )


class CatalogTests(unittest.TestCase):
    """`en_GB.ts` 与代码双向对齐。

    单向不够：只查「代码里的都有译文」挡得住漏译，但改过文案的旧条目会一直躺在目录里，
    下次谁想搬运译文就会照着一条早已不存在的中文抄。所以两个方向都断言。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.entries = [
            (ctx.findtext("name") or "", msg)
            for ctx in ET.parse(TS_PATH).getroot()
            for msg in ctx.findall("message")
        ]

    def test_the_catalog_is_not_empty(self) -> None:
        """整个文件的前提：空目录会让下面每条断言都空转通过。"""
        self.assertGreater(len(self.entries), 400)

    def test_every_literal_has_a_non_empty_translation(self) -> None:
        """漏译在运行时静默退回中文源文本，这条断言是唯一的警报。"""
        translated = {
            msg.findtext("source")
            for _ctx, msg in self.entries
            if (msg.findtext("translation") or "").strip()
        }
        for text, where in source_literals():
            with self.subTest(source=text, at=where):
                self.assertIn(text, translated, f"{where} 缺英文译文")

    def test_no_catalog_entry_outlived_its_source(self) -> None:
        """反向：目录里不该留下代码中已经没有的 source。

        文案改一个词，旧条目就成了化石。lupdate 带 `-no-obsolete`，重跑一次就会清掉，
        所以这条失败的意思基本都是「改完文案忘了跑流水线」。
        """
        literals = {text for text, _where in source_literals()}
        stale = sorted(
            {msg.findtext("source") or "" for _ctx, msg in self.entries} - literals
        )
        self.assertEqual(stale, [], "目录里有代码中已不存在的 source，请重跑流水线")

    def test_no_entry_is_marked_unfinished_or_obsolete(self) -> None:
        """`unfinished` / `vanished` / `obsolete` 都表示这条没在生效。

        带文本的 unfinished 其实照样能用，但它是「填了一半」的信号，而且 lrelease 的
        计数会把它算成漏译，留着必然误判。
        """
        for ctx, msg in self.entries:
            node = msg.find("translation")
            assert node is not None
            with self.subTest(context=ctx, source=msg.findtext("source")):
                self.assertIsNone(node.get("type"), f"{ctx} 有未完成/废弃条目")

    def test_every_location_points_at_a_real_file(self) -> None:
        """`<location>` 是相对 `.ts` 所在目录算的，重构完不跑 lupdate 就会指向空气。

        化石目录当初就是这么露馅的：`<location>` 还指着早已改名成 `apps/` 的 `view/`。
        """
        for _ctx, msg in self.entries:
            for loc in msg.findall("location"):
                filename = loc.get("filename") or ""
                with self.subTest(location=filename):
                    self.assertTrue(
                        (TS_PATH.parent / filename).resolve().is_file(),
                        f"{filename} 不存在，请重跑 lupdate",
                    )

    def test_no_entry_carries_an_implementation_note(self) -> None:
        """`#:` 开头的注释会被 lupdate 当成下一个 `tr()` 的 `<extracomment>`。

        Sphinx 用 `#:` 给模块级常量写文档，而 lupdate 把它读成「给译者的说明」，于是
        讲状态码语义色、键前缀、大小口径的整段实现说明，会贴到源码里紧随其后的那条
        UI 文案上（`Copy cookies`、`Nothing to show`、`#` 都中过）。译者看到的是一段
        与那个词毫无关系的中文，而代码、ruff、ty、其余 i18n 断言全绿。

        这一条已经修了三次（`filter.py`、`detail.py`、`fields.py` 各一次），所以钉成
        断言：**目录里不该有任何 `<extracomment>`** —— 本项目从不刻意给译者留说明，
        出现一条就说明某个 `#:` 又漏进来了。改成普通 `#` 即可（没有 `tr()` 的文件里
        `#:` 是安全的）。
        """
        leaked = [
            (ctx, msg.findtext("source"), (msg.findtext("extracomment") or "")[:40])
            for ctx, msg in self.entries
            if msg.find("extracomment") is not None
        ]
        self.assertEqual(leaked, [], "有 `#:` 注释漏成了译者说明，请改成普通 `#`")


class CompiledCatalogTests(unittest.TestCase):
    """`.ts` 是源、`.qm` 是运行时真正读的东西，中间隔着 lrelease + rcc 两步。

    漏掉任一步都不会报错：程序继续用资源里那份旧 qm，界面上只是某几句还是老文案（或者
    干脆退回英文）。所以这里把整本目录逐条比一遍，而不是抽样。

    翻译器**故意不装进 QApplication**：unittest 一个进程跑完所有用例，装上去会污染其他
    模块里那些断言英文文案的用例。`QTranslator.translate()` 直接查表，够用。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.translator = QTranslator()
        cls.loaded = cls.translator.load(":/i18n/en_GB.qm")

    def test_the_compiled_catalog_ships_in_the_resources(self) -> None:
        """没跑 rcc、或者 rcc 又写去了没人 import 的路径，都会挂在这。"""
        self.assertTrue(self.loaded, ":/i18n/en_GB.qm 加载失败，请重跑流水线")

    def test_the_compiled_catalog_matches_the_source_catalog(self) -> None:
        for ctx in ET.parse(TS_PATH).getroot():
            name = ctx.findtext("name") or ""
            for msg in ctx.findall("message"):
                source = msg.findtext("source") or ""
                with self.subTest(context=name, source=source):
                    self.assertEqual(
                        self.translator.translate(name, source),
                        msg.findtext("translation"),
                        f"{name} / {source!r} 与编译产物不一致，请重跑流水线",
                    )

    def test_the_navigation_labels_translate_to_english(self) -> None:
        """导航七条逐字钉住（中文源 → 英文译文，源语言翻转后的方向）。"""
        expected = {
            "捕获": "Captures",
            "会话": "Sessions",
            "网关": "Gateway",
            "重写": "Rewrite",
            "断点": "Intercept",
            "证书": "Certificate",
            "设置": "Settings",
        }
        for source, english in expected.items():
            with self.subTest(source=source):
                self.assertEqual(
                    self.translator.translate("MainWindow", source), english
                )

    def test_a_marker_table_entry_survives_the_whole_pipeline(self) -> None:
        """模块级的表只存 `QT_TRANSLATE_NOOP` 标记，到使用点才求值。

        「标记式能不能被 lupdate 提取」当初是实测出来的，这里留一条端到端断言看着它：
        这句文案只写在 `core/mitm/certificate.py` 的模块级常量 `EXPORT_FORMATS` 里。
        """
        self.assertEqual(
            self.translator.translate("CertExportFormat", "PEM 证书 (.pem)"),
            "PEM certificate (.pem)",
        )


class ExtractableTests(unittest.TestCase):
    """写法得是 lupdate 认的形式，否则文案压根进不了目录。

    lupdate 只做静态扫描：**不看 f-string 内部**，也认不出 `tr(变量)` —— context 与源
    文本都必须是字面量。绕法是把文案存成 `QT_TRANSLATE_NOOP` 标记，用的时候再
    `QCoreApplication.translate` 求值（见 `ferret.utils.i18n.resolve_marker`）。
    """

    def test_no_call_hides_its_source_text_in_an_f_string(self) -> None:
        """`tr(f"共 {n} 条")` 提取出来是空的 —— 变量部分得留给 `.format()`。"""
        for where, name, node, _ctx in _calls():
            with self.subTest(at=where, call=name):
                self.assertNotIsInstance(node, ast.JoinedStr, f"{where} 用了 f-string")

    def test_tr_is_never_handed_a_variable(self) -> None:
        """`self.tr(变量)` 提取不到。变量文案一律走标记表 + `translate`。"""
        for where, name, node, _ctx in _calls():
            if name != "tr":
                continue
            with self.subTest(at=where):
                self.assertIsNotNone(_string(node), f"{where} 的 tr() 收到的不是字面量")

    def test_translate_always_names_its_context_literally(self) -> None:
        """源文本可以是变量（标记表就靠这个），但 context 不行 —— 那样整条都提取不到。

        唯一的例外是 `resolve_marker` 本身：它是所有标记表共用的查表器，context 只能
        由调用方传进来，而每个调用方给的都是字面量。多出别的例外就是真漏了。
        """
        offenders = {
            where.rsplit(":", 1)[0]
            for where, name, _node, context in _calls()
            if name == "translate" and _string(context) is None
        }
        self.assertEqual(offenders - {MARKER_HELPER}, set(), "context 必须写成字面量")


if __name__ == "__main__":
    unittest.main()
