import socket
import unittest

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

from ferret.core.mitm import MitmRuntime, MitmRuntimeState


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MitmRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def wait_for_signal(self, signal, timeout_ms: int = 5000):
        loop = QEventLoop()
        values = []

        def receive(*args):
            values.append(args)
            loop.quit()

        signal.connect(receive)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        signal.disconnect(receive)
        return values

    def test_runtime_starts_and_stops_one_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        runtime.start()

        self.assertTrue(self.wait_for_signal(runtime.ready))
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)
        self.assertTrue(runtime.stop())
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)

    def test_occupied_port_fails_without_entering_running(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            runtime = MitmRuntime(listen_port=blocker.getsockname()[1])
            runtime.start()

            values = self.wait_for_signal(runtime.failed)
            self.assertTrue(values)
            self.assertIn("已被占用", values[0][0])
            self.assertEqual(runtime.state, MitmRuntimeState.FAILED)
            self.assertTrue(runtime.stop())

    def test_immediate_stop_does_not_leave_background_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        runtime.start()

        self.assertTrue(runtime.stop())
        QCoreApplication.processEvents()
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)
        self.assertIsNone(runtime.master)


if __name__ == "__main__":
    unittest.main()
