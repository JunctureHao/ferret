"""Measure UI allocation in isolated Windows processes, without starting capture.

Run with ``uv run python tests/measure_memory.py --runs 5 --output report.json``.
Use ``--gui`` for the native Windows platform (opens a visible window); the default
uses offscreen Qt. Compare reports from the same platform, machine, and fixture.
Private Bytes is committed private memory; Working Set also includes shared DLLs,
font caches, and mapped resources. These are whole-process cumulative measurements,
not the exclusive ownership of individual widgets. Imports, bootstrap, and window
construction are separate phases. ``action_ms`` excludes the fixed settling period.
Settling pumps DeferredDelete as well as normal events: processEvents alone does
not release deleteLater widgets outside the application's real exec loop.

Each child uses a temporary configuration and offline HTTP fixtures, leaves the
kernel stopped, and suppresses automatic updates, tray display, and session scans.
Application fonts, resources, translation, styles, and MainWindow are retained.
The detail fixture includes a form request, query parameters, cookies, and a Chinese
JSON response. Call counters distinguish full details, summary/body fetches, raw
exports, and actual body builders. Tabs are switched before accessing lazy widgets.
Set PYTHONPATH to a saved source tree to compare it with current code using this
same script; reports record the imported source root and measurement script hash.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import json
import os
import statistics
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from unittest.mock import patch

_CALL_METRICS = (
    "detail_calls",
    "summary_calls",
    "request_body_calls",
    "response_body_calls",
    "messages_calls",
    "raw_request_calls",
    "raw_response_calls",
    "request_body_builds",
    "response_body_builds",
)
_REQUEST_TABS = {
    "Overview": "overview",
    "Raw": "req_raw",
    "Headers": "req_headers",
    "Body": "req_body",
    "Query": "query_widget",
    "Cookies": "cookie_card",
    "Comment": "comment_pane",
}
_RESPONSE_TABS = {
    "Raw": "raw_edit",
    "Headers": "header_card",
    "Body": "body_pane",
}


class _MemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _memory() -> dict[str, int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_MemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    counters = _MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(
        kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "private_bytes": counters.PrivateUsage,
        "working_set_bytes": counters.WorkingSetSize,
    }


def _child(args: argparse.Namespace) -> dict[str, Any]:
    # Must precede any PySide import. Do not inherit a caller's platform in GUI mode.
    os.environ["QT_QPA_PLATFORM"] = "windows" if args.gui else "offscreen"
    started = time.perf_counter()
    phases: list[dict[str, Any]] = []
    application: Any = None
    window: Any = None
    calls = dict.fromkeys(_CALL_METRICS, 0)

    def settle() -> None:
        if application is None:
            gc.collect()
            return
        deadline = time.perf_counter() + args.settle_ms / 1000
        while time.perf_counter() < deadline:
            application.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(0.01)
        gc.collect()
        application.app.processEvents()

    def phase(name: str, action=None) -> None:
        before = time.perf_counter()
        if action is not None:
            action()
        action_ms = (time.perf_counter() - before) * 1000
        settle()
        row: dict[str, Any] = {
            "name": name,
            "action_ms": round(action_ms, 3),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            **_memory(),
            **calls,
            "request_tabs_created": 0,
            "response_tabs_created": 0,
            "messages_created": False,
        }
        if window is not None:
            pane = window.captures_interface.content
            row.update(
                detail_created=pane.panel is not None,
                connection_tree_created=pane.tree is not None,
                settings_created=getattr(window.settings_interface, "_page", None)
                is not None,
                sessions_created=not hasattr(window.sessions_interface, "_page")
                or window.sessions_interface._page is not None,
            )
            panel = pane.panel
            if panel is not None:
                request_tabs = {
                    tab: getattr(panel, attribute, None) is not None
                    for tab, attribute in _REQUEST_TABS.items()
                }
                response_tabs = {
                    tab: getattr(panel.res_pane, attribute, None) is not None
                    for tab, attribute in _RESPONSE_TABS.items()
                }
                row.update(
                    request_tabs=request_tabs,
                    response_tabs=response_tabs,
                    request_tabs_created=sum(request_tabs.values()),
                    response_tabs_created=sum(response_tabs.values()),
                    messages_created=getattr(panel, "messages", None) is not None,
                )
        phases.append(row)

    phase("python")
    # Imports are intentionally timed after the Python baseline.
    before = time.perf_counter()
    from PySide6.QtCore import QCoreApplication, QEvent

    import ferret
    from ferret.core.application import Application
    from ferret.core.runtime import ApplicationRuntime
    from ferret.core.settings import CONFIG

    import_ms = (time.perf_counter() - before) * 1000
    phase("imports")
    phases[-1]["action_ms"] = round(import_ms, 3)

    with TemporaryDirectory(prefix="ferret-memory-") as directory, ExitStack() as stack:
        config_dir = Path(directory)
        stack.enter_context(
            patch("ferret.core.settings.get_config_dir", return_value=config_dir)
        )
        stack.enter_context(
            patch("ferret.core.runtime.get_config_dir", return_value=config_dir)
        )
        stack.enter_context(patch("ferret.apps.window.SystemTray.show"))
        stack.enter_context(
            patch("ferret.apps.session.controllers.SessionController.refresh")
        )
        stack.enter_context(
            patch("ferret.apps.update.coordinator.UpdateCoordinator.check_auto")
        )

        def bootstrap() -> None:
            nonlocal application
            application = Application()
            application._init_app_info()
            application._init_config()
            CONFIG.set(CONFIG.auto_check_update, False, save=False)
            application._init_font()
            application._init_dpi()
            application._create_qapp()
            application._init_i18n()

        phase("bootstrap", bootstrap)
        runtime = ApplicationRuntime()

        def create_window() -> None:
            nonlocal window
            from ferret.apps.window import MainWindow

            window = MainWindow(runtime)
            window.show()

        phase("window_idle", create_window)
        assert not runtime.mitm_runtime.is_running

        # This flow is owned by this GUI thread and never enters a live Master.
        # Models receive immutable FlowRow snapshots. Callbacks use the same
        # builders/exports as production while keeping capture and networking off.
        from mitmproxy.test import tflow

        from ferret.core import mitm
        from ferret.core.mitm import detail as detail_module

        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.request.url = "https://memory.example.test/api/items?page=1&sort=name"
        flow.request.method = "POST"
        flow.request.headers["content-type"] = "application/x-www-form-urlencoded"
        flow.request.headers["cookie"] = "session=memory-fixture; locale=zh-CN"
        flow.request.content = b"name=memory-fixture&enabled=true"
        flow.comment = "Offline memory fixture"
        flow.response.headers["content-type"] = "application/json; charset=utf-8"
        flow.response.content = json.dumps(
            {
                "标题": "中文 JSON 内存测量",
                "项目": [
                    {"序号": index, "名称": "测试项目", "启用": True, "值": [1, 2, 3]}
                    for index in range(args.json_rows)
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

        class Source:
            def __init__(self) -> None:
                self.rows = [mitm.flow_row(flow)]

            def __iter__(self):
                return iter(self.rows)

            def clear(self) -> None:
                self.rows.clear()

            def remove(self, ids) -> None:
                self.rows = [row for row in self.rows if row.id not in ids]

        def detail(flow_id: str) -> dict:
            assert flow_id == flow.id
            calls["detail_calls"] += 1
            return mitm.build_flow_detail(flow)

        def summary(flow_id: str) -> dict:
            assert flow_id == flow.id
            calls["summary_calls"] += 1
            return mitm.build_flow_summary(flow)

        def body(flow_id: str, side: Literal["Request", "Response"]) -> dict:
            assert flow_id == flow.id
            assert side in ("Request", "Response")
            calls[f"{side.lower()}_body_calls"] += 1
            return mitm.build_flow_body(flow, side)

        def messages(flow_id: str) -> dict:
            assert flow_id == flow.id
            calls["messages_calls"] += 1
            return mitm.build_flow_messages(flow)

        def raw_request(flow_id: str) -> bytes:
            assert flow_id == flow.id
            calls["raw_request_calls"] += 1
            return mitm.FlowExporter.raw_request(flow)

        def raw_response(flow_id: str) -> bytes:
            assert flow_id == flow.id
            calls["raw_response_calls"] += 1
            return mitm.FlowExporter.raw_response(flow)

        original_build_body = detail_module.build_body

        def build_body(target_flow, message) -> dict:
            assert target_flow is flow
            side = "request" if message is flow.request else "response"
            calls[f"{side}_body_builds"] += 1
            return original_build_body(target_flow, message)

        pane = window.captures_interface.content
        stack.enter_context(patch.object(pane.controller, "flow_detail", detail))
        # Older source snapshots intentionally lack the narrow fetch APIs.
        for name, callback in (
            ("flow_summary", summary),
            ("flow_body", body),
            ("flow_messages", messages),
        ):
            if hasattr(pane.controller, name):
                stack.enter_context(patch.object(pane.controller, name, callback))
        stack.enter_context(patch.object(detail_module, "build_body", build_body))
        stack.enter_context(
            patch.object(pane.controller, "get_raw_request", raw_request)
        )
        stack.enter_context(
            patch.object(pane.controller, "get_raw_response", raw_response)
        )
        phase("fixture_loaded", lambda: pane.set_source(Source()))
        phase("selection_collapsed", lambda: pane.table.selectRow(0))
        phase("detail_first_open", pane.open_selected)
        assert pane.panel is not None
        phase(
            "response_headers_first_open",
            lambda: pane.panel.res_pane.setCurrentTab("Headers"),
        )
        phase("response_raw_reopen", lambda: pane.panel.res_pane.setCurrentTab("Raw"))
        for tab in ("Headers", "Query", "Cookies", "Comment", "Raw", "Body"):
            phase(
                f"request_{tab.lower()}_first_open",
                lambda tab=tab: pane.panel.req_tabs.setCurrentTab(tab),
            )
        phase("chinese_json_body", lambda: pane.panel.res_pane.setCurrentTab("Body"))
        body_panel = pane.panel.res_pane.body_pane.json_panel
        phase("json_tree_first_open", body_panel._btn_tree.click)
        phase(
            "request_overview_reopen",
            lambda: pane.panel.req_tabs.setCurrentTab("Overview"),
        )
        phase("connection_tree_first_open", lambda: pane.set_grouping_mode("conn"))
        phase("settings_first_open", lambda: window.switchTo(window.settings_interface))
        phase("sessions_first_open", lambda: window.switchTo(window.sessions_interface))

        def repeat() -> None:
            window.switchTo(window.captures_interface)
            pane.set_grouping_mode("flat")
            pane.collapse_panel()
            pane.table.selectRow(0)
            pane.open_selected()
            for tab in ("Headers", "Query", "Cookies", "Comment", "Raw", "Body"):
                pane.panel.req_tabs.setCurrentTab(tab)
            pane.panel.req_tabs.setCurrentTab("Overview")
            for tab in ("Headers", "Raw", "Body"):
                pane.panel.res_pane.setCurrentTab(tab)
            body_panel._btn_text.click()
            body_panel._btn_tree.click()
            pane.set_grouping_mode("conn")
            window.switchTo(window.settings_interface)
            window.switchTo(window.sessions_interface)

        for index in range(args.repeat):
            phase(f"repeat_{index + 1:02}", repeat)
        assert not runtime.mitm_runtime.is_running
        # Avoid the close handler: it deliberately asks to save active captures.
        window.tray_icon.hide()
        window.deleteLater()
        runtime.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        CONFIG.flush_pending_save()

    return {
        "pid": os.getpid(),
        "platform": "windows" if args.gui else "offscreen",
        "source_root": str(Path(ferret.__file__).resolve().parent.parent),
        "phases": phases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--settle-ms", type=int, default=700)
    parser.add_argument("--json-rows", type=int, default=250)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--child", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Private Bytes measurement currently requires Windows")
    if args.runs < 1 or args.settle_ms < 1 or args.json_rows < 1 or args.repeat < 1:
        parser.error("runs, settle-ms, json-rows, and repeat must be positive")
    if args.child is not None:
        args.child.write_text(
            json.dumps(_child(args), ensure_ascii=False), encoding="utf-8"
        )
        return

    runs = []
    with TemporaryDirectory(prefix="ferret-memory-results-") as directory:
        for index in range(args.runs):
            result = Path(directory) / f"run-{index + 1}.json"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--child",
                str(result),
                "--settle-ms",
                str(args.settle_ms),
                "--json-rows",
                str(args.json_rows),
                "--repeat",
                str(args.repeat),
            ]
            if args.gui:
                command.append("--gui")
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if completed.returncode:
                sys.stderr.write(completed.stdout + completed.stderr)
                raise SystemExit(completed.returncode)
            runs.append(json.loads(result.read_text(encoding="utf-8")))
            print(f"Completed run {index + 1}/{args.runs}", flush=True)

    summary = []
    for index, first in enumerate(runs[0]["phases"]):
        measurements = [run["phases"][index] for run in runs]
        summary.append(
            {
                "name": first["name"],
                **{
                    metric: {
                        "median": statistics.median(
                            row[metric] for row in measurements
                        ),
                        "min": min(row[metric] for row in measurements),
                        "max": max(row[metric] for row in measurements),
                    }
                    for metric in (
                        "private_bytes",
                        "working_set_bytes",
                        "action_ms",
                        *_CALL_METRICS,
                        "request_tabs_created",
                        "response_tabs_created",
                        "messages_created",
                    )
                },
            }
        )
    report = {
        "schema": 3,
        "deferred_delete": True,
        "fixture": "form-query-cookie-chinese-json",
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_root": runs[0]["source_root"],
        "python": sys.version,
        "platform": "windows" if args.gui else "offscreen",
        "settle_ms": args.settle_ms,
        "json_rows": args.json_rows,
        "repeat": args.repeat,
        "runs": runs,
        "summary": summary,
    }
    print(f"{'Phase':28} {'Private MiB':>12} {'WS MiB':>12} {'Action ms':>12}")
    for row in summary:
        print(
            f"{row['name']:28} "
            f"{row['private_bytes']['median'] / 2**20:12.2f} "
            f"{row['working_set_bytes']['median'] / 2**20:12.2f} "
            f"{row['action_ms']['median']:12.2f}"
        )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Saved {args.output.resolve()}")


if __name__ == "__main__":
    main()
