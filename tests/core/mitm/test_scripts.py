"""Tests for the user-scripts feature: model, loader, addon wiring, kernel run."""

from __future__ import annotations

import http.client
import http.server
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    MitmFacade,
    MitmRuntime,
    ScriptEntry,
    ScriptState,
    ScriptStatus,
    scripts_from_config,
    scripts_to_config,
)
from ferret.core.mitm.scripts import load_script_module
from tests.core.mitm._qt import start_runtime, wait_until


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


HEADER_SCRIPT = 'def request(f):\n    f.request.headers["X-T"] = "1"\n'
BROKEN_SCRIPT = "def request(f:\n"


class ScriptEntryTests(unittest.TestCase):
    """模型层：校验 / 序列化往返 / 配置容错。"""

    def test_validate_rejects_blank_path(self) -> None:
        with self.assertRaises(ValueError):
            ScriptEntry(path="  ").validate()

    def test_validate_rejects_non_py_suffix(self) -> None:
        with self.assertRaises(ValueError):
            ScriptEntry(path="/tmp/a.txt").validate()

    def test_validate_accepts_a_normal_path(self) -> None:
        ScriptEntry(path="/tmp/a.py").validate()

    def test_roundtrip(self) -> None:
        entry = ScriptEntry(path="/tmp/a.py", enabled=False, origin="new")
        clone = ScriptEntry.from_dict(entry.to_dict())
        self.assertEqual(clone, entry)

    def test_from_dict_defaults_origin_to_import(self) -> None:
        entry = ScriptEntry.from_dict({"path": "/tmp/a.py"})
        self.assertEqual(entry.origin, "import")
        self.assertTrue(entry.enabled)

    def test_from_dict_rejects_bad_shapes(self) -> None:
        with self.assertRaises(TypeError):
            ScriptEntry.from_dict("nope")
        with self.assertRaises(ValueError):
            ScriptEntry.from_dict({"enabled": True})

    def test_scripts_from_config_skips_bad_entries(self) -> None:
        raw = [{"path": "/tmp/a.py"}, "junk", {"path": ""}, {"path": "/tmp/b.py"}]
        entries = scripts_from_config(raw)
        self.assertEqual([e.path for e in entries], ["/tmp/a.py", "/tmp/b.py"])
        self.assertEqual(scripts_from_config("not a list"), [])

    def test_scripts_to_config_roundtrip(self) -> None:
        entries = [
            ScriptEntry(path="/tmp/a.py"),
            ScriptEntry(path="/tmp/b.py", enabled=False),
        ]
        self.assertEqual(scripts_from_config(scripts_to_config(entries)), entries)


class LoadScriptModuleTests(unittest.TestCase):
    """纯装载器：report 回调语义，不碰 master。"""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.reports: list[tuple[str, BaseException]] = []

    def write(self, name: str, text: str) -> str:
        path = Path(self.dir.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def report(self, path: str, exc: BaseException) -> None:
        self.reports.append((path, exc))

    def test_good_script_loads_as_a_module(self) -> None:
        path = self.write("good.py", HEADER_SCRIPT)
        module = load_script_module(path, self.report)
        assert module is not None
        self.assertEqual(self.reports, [])
        self.assertTrue(module.__name__.startswith("__mitmproxy_script__."))
        self.assertTrue(hasattr(module, "request"))

    def test_syntax_error_reports_and_returns_none(self) -> None:
        path = self.write("broken.py", BROKEN_SCRIPT)
        module = load_script_module(path, self.report)
        self.assertIsNone(module)
        self.assertEqual(len(self.reports), 1)
        self.assertIsInstance(self.reports[0][1], SyntaxError)

    def test_missing_file_reports_file_not_found(self) -> None:
        module = load_script_module(str(Path(self.dir.name) / "nope.py"), self.report)
        self.assertIsNone(module)
        self.assertIsInstance(self.reports[0][1], FileNotFoundError)

    def test_reload_replaces_the_module(self) -> None:
        path = self.write("v.py", "MARK = 1\n")
        first = load_script_module(path, self.report)
        assert first is not None
        Path(path).write_text("MARK = 2\n", encoding="utf-8")
        second = load_script_module(path, self.report)
        assert second is not None
        self.assertIsNot(first, second)
        self.assertEqual(first.MARK, 1)
        self.assertEqual(second.MARK, 2)


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """回显服务器：把收到的请求头记下来供断言。"""

    received: ClassVar[list[http.client.HTTPMessage]] = []

    def _reply(self) -> None:
        # typeshed 把 `headers` 标成基类 `email.message.Message`，运行期是 HTTPMessage。
        type(self).received.append(self.headers)  # ty: ignore[invalid-argument-type]
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, format: str, *args) -> None:
        pass  # 安静点


