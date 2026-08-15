import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ferret.apps.capture.controllers import CaptureController, CaptureState
from ferret.core.mitm import MitmRuntimeState, View


class FakeRuntime(QObject):
    flow_added = Signal(object)
    flow_updated = Signal(object)
    flow_removed = Signal(object, int)
    view_refreshed = Signal()
    ready = Signal(object)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.view = View()
        self.state = MitmRuntimeState.RUNNING
        self.is_running = True
        self.listen_host = "127.0.0.1"
        self.listen_port = 8080
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> bool:
        self.stop_calls += 1
        self.is_running = False
        self.state = MitmRuntimeState.STOPPED
        return True

    def restart(self, *, listen_port=None) -> None:
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
    def listen_port(self):
        return self.runtime.listen_port

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

    def make_controller(self, *, fail_attach: bool = False):
        runtime = FakeRuntime()
        facade = FakeFacade(runtime)
        proxy = FakeSystemProxy(fail_attach=fail_attach)
        controller = CaptureController(mitm=facade, system_proxy=proxy)
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


if __name__ == "__main__":
    unittest.main()
