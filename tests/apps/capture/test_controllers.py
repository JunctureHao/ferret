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
        self.start_calls = 0
        self.stop_calls = 0
        self.restart_calls = 0

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

    def set_block_options(self, *, block_global=None, block_private=None) -> None:
        if block_global is not None:
            self.runtime.block_global = block_global
        if block_private is not None:
            self.runtime.block_private = block_private

    def start_capture_recording(self):
        self.recording = True

    def stop_capture_recording(self):
        self.recording = False


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

    def setUp(self) -> None:
        # 控制器提交设置时会写 CONFIG —— 绝不能落到用户真实的 config.json 上。
        self._config_dir = tempfile.TemporaryDirectory()
        self._original_file = CONFIG.file
        CONFIG.file = Path(self._config_dir.name) / "config.json"
        self.addCleanup(self._restore_config)

    def _restore_config(self) -> None:
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

        controller.stop_capture()
        self.assertEqual(controller.capture_state, CaptureState.STOPPED)
        self.assertFalse(proxy.attached)
        self.assertFalse(facade.recording)
        self.assertTrue(runtime.is_running)
        self.assertEqual(runtime.stop_calls, 0)
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


if __name__ == "__main__":
    unittest.main()
