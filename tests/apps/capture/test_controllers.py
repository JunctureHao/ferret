import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.core.mitm import MitmRuntimeState, View
from ferret.core.network import ANY_HOST, LOOPBACK_HOST
from ferret.core.settings import CONFIG


class FakeRuntime(QObject):
    flow_added = Signal(object)
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
        self, *, use_local=None, local_spec=None, use_wireguard=None
    ) -> None:
        if use_local is not None:
            self.use_local = use_local
        if local_spec is not None:
            self.local_spec = local_spec
        if use_wireguard is not None:
            self.use_wireguard = use_wireguard
        self.channel_pushes += 1

    def channel_health(self) -> dict:
        return dict(self.health)


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
    def use_local(self):
        return self.runtime.use_local

    @property
    def local_spec(self):
        return self.runtime.local_spec

    @property
    def use_wireguard(self):
        return self.runtime.use_wireguard

    def set_block_options(self, *, block_global=None, block_private=None) -> None:
        if block_global is not None:
            self.runtime.block_global = block_global
        if block_private is not None:
            self.runtime.block_private = block_private

    def start_capture_recording(self):
        self.recording = True

    def stop_capture_recording(self):
        self.recording = False

    def engage_channels(self) -> None:
        self.runtime.set_channels_engaged(True)

    def disengage_channels(self) -> None:
        self.runtime.set_channels_engaged(False)

    def set_channels(
        self, *, use_local=None, local_spec=None, use_wireguard=None
    ) -> None:
        self.runtime.apply_channels(
            use_local=use_local, local_spec=local_spec, use_wireguard=use_wireguard
        )

    def validate_local_spec(self, local_spec: str) -> None:
        if ",," in local_spec or local_spec.strip() == "!":
            raise ValueError("invalid intercept spec")

    def channel_health(self) -> dict:
        return self.runtime.channel_health()

    def wireguard_client_config(self) -> str:
        return "[Interface]"


class FakeSystemProxy:
    def __init__(self, *, fail_attach: bool = False) -> None:
        self.fail_attach = fail_attach
        self.attached = False
        self.endpoint = None

    def attach(self, host: str, port: int) -> None:
        if self.fail_attach:
            raise RuntimeError("address already in use")
        self.attached = True
        self.endpoint = (host, port)

    def detach(self) -> bool:
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

    def make_controller(self, *, fail_attach: bool = False):
        runtime = FakeRuntime()
        facade = FakeFacade(runtime)
        proxy = FakeSystemProxy(fail_attach=fail_attach)
        controller = CaptureController(mitm=facade, system_proxy=proxy)  # type: ignore
        return controller, runtime, facade, proxy

    def test_start_ready_stop_state_sequence(self) -> None:
        controller, runtime, facade, proxy = self.make_controller()
        states = []
        legacy = []
        controller.capture_state_changed.connect(states.append)
        controller.captureStateChanged.connect(legacy.append)

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
        self.assertEqual(legacy, [True, False])

    def test_attach_failure_exposes_failed_state_and_rolls_back_recording(self) -> None:
        controller, _, facade, proxy = self.make_controller(fail_attach=True)

        controller.start_capture()

        self.assertEqual(controller.capture_state, CaptureState.FAILED)
        self.assertEqual(controller.last_error, "address already in use")
        self.assertFalse(proxy.attached)
        self.assertFalse(facade.recording)

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
        """闸门语义：未抓包时新 flow 不进表，既有行的更新照常通过。"""
        controller, runtime, _, _ = self.make_controller()
        added, updated = [], []
        controller.flow_added.connect(added.append)
        controller.flow_updated.connect(updated.append)

        runtime.flow_added.emit(object())
        runtime.flow_updated.emit(object())
        self.assertEqual(added, [])
        self.assertEqual(len(updated), 1)

        controller.start_capture()
        runtime.flow_added.emit(object())
        self.assertEqual(len(added), 1)

        controller.stop_capture()
        runtime.flow_added.emit(object())
        self.assertEqual(len(added), 1)

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

    def test_channel_health_is_polled_only_while_capturing(self) -> None:
        controller, runtime, _, _ = self.make_controller()
        runtime.health = {"local": "boom"}

        controller._check_channel_health()

        self.assertEqual(controller.channel_errors, {})
        controller.start_capture()
        controller._check_channel_health()
        self.assertIn("local", controller.channel_errors)


if __name__ == "__main__":
    unittest.main()
