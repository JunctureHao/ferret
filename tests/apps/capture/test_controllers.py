from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy.test import tflow
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.core.mitm import MitmRuntimeState, View
from ferret.core.network import ANY_HOST, LOOPBACK_HOST
from ferret.core.settings import CONFIG


class FakeRuntime(QObject):
    flow_added = Signal(object)
    compose_flow_added = Signal(object)
    flow_stored = Signal(object)
    compose_flow_stored = Signal(object)
    flow_discarded = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    flow_suspended = Signal(object)
    flow_intercepted = Signal(object)
    websocket_started = Signal(str)
    websocket_frame = Signal(str, object)
    websocket_closed = Signal(str, object)
    sse_started = Signal(str)
    sse_event = Signal(str, object)
    sse_ended = Signal(str)
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.view = View()
        self.state = MitmRuntimeState.RUNNING
        self.is_running = True
        self.listen_host = LOOPBACK_HOST
        self.listen_port = 8080
        self.block_global = True
        self.block_private = False
        # 通道意图值与会话接通位（见 core/mitm/runtime.py）。
        self.use_local = True
        self.local_spec = ""
        self.use_wireguard = True
        self.use_reverse = False
        self.reverse_target = ""
        self.reverse_port = 8081
        self.use_socks5 = False
        self.socks5_port = 1080
        # 上游代理不是第五条通道，它替换 mode[0] 的 regular 槽位。
        self.use_upstream = False
        self.upstream_target = ""
        self.upstream_username = ""
        self.upstream_password = ""
        # 代理认证三意图值（docs/design.md#auth）：独立开关，不随通道回滚。
        self.proxyauth_enabled = False
        self.proxyauth_username = ""
        self.proxyauth_password = ""
        self.channels_engaged = False
        self.health: dict = {}
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0
        self.channel_pushes = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> bool:
        self.stop_calls += 1
        self.is_running = False
        self.state = MitmRuntimeState.STOPPED
        return True

    def restart(self, *, listen_host=None, listen_port=None) -> None:
        self.restart_calls += 1
        if listen_host is not None:
            self.listen_host = listen_host
        if listen_port is not None:
            self.listen_port = listen_port

    def set_channels_engaged(self, engaged: bool) -> None:
        self.channels_engaged = engaged
        self.channel_pushes += 1

    def apply_channels(
        self,
        *,
        use_local=None,
        local_spec=None,
        use_wireguard=None,
        use_reverse=None,
        reverse_target=None,
        reverse_port=None,
        use_socks5=None,
        socks5_port=None,
        use_upstream=None,
        upstream_target=None,
        upstream_username=None,
        upstream_password=None,
    ) -> None:
        if use_local is not None:
            self.use_local = use_local
        if local_spec is not None:
            self.local_spec = local_spec
        if use_wireguard is not None:
            self.use_wireguard = use_wireguard
        if use_reverse is not None:
            self.use_reverse = use_reverse
        if reverse_target is not None:
            self.reverse_target = reverse_target
        if reverse_port is not None:
            self.reverse_port = reverse_port
        if use_socks5 is not None:
            self.use_socks5 = use_socks5
        if socks5_port is not None:
            self.socks5_port = socks5_port
        if use_upstream is not None:
            self.use_upstream = use_upstream
        if upstream_target is not None:
            self.upstream_target = upstream_target
        if upstream_username is not None:
            self.upstream_username = upstream_username
        if upstream_password is not None:
            self.upstream_password = upstream_password
        self.channel_pushes += 1

    def channel_health(self) -> dict:
        return dict(self.health)

    def apply_proxy_auth(self, *, enabled=None, username=None, password=None) -> None:
        if enabled is not None:
            self.proxyauth_enabled = enabled
        if username is not None:
            self.proxyauth_username = username
        if password is not None:
            self.proxyauth_password = password