class RuntimeScriptTests(unittest.TestCase):
    """内核集成：起 runtime，脚本钩子改请求头，热更 / 停用 / 重载 / 状态上报。"""

    @classmethod
    def setUpClass(cls) -> None:
        QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        _EchoHandler.received = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

        self.runtime = MitmRuntime(listen_port=free_port())
        # 脚本总开关出厂**关**（core/settings.py::scripts_enabled）：这一组用例都在
        # 验证脚本行为本身，先把主闸打开，等价于用户在界面上开了脚本页开关。
        self.runtime.scripts_enabled = True
        self.addCleanup(self.runtime.stop)
        self.facade = MitmFacade(self.runtime)
        self.statuses: list[tuple[str, ScriptStatus]] = []
        self.runtime.script_status_changed.connect(
            lambda path, status: self.statuses.append((path, status))
        )

    def write(self, name: str, text: str) -> str:
        path = Path(self.dir.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def send_once(self) -> None:
        """发一条请求并等**这一条**到达 —— 按条数增长等，不看「非空」。

        `received` 是跨用例累积的：只等 `bool(received)` 的话第二次调用会立刻
        返回，`last_headers()` 读到的还是上一条请求的头，断言就成了自说自话。
        """
        before = len(_EchoHandler.received)
        self.facade.send_custom_request("GET", self.url, [], b"", record=False)
        self.assertTrue(
            wait_until(lambda: len(_EchoHandler.received) > before),
            "请求没有在超时内到达本地服务器",
        )

    def last_headers(self) -> http.client.HTTPMessage:
        return _EchoHandler.received[-1]

    def test_script_hook_modifies_the_request(self) -> None:
        path = self.write("mark.py", HEADER_SCRIPT)
        self.runtime.scripts = [ScriptEntry(path=path)]
        start_runtime(self.runtime)

        self.assertTrue(
            wait_until(lambda: bool(self.statuses)),
            "装载状态信号没有在超时内到达",
        )
        self.assertEqual(self.statuses[0][1].state, ScriptState.LOADED)
        self.assertEqual(self.facade.script_statuses[path].state, ScriptState.LOADED)

        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "1")

    def test_syntax_error_becomes_error_status_and_other_scripts_survive(self) -> None:
        bad = self.write("bad.py", BROKEN_SCRIPT)
        good = self.write("good.py", HEADER_SCRIPT)
        self.runtime.scripts = [
            ScriptEntry(path=bad),
            ScriptEntry(path=good),
        ]
        start_runtime(self.runtime)

        self.assertTrue(
            wait_until(lambda: len(self.statuses) >= 2),
            "两条状态信号没有在超时内到齐",
        )
        states = {path: status for path, status in self.statuses}
        self.assertEqual(states[bad].state, ScriptState.ERROR)
        self.assertIn("SyntaxError", states[bad].error)
        self.assertEqual(states[good].state, ScriptState.LOADED)

        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "1")

    def test_missing_file_becomes_missing_status(self) -> None:
        gone = str(Path(self.dir.name) / "gone.py")
        self.runtime.scripts = [ScriptEntry(path=gone)]
        start_runtime(self.runtime)

        self.assertTrue(wait_until(lambda: bool(self.statuses)))
        self.assertEqual(self.statuses[0][1].state, ScriptState.MISSING)

    def test_disabled_entry_keeps_config_and_stays_out_of_dispatch(self) -> None:
        path = self.write("mark.py", HEADER_SCRIPT)
        self.runtime.scripts = [ScriptEntry(path=path)]
        start_runtime(self.runtime)
        self.assertTrue(wait_until(lambda: bool(self.statuses)))

        # 停用：配置保留，钩子不再生效。
        self.facade.set_scripts([ScriptEntry(path=path, enabled=False)])
        self.assertTrue(
            wait_until(
                lambda: any(s.state == ScriptState.DISABLED for _, s in self.statuses)
            )
        )
        self.assertEqual(len(self.facade.scripts), 1)
        self.send_once()
        self.assertIsNone(self.last_headers().get("X-T"))

        # 重启用：同一路径换批，钩子回来。
        self.facade.set_scripts([ScriptEntry(path=path)])
        self.assertTrue(
            wait_until(
                lambda: (
                    self.statuses and self.statuses[-1][1].state == ScriptState.LOADED
                )
            )
        )
        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "1")

    def test_hot_swap_replaces_the_old_namespace(self) -> None:
        first = self.write(
            "one.py", 'def request(f):\n    f.request.headers["X-T"] = "1"\n'
        )
        second = self.write(
            "two.py", 'def request(f):\n    f.request.headers["X-T"] = "2"\n'
        )
        self.runtime.scripts = [ScriptEntry(path=first)]
        start_runtime(self.runtime)
        self.assertTrue(wait_until(lambda: bool(self.statuses)))

        self.facade.set_scripts([ScriptEntry(path=second)])
        self.assertTrue(
            wait_until(
                lambda: any(p == second for p, _ in self.statuses),
            )
        )
        # 注册表键取自 addon 的 `name` 属性，脚本模块那一份由装载器填成脚本路径
        # （原生 `load_script` 同款），所以这里按路径查：旧 ns 必须已经被 remove。
        lookup = self.runtime.call(
            lambda: sorted(
                name
                for name in self.runtime.master.addons.lookup  # ty: ignore[unresolved-attribute]
                if name in (first, second)
            )
        )
        self.assertEqual(lookup, [second])
        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "2")

    def test_reload_re_executes_the_module(self) -> None:
        path = self.write(
            "mark.py", 'def request(f):\n    f.request.headers["X-T"] = "1"\n'
        )
        self.runtime.scripts = [ScriptEntry(path=path)]
        start_runtime(self.runtime)
        self.assertTrue(wait_until(lambda: bool(self.statuses)))

        Path(path).write_text(
            'def request(f):\n    f.request.headers["X-T"] = "9"\n', encoding="utf-8"
        )
        self.facade.reload_script(path)
        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "9")

    def test_master_switch_gates_every_script(self) -> None:
        """总开关关掉：所有脚本一律不派发、状态转停用；重开后恢复，各行启用位不变。"""
        path = self.write("mark.py", HEADER_SCRIPT)
        self.runtime.scripts = [ScriptEntry(path=path)]
        start_runtime(self.runtime)
        self.assertTrue(wait_until(lambda: bool(self.statuses)))
        self.assertEqual(self.facade.script_statuses[path].state, ScriptState.LOADED)

        # 关总开关：钩子不再生效，条目状态转停用，各行 enabled 落盘值原样保留。
        self.facade.set_scripts_enabled(False)
        self.assertTrue(
            wait_until(
                lambda: self.facade.script_statuses.get(path)
                == ScriptStatus(ScriptState.DISABLED)
            )
        )
        self.assertFalse(self.facade.scripts_enabled)
        self.assertTrue(self.facade.scripts[0].enabled)
        self.send_once()
        self.assertIsNone(self.last_headers().get("X-T"))

        # 重开：脚本回到派发链。
        self.facade.set_scripts_enabled(True)
        self.assertTrue(
            wait_until(
                lambda: self.facade.script_statuses.get(path)
                == ScriptStatus(ScriptState.LOADED)
            )
        )
        self.send_once()
        self.assertEqual(self.last_headers().get("X-T"), "1")

    def test_apply_scripts_rejects_an_invalid_entry_without_touching_state(
        self,
    ) -> None:
        with self.assertRaises(ValueError):
            self.facade.set_scripts([ScriptEntry(path="/tmp/a.txt")])
        self.assertEqual(self.facade.scripts, [])

    def test_script_statuses_empty_when_stopped(self) -> None:
        self.assertEqual(self.facade.script_statuses, {})


if __name__ == "__main__":
    unittest.main()