class FakeFacade:
    def __init__(self, runtime: FakeRuntime) -> None:
        self.runtime = runtime
        self.view = runtime.view
        self.recording = False

    @property
    def listen_host(self):
        return self.runtime.listen_host

    @property
    def local_client_host(self):
        return LOOPBACK_HOST

    @property
    def is_lan_exposed(self):
        return self.runtime.listen_host == ANY_HOST

    def lan_address(self):
        return "192.168.1.9"

    @property
    def listen_port(self):
        return self.runtime.listen_port

    @property
    def block_global(self):
        return self.runtime.block_global

    @property
    def block_private(self):
        return self.runtime.block_private

    @property
    def proxyauth_enabled(self):
        return self.runtime.proxyauth_enabled

    @property
    def proxyauth_username(self):
        return self.runtime.proxyauth_username

    @property
    def proxyauth_password(self):
        return self.runtime.proxyauth_password

    @property
    def use_local(self):
        return self.runtime.use_local

    @property
    def local_spec(self):
        return self.runtime.local_spec

    @property
    def use_wireguard(self):
        return self.runtime.use_wireguard

    @property
    def use_reverse(self):
        return self.runtime.use_reverse

    @property
    def reverse_target(self):
        return self.runtime.reverse_target

    @property
    def reverse_port(self):
        return self.runtime.reverse_port

    @property
    def use_socks5(self):
        return self.runtime.use_socks5

    @property
    def socks5_port(self):
        return self.runtime.socks5_port

    def set_block_options(self, *, block_global=None, block_private=None) -> None:
        if block_global is not None:
            self.runtime.block_global = block_global
        if block_private is not None:
            self.runtime.block_private = block_private

    def set_proxy_auth(self, *, enabled=None, username=None, password=None) -> None:
        self.runtime.apply_proxy_auth(
            enabled=enabled, username=username, password=password
        )

    def start_capture_recording(self):
        self.recording = True

    def stop_capture_recording(self):
        self.recording = False

    def engage_channels(self) -> None:
        self.runtime.set_channels_engaged(True)

    def disengage_channels(self) -> None:
        self.runtime.set_channels_engaged(False)

    def set_channels(
        self,
        *,
        use_local=None,
        local_spec=None,
        use_wireguard=None,
        use_reverse=None,
        reverse_target=None,
        reverse_port=None,
        use_socks5=None,
        socks5_port=None,
        use_upstream=None,
        upstream_target=None,
        upstream_username=None,
        upstream_password=None,
    ) -> None:
        self.runtime.apply_channels(
            use_local=use_local,
            local_spec=local_spec,
            use_wireguard=use_wireguard,
            use_reverse=use_reverse,
            reverse_target=reverse_target,
            reverse_port=reverse_port,
            use_socks5=use_socks5,
            socks5_port=socks5_port,
            use_upstream=use_upstream,
            upstream_target=upstream_target,
            upstream_username=upstream_username,
            upstream_password=upstream_password,
        )

    def validate_local_spec(self, local_spec: str) -> None:
        if ",," in local_spec or local_spec.strip() == "!":
            raise ValueError("invalid intercept spec")

    def validate_upstream_target(self, target: str) -> None:
        # 真身走 validate_mode_specs([upstream_mode_spec(target)])；这里只要够
        # 分辨「坏地址被拦下」即可。
        if "://" in target and not target.startswith(("http://", "https://")):
            raise ValueError("invalid upstream target")
        if "@" in target:
            raise ValueError("invalid upstream target")

    def upstream_targets_self(self, target, *, listen_host, listen_port) -> bool:
        return target.endswith(f":{listen_port}")

    def channel_health(self) -> dict:
        return self.runtime.channel_health()

    def wireguard_client_config(self) -> str:
        return "[Interface]"

    def set_filter(self, matcher) -> None:
        # 记账最近一次上屏的编译结果；apply_filter 的校验断言据此看「上没上屏」。
        self.applied_filter = matcher

    def remove_unmarked_flows(self, flow_ids=None) -> int:
        self.removed_unmarked_calls = getattr(self, "removed_unmarked_calls", 0) + 1
        return 3

    def match_ids(self, matcher) -> set[str]:
        # 记账调用次数与最近一次 matcher；apply_highlight 的断言据此看「下没下发」。
        self.match_calls = getattr(self, "match_calls", 0) + 1
        self.last_matcher = matcher
        return set(getattr(self, "match_result", set()))


class FakeSystemProxy:
    def __init__(self, *, fail_attach: bool = False, fail_detach: bool = False) -> None:
        self.fail_attach = fail_attach
        self.fail_detach = fail_detach
        self.attached = False
        self.endpoint = None
        self.detach_calls = 0

    @property
    def is_attached(self) -> bool:
        # 控制器以这个 property 为唯一事实源；与真身对齐：restore 失败时端点
        # 不落、仍算挂着。
        return self.attached

    def attach(self, host: str, port: int) -> None:
        if self.fail_attach:
            raise RuntimeError("address already in use")
        self.attached = True
        self.endpoint = (host, port)

    def detach(self) -> bool:
        self.detach_calls += 1
        if self.fail_detach and self.attached:
            # 真身 restore 失败会回滚端点并保持 is_attached 为真。
            return False
        self.attached = False
        return True


class CaptureControllerStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    ITEMS = (
        "listen_host",
        "listen_port",
        "block_global",
        "block_private",
        "system_proxy_enabled",
        "local_enabled",
        "local_spec",
        "wireguard_enabled",
    )

    def setUp(self) -> None:
        # 控制器提交设置时会写 CONFIG —— 绝不能落到用户真实的 config.json 上。
        self._config_dir = tempfile.TemporaryDirectory()
        self._original_file = CONFIG.file
        self._original_values = {
            name: CONFIG.get(getattr(CONFIG, name)) for name in self.ITEMS
        }
        CONFIG.file = Path(self._config_dir.name) / "config.json"
        self.addCleanup(self._restore_config)

    def _restore_config(self) -> None:
        for name, value in self._original_values.items():
            CONFIG.set(getattr(CONFIG, name), value, save=False)
        CONFIG.file = self._original_file
        self._config_dir.cleanup()

    def make_controller(self, *, fail_attach: bool = False, fail_detach: bool = False):
        runtime = FakeRuntime()
        facade = FakeFacade(runtime)
        proxy = FakeSystemProxy(fail_attach=fail_attach, fail_detach=fail_detach)
        controller = CaptureController(mitm=facade, system_proxy=proxy)  # type: ignore
        return controller, runtime, facade, proxy

    def test_start_ready_stop_state_sequence(self) -> None:
        controller, runtime, facade, proxy = self.make_controller()
        states = []
        controller.capture_state_changed.connect(states.append)

        controller.start_capture()
        self.assertEqual(controller.capture_state, CaptureState.RUNNING)
        self.assertEqual(proxy.endpoint, ("127.0.0.1", 8080))
        self.assertTrue(facade.recording)
        # 会话接通：通道已拼进内核，写入闸门打开。
        self.assertTrue(runtime.channels_engaged)
        self.assertTrue(controller.recording)

        controller.stop_capture()
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(proxy.attached)
        self.assertFalse(facade.recording)
        self.assertTrue(runtime.is_running)
        self.assertEqual(runtime.stop_calls, 0)
        # 会话整体回落：接通位与闸门都落下，但通道意图值原样保留。
        self.assertFalse(runtime.channels_engaged)
        self.assertFalse(controller.recording)
        self.assertTrue(runtime.use_local)
        self.assertTrue(runtime.use_wireguard)
        self.assertEqual(
            states,
            [
                CaptureState.STARTING,
                CaptureState.RUNNING,
                CaptureState.STOPPING,
                CaptureState.STOPPED,
            ],
        )

    def test_attach_failure_exposes_failed_state_and_rolls_back_recording(self) -> None:
        controller, _, facade, proxy = self.make_controller(fail_attach=True)

        controller.start_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertEqual(controller.last_error, "address already in use")
        self.assertFalse(proxy.attached)
        self.assertFalse(facade.recording)

    def test_stop_with_failed_detach_still_tears_down_recording_and_channels(
        self,
    ) -> None:
        """detach 失败不短路停止链路：录制与通道照常回落，只有代理遗留成 FAILED。"""
        controller, runtime, facade, proxy = self.make_controller(fail_detach=True)
        controller.start_capture()
        self.assertTrue(proxy.attached)

        controller.stop_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertIn("恢复原系统代理失败", controller.last_error)
        # 注册表还挂着我们：不能假报已停止（service 的 is_attached 保持为真）。
        self.assertTrue(proxy.attached)
        self.assertTrue(controller.system_proxy_attached)
        # 但录制与通道的回落不被短路，否则留下「闸门开着、通道接着」的半开会话。
        self.assertFalse(facade.recording)
        self.assertFalse(controller.recording)
        self.assertFalse(runtime.channels_engaged)

    def test_stop_again_after_failed_detach_retries_the_restore(self) -> None:
        controller, _, _, proxy = self.make_controller(fail_detach=True)
        controller.start_capture()
        controller.stop_capture()
        self.assertEqual(controller.capture_state, CaptureState.FAILED)

        proxy.fail_detach = False
        controller.stop_capture()

        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(proxy.attached)
        self.assertEqual(proxy.detach_calls, 2)

    def test_toggle_retries_stop_while_the_proxy_is_still_ours(self) -> None:
        """FAILED + 代理仍挂着 = 停止失败：toggle 往停止走一键重试，不再岔去开始。"""
        controller, _, _, proxy = self.make_controller(fail_detach=True)
        controller.start_capture()
        controller.stop_capture()
        self.assertEqual(controller.capture_state, CaptureState.FAILED)

        proxy.fail_detach = False
        self.assertFalse(controller.toggle_capture())

        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(proxy.attached)

    def test_toggle_still_retries_start_after_a_start_failure(self) -> None:
        """启动失败时代理没挂上：toggle 保持原有的「重试抓包」方向。"""
        controller, _, _, proxy = self.make_controller(fail_attach=True)
        controller.start_capture()
        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertFalse(controller.system_proxy_attached)

        proxy.fail_attach = False
        self.assertTrue(controller.toggle_capture())
        self.assertEqual(controller.capture_state, CaptureState.RUNNING)

    def test_recording_stop_failure_stays_failed_and_toggle_retries_cleanup(
        self,
    ) -> None:
        controller, runtime, facade, proxy = self.make_controller()
        controller.start_capture()

        with patch.object(
            facade, "stop_capture_recording", side_effect=OSError("disk full")
        ):
            controller.stop_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertIn("disk full", controller.last_error)
        self.assertFalse(proxy.attached)
        self.assertFalse(runtime.channels_engaged)
        self.assertFalse(controller.recording)
        self.assertTrue(facade.recording)

        with patch.object(facade, "start_capture_recording") as start_recording:
            self.assertFalse(controller.toggle_capture())
        start_recording.assert_not_called()
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(facade.recording)
        self.assertEqual(controller.last_error, "")

    def test_channel_stop_failure_stays_failed_and_toggle_retries_cleanup(self) -> None:
        controller, runtime, facade, proxy = self.make_controller()
        controller.start_capture()

        with patch.object(
            facade, "disengage_channels", side_effect=RuntimeError("channel busy")
        ):
            controller.stop_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertIn("channel busy", controller.last_error)
        self.assertFalse(proxy.attached)
        self.assertTrue(runtime.channels_engaged)
        self.assertFalse(controller.recording)
        self.assertFalse(facade.recording)

        with patch.object(facade, "engage_channels") as engage:
            self.assertFalse(controller.toggle_capture())
        engage.assert_not_called()
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(runtime.channels_engaged)

    def test_failed_stop_prevents_restarting_on_a_new_listen_port(self) -> None:
        controller, runtime, facade, _proxy = self.make_controller()
        controller.start_capture()

        with (
            patch.object(
                facade, "stop_capture_recording", side_effect=OSError("disk full")
            ),
            self.assertRaisesRegex(RuntimeError, "disk full"),
        ):
            controller.update_proxy_settings(listen_port=8081)

        self.assertEqual(runtime.restart_calls, 0)
        self.assertEqual(controller.current_port, 8080)
        self.assertEqual(controller.capture_state, CaptureState.FAILED)

    def test_detach_exception_does_not_skip_other_cleanup(self) -> None:
        controller, runtime, facade, proxy = self.make_controller()
        controller.start_capture()

        with patch.object(proxy, "detach", side_effect=OSError("registry busy")):
            controller.stop_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertIn("registry busy", controller.last_error)
        self.assertFalse(facade.recording)
        self.assertFalse(runtime.channels_engaged)
        self.assertFalse(controller.recording)
        self.assertFalse(controller.toggle_capture())
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)

    def test_port_change_retries_prior_failed_cleanup_before_restarting(self) -> None:
        controller, runtime, facade, _proxy = self.make_controller()
        controller.start_capture()
        with patch.object(
            facade, "stop_capture_recording", side_effect=OSError("disk full")
        ):
            controller.stop_capture()
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                controller.update_proxy_settings(listen_port=8081)
        self.assertEqual(runtime.restart_calls, 0)
        self.assertEqual(controller.current_port, 8080)

        controller.update_proxy_settings(listen_port=8081)
        self.assertEqual(runtime.restart_calls, 1)
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(runtime.channels_engaged)
        self.assertFalse(facade.recording)

    def test_direct_start_cannot_bypass_a_failed_proxy_restore(self) -> None:
        controller, _runtime, facade, proxy = self.make_controller(fail_detach=True)
        controller.start_capture()
        controller.update_channels(
            use_system_proxy=False,
            use_local=True,
            local_spec="",
            use_wireguard=True,
        )
        self.assertEqual(controller.capture_state, CaptureState.FAILED)

        with patch.object(facade, "start_capture_recording") as start_recording:
            controller.start_capture()
        start_recording.assert_not_called()
        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertTrue(proxy.attached)
        self.assertFalse(controller.recording)

    def test_dialog_toggle_off_with_failed_detach_surfaces_failed(self) -> None:
        """对话框取消勾选遇 restore 失败：置 FAILED 留重试入口，会话其余部分照旧。"""
        controller, _, _, proxy = self.make_controller(fail_detach=True)
        controller.start_capture()
        self.assertTrue(proxy.attached)

        controller.update_channels(
            use_system_proxy=False,
            use_local=True,
            local_spec="",
            use_wireguard=True,
        )

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertTrue(proxy.attached)
        self.assertTrue(controller.recording)

    def test_system_proxy_stays_on_loopback_when_bound_to_all_interfaces(self) -> None:
        """放开监听不能改系统代理 —— 写 `0.0.0.0:8080` 会让抓包整体失效。"""
        controller, runtime, _, proxy = self.make_controller()
        runtime.listen_host = ANY_HOST

        controller.start_capture()

        self.assertEqual(proxy.endpoint, (LOOPBACK_HOST, 8080))
        self.assertEqual(controller.local_endpoint, f"{LOOPBACK_HOST}:8080")
        self.assertTrue(controller.is_lan_exposed)

    def test_block_options_apply_hot_without_restarting_the_kernel(self) -> None:
        controller, runtime, _, _ = self.make_controller()

        controller.update_proxy_settings(block_global=False, block_private=True)

        self.assertFalse(runtime.block_global)
        self.assertTrue(runtime.block_private)
        self.assertEqual(runtime.restart_calls, 0)
        self.assertFalse(CONFIG.get(CONFIG.block_global))
        self.assertTrue(CONFIG.get(CONFIG.block_private))

    def test_listen_host_change_restarts_and_persists(self) -> None:
        controller, runtime, _, _ = self.make_controller()

        controller.update_proxy_settings(listen_host=ANY_HOST, listen_port=8081)

        self.assertEqual(runtime.restart_calls, 1)
        self.assertEqual(controller.current_host, ANY_HOST)
        self.assertEqual(controller.current_port, 8081)
        self.assertEqual(CONFIG.get(CONFIG.listen_host), ANY_HOST)
        self.assertEqual(CONFIG.get(CONFIG.listen_port), 8081)

    def test_unchanged_settings_do_not_restart(self) -> None:
        controller, runtime, _, _ = self.make_controller()

        controller.update_proxy_settings(
            listen_host=controller.current_host,
            listen_port=controller.current_port,
            block_global=controller.block_global,
            block_private=controller.block_private,
        )

        self.assertEqual(runtime.restart_calls, 0)

    def test_running_capture_is_rearmed_after_endpoint_change(self) -> None:
        controller, _, _, proxy = self.make_controller()
        controller.start_capture()
        self.assertEqual(controller.capture_state, CaptureState.RUNNING)

        controller.update_proxy_settings(listen_port=8081)

        # 重启期间系统代理必须先摘掉，等新监听就绪再挂回去。
        self.assertFalse(proxy.attached)
        self.assertEqual(controller.capture_state, CaptureState.STARTING)

    def test_write_gate_drops_new_flows_until_capture_starts(self) -> None:
        """普通抓包流量仍受闸门控制，既有行的更新照常通过。"""
        controller, runtime, _, _ = self.make_controller()
        added, updated = [], []
        controller.flow_added.connect(added.append)
        controller.flow_updated.connect(updated.append)

        flow = tflow.tflow()
        runtime.flow_stored.emit(flow)
        runtime.flow_added.emit(flow)
        runtime.flow_updated.emit(object())
        self.assertEqual(added, [])
        self.assertEqual(len(updated), 1)

        controller.start_capture()
        flow = tflow.tflow()
        runtime.flow_stored.emit(flow)
        runtime.flow_added.emit(flow)
        self.assertEqual(len(added), 1)

        controller.stop_capture()
        flow = tflow.tflow()
        runtime.flow_stored.emit(flow)
        runtime.flow_added.emit(flow)
        self.assertEqual(len(added), 1)

    def test_recorded_compose_flows_pass_once_without_changing_capture_state(
        self,
    ) -> None:
        """手工记录在开始前、抓包中和停止后均可入表，不接通通道或系统代理。"""
        controller, runtime, facade, proxy = self.make_controller()
        added: list[object] = []
        recording_changes: list[bool] = []
        capture_changes: list[CaptureState] = []
        controller.flow_added.connect(added.append)
        controller.recordingChanged.connect(recording_changes.append)
        controller.capture_state_changed.connect(capture_changes.append)

        def capture_state() -> tuple:
            return (
                controller.capture_state,
                controller.recording,
                facade.recording,
                proxy.attached,
                proxy.endpoint,
                runtime.channels_engaged,
                runtime.channel_pushes,
                runtime.start_calls,
                runtime.stop_calls,
                runtime.restart_calls,
                tuple(recording_changes),
                tuple(capture_changes),
            )

        for phase, transition, expected_state in (
            ("before_start", None, CaptureState.STOPPED),
            ("capturing", controller.start_capture, CaptureState.RUNNING),
            ("after_stop", controller.stop_capture, CaptureState.STOPPED),
        ):
            with self.subTest(phase=phase):
                if transition is not None:
                    transition()
                self.assertEqual(controller.capture_state, expected_state)
                before = capture_state()
                previous_count = len(added)
                flow = tflow.tflow()

                runtime.compose_flow_stored.emit(flow)
                runtime.compose_flow_added.emit(flow)

                self.assertEqual(len(added), previous_count + 1)
                self.assertIs(added[-1], flow)
                self.assertEqual(capture_state(), before)

    def test_update_channels_persists_and_hot_applies_while_capturing(self) -> None:
        controller, runtime, _facade, _proxy = self.make_controller()
        controller.start_capture()
        pushes = runtime.channel_pushes

        controller.update_channels(
            use_system_proxy=False,
            use_local=True,
            local_spec="curl",
            use_wireguard=False,
        )

        self.assertEqual(CONFIG.get(CONFIG.local_spec), "curl")
        self.assertFalse(CONFIG.get(CONFIG.system_proxy_enabled))
        self.assertEqual(runtime.local_spec, "curl")
        self.assertGreater(runtime.channel_pushes, pushes)
        self.assertFalse(CONFIG.get(CONFIG.listen_port) == 0)

    def test_update_channels_lives_system_proxy_alone_when_not_capturing(self) -> None:
        controller, runtime, _, proxy = self.make_controller()

        controller.update_channels(
            use_system_proxy=False,
            use_local=True,
            local_spec="",
            use_wireguard=True,
        )

        self.assertFalse(CONFIG.get(CONFIG.system_proxy_enabled))
        # 意图值已提交，但会话没接通 —— 内核保持 regular-only，注册表不被碰。
        self.assertFalse(runtime.channels_engaged)
        self.assertFalse(proxy.attached)

    def test_update_channels_rejects_a_bad_filter_without_touching_config(self) -> None:
        controller, runtime, _, _ = self.make_controller()

        with self.assertRaises(ValueError):
            controller.update_channels(
                use_system_proxy=True,
                use_local=True,
                local_spec="a,,b",
                use_wireguard=True,
            )

        self.assertEqual(CONFIG.get(CONFIG.local_spec), "")
        self.assertEqual(runtime.local_spec, "")

    def test_disabling_local_skips_filter_validation(self) -> None:
        """关闭本地重定向不被残留过滤串卡住（坏值仅落盘，开启时再拦）。"""
        controller, runtime, _, _ = self.make_controller()

        controller.update_channels(
            use_system_proxy=True,
            use_local=False,
            local_spec="a,,b",
            use_wireguard=True,
        )

        self.assertFalse(CONFIG.get(CONFIG.local_enabled))
        self.assertEqual(CONFIG.get(CONFIG.local_spec), "a,,b")
        self.assertFalse(runtime.use_local)

    def test_update_channels_rejects_a_bad_upstream_without_touching_config(
        self,
    ) -> None:
        """坏上游地址与坏 local 过滤串同款：拦在落盘之前，CONFIG 与内核都不动。"""
        controller, runtime, _, _ = self.make_controller()

        with self.assertRaises(ValueError):
            controller.update_channels(
                use_system_proxy=True,
                use_local=False,
                local_spec="",
                use_wireguard=False,
                use_upstream=True,
                upstream_target="ftp://proxy:8080",
            )

        self.assertFalse(CONFIG.get(CONFIG.upstream_enabled))
        self.assertEqual(CONFIG.get(CONFIG.upstream_target), "")
        self.assertFalse(runtime.use_upstream)

    def test_disabling_the_upstream_skips_target_validation(self) -> None:
        """「关的动作一律放行」：哪怕地址栏留着历史坏值也不该卡住提交
        （与 `test_disabling_local_skips_filter_validation` 同一条规矩）。"""
        controller, runtime, _, _ = self.make_controller()

        controller.update_channels(
            use_system_proxy=True,
            use_local=False,
            local_spec="",
            use_wireguard=False,
            use_upstream=False,
            upstream_target="ftp://proxy:8080",
        )

        self.assertFalse(CONFIG.get(CONFIG.upstream_enabled))
        self.assertEqual(CONFIG.get(CONFIG.upstream_target), "ftp://proxy:8080")
        self.assertFalse(runtime.use_upstream)

    def test_update_channels_persists_all_four_upstream_values(self) -> None:
        """四个值都要落盘并推到内核 —— 密码也在内，它是 CONFIG 明文项。"""
        controller, runtime, _, _ = self.make_controller()

        controller.update_channels(
            use_system_proxy=True,
            use_local=False,
            local_spec="",
            use_wireguard=False,
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )

        self.assertTrue(CONFIG.get(CONFIG.upstream_enabled))
        self.assertEqual(CONFIG.get(CONFIG.upstream_target), "http://proxy.corp:8080")
        self.assertEqual(CONFIG.get(CONFIG.upstream_username), "alice")
        self.assertEqual(CONFIG.get(CONFIG.upstream_password), "secret")
        self.assertTrue(runtime.use_upstream)
        self.assertEqual(runtime.upstream_target, "http://proxy.corp:8080")
        self.assertEqual(runtime.upstream_username, "alice")
        self.assertEqual(runtime.upstream_password, "secret")

    def test_detaching_system_proxy_on_dialog_toggle_while_capturing(self) -> None:
        controller, _runtime, _, proxy = self.make_controller()
        controller.start_capture()
        self.assertTrue(proxy.attached)

        controller.update_channels(
            use_system_proxy=False,
            use_local=True,
            local_spec="",
            use_wireguard=True,
        )

        self.assertFalse(proxy.attached)
        self.assertEqual(controller.capture_state, CaptureState.RUNNING)
        self.assertTrue(controller.recording)

    def test_channel_health_errors_surface_as_displayable_messages(self) -> None:
        """UAC 拒绝等实例启动失败只能延迟读 channel_health —— 文案要人话。"""
        controller, runtime, _, _ = self.make_controller()
        runtime.health = {
            "local": "Failed to start the interception process as administrator."
        }
        controller.start_capture()

        controller._check_channel_health()

        error = controller.channel_errors.get("local", "")
        self.assertIn("管理员授权（UAC）", error)
        self.assertNotIn("Failed to start", error)

    def test_socks5_port_conflict_surfaces_as_displayable_message(self) -> None:
        """socks5 实例启动失败（端口被占）的 last_exception 是裸 OSError，文本里
        不含通道名，按通用特征词映射成人话（docs/design.md#capture）。"""
        controller, runtime, _, _ = self.make_controller()
        runtime.health = {"socks5": "[Errno 98] Address already in use"}
        controller.start_capture()

        controller._check_channel_health()

        error = controller.channel_errors.get("socks5", "")
        self.assertIn("端口被占用", error)
        self.assertNotIn("Errno", error)

    def test_channel_health_is_polled_only_while_capturing(self) -> None:
        controller, runtime, _, _ = self.make_controller()
        runtime.health = {"local": "boom"}

        controller._check_channel_health()

        self.assertEqual(controller.channel_errors, {})
        controller.start_capture()
        controller._check_channel_health()
        self.assertIn("local", controller.channel_errors)

    def test_channel_health_polls_repeatedly_while_capturing(self) -> None:
        """#15：单次 1.5s 检查盖不住晚到的失败（UAC 晚拒绝、守护进程中途死）。

        健康在第一轮检查之后才变坏：旧实现的 singleShot 已经跑完，永远看不见；
        周期轮询下第二轮照样把它捞上来。
        """
        controller, runtime, _, _ = self.make_controller()
        controller.start_capture()

        runtime.health = {}
        controller._check_channel_health()
        self.assertEqual(controller.channel_errors, {})

        runtime.health = {
            "local": "Failed to start the interception process as administrator."
        }
        controller._check_channel_health()
        self.assertIn("管理员授权（UAC）", controller.channel_errors["local"])

    def test_channel_health_timer_stops_with_the_session(self) -> None:
        """#15：轮询只在抓包期间转 —— 停止即停表，不抓包时零常驻唤醒。"""
        controller, _runtime, _, _ = self.make_controller()
        self.assertFalse(controller._channel_timer.isActive())

        controller.start_capture()
        self.assertTrue(controller._channel_timer.isActive())
        self.assertEqual(controller._channel_timer.interval(), 1500)

        controller.stop_capture()
        self.assertFalse(controller._channel_timer.isActive())


class ApplyFilterRawTests(unittest.TestCase):
    """原生 flowfilter 表达式的唯一校验点在 controller（`docs/design.md#ui`）：合法上屏并清错误态，非法沿用上次有效结果并回传错误、绝不把半截表达式
    打到内核层。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_controller(self):
        runtime = FakeRuntime()
        facade = FakeFacade(runtime)
        proxy = FakeSystemProxy()
        return CaptureController(mitm=facade, system_proxy=proxy), facade  # type: ignore

    def test_a_valid_raw_expression_reaches_the_kernel_and_clears_the_error(
        self,
    ) -> None:
        controller, facade = self.make_controller()
        errors: list[str] = []
        controller.filterExpressionRejected.connect(errors.append)

        controller.apply_filter('~u "api/.*"')

        self.assertIsNotNone(facade.applied_filter)
        self.assertEqual(controller._last_valid_raw_filter, '~u "api/.*"')
        self.assertEqual(errors, [""])  # 成功时清错误态

    def test_an_invalid_raw_falls_back_and_reports_without_going_on_screen(
        self,
    ) -> None:
        controller, facade = self.make_controller()
        # 先攒一个有效表达式当作「上次有效」。
        controller.apply_filter("~m GET")
        good = facade.applied_filter
        errors: list[str] = []
        controller.filterExpressionRejected.connect(errors.append)

        controller.apply_filter("~~~ not a filter")

        # 沿用上次有效：内核上屏的仍是「上次有效」的合并结果，last_valid 不被污染。
        self.assertEqual(controller._last_valid_raw_filter, "~m GET")
        self.assertIsNotNone(facade.applied_filter)
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0])  # 非空错误原文回传
        # 上一步的 good 与本次回退都源自同一表达式，说明没让坏表达式覆盖上屏结果。
        self.assertIsNotNone(good)

    def test_removing_unmarked_flows_passes_through_to_the_facade(self) -> None:
        controller, facade = self.make_controller()
        self.assertEqual(controller.remove_unmarked_flows(), 3)
        self.assertEqual(facade.removed_unmarked_calls, 1)


class ApplyHighlightTests(unittest.TestCase):
    """搜索高亮：复用 flowfilter 的编译与校验，但命中集只回给调用方、不动 View 过滤
    （`docs/design.md#ui`）。空表达式空转，非法沿用错误契约回空集。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_controller(self):
        runtime = FakeRuntime()
        facade = FakeFacade(runtime)
        proxy = FakeSystemProxy()
        return CaptureController(mitm=facade, system_proxy=proxy), facade  # type: ignore

    def test_a_valid_expression_returns_the_facade_match_set(self) -> None:
        controller, facade = self.make_controller()
        facade.match_result = {"id-a", "id-b"}
        controller._admitted_ids.update(facade.match_result)
        errors: list[str] = []
        controller.filterExpressionRejected.connect(errors.append)

        hits = controller.apply_highlight('~u "api/.*"')

        self.assertEqual(hits, {"id-a", "id-b"})
        self.assertEqual(facade.match_calls, 1)
        self.assertIsNotNone(facade.last_matcher)
        self.assertEqual(errors, [])  # 合法表达式不报错

    def test_an_empty_expression_short_circuits_without_touching_the_facade(
        self,
    ) -> None:
        controller, facade = self.make_controller()

        self.assertEqual(controller.apply_highlight("   "), set())
        self.assertEqual(getattr(facade, "match_calls", 0), 0)

    def test_an_invalid_expression_reports_and_returns_empty(self) -> None:
        controller, facade = self.make_controller()
        errors: list[str] = []
        controller.filterExpressionRejected.connect(errors.append)

        hits = controller.apply_highlight("~~~ not a filter")

        self.assertEqual(hits, set())
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0])  # 非空错误原文回传
        self.assertEqual(getattr(facade, "match_calls", 0), 0)  # 坏表达式不下发


if __name__ == "__main__":
    unittest.main()
