import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, MitmRuntimeState
from ferret.core.mitm.modes import REVERSE_DEFAULT_PORT
from ferret.core.network import ANY_HOST, LOOPBACK_HOST

from ._qt import start_runtime, wait_for_signal


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MitmRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_runtime_starts_and_stops_one_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        start_runtime(runtime)
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)
        self.assertTrue(runtime.stop())
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)

    def test_occupied_port_fails_without_entering_running(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            runtime = MitmRuntime(listen_port=blocker.getsockname()[1])
            runtime.start()

            values = wait_for_signal(runtime.failed)
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


class ReverseChannelStateTests(unittest.TestCase):
    """reverse 三意图值与 engaged 闸门的内建状态机（不跑真内核）。

    让路判据与 channel_health reverse 键是反向代理通道的两个独立验收面：
    前者纯函数式可钉，后者需要真内核拉起实例、复现 spec 启动失败探针。
    让路与 engaged 闸门在这一组跑；channel_health reverse 键在
    ``test_master_addon_assembly``（test_facade）里跟 UpdateAltSvc 挂载点
    一同验。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_intents_have_reverse_disabled_and_empty_target(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.assertFalse(runtime.use_reverse)
        self.assertEqual(runtime.reverse_target, "")
        self.assertEqual(runtime.reverse_port, REVERSE_DEFAULT_PORT)

    def test_mode_specs_omit_reverse_when_disengaged(self) -> None:
        """未接通时 reverse 不进 mode 列表：与 local/wireguard 同构。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime._mode_specs(), ["regular"])

    def test_mode_specs_include_reverse_when_engaged(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True
        specs = runtime._mode_specs()
        self.assertEqual(specs[0], "regular")
        self.assertTrue(specs[1].startswith("reverse:"))
        self.assertIn("example.com", specs[1])

    def test_apply_channels_rejects_bad_target_and_rolls_back(self) -> None:
        """坏 target 在落盘前被原生解析器拒，意图值整体回滚到 previous。

        必须在 ``channels_engaged=True`` 下验证：未接通时 reverse spec 根本
        不进 ``_mode_specs()``（与 local/wireguard 同构），validate 当然过——
        这与 plan §4 的「不接通的意图值不算提交到内核」语义一致。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True
        previous = (
            runtime.use_local,
            runtime.local_spec,
            runtime.use_wireguard,
            runtime.use_reverse,
            runtime.reverse_target,
            runtime.reverse_port,
        )
        with self.assertRaises(ValueError):
            runtime.apply_channels(
                use_reverse=True, reverse_target="not a url", reverse_port=8081
            )
        # 元组扩容后的不变量：所有六项必须原样回滚。
        self.assertEqual(
            (
                runtime.use_local,
                runtime.local_spec,
                runtime.use_wireguard,
                runtime.use_reverse,
                runtime.reverse_target,
                runtime.reverse_port,
            ),
            previous,
        )


class EffectiveBlockPrivateTests(unittest.TestCase):
    """``_effective_block_private`` 让路判据（plan §4 定式）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _runtime(
        self,
        *,
        block_private: bool = True,
        listen_host: str = LOOPBACK_HOST,
        use_wireguard: bool = False,
        use_reverse: bool = False,
        channels_engaged: bool = True,
    ) -> MitmRuntime:
        runtime = MitmRuntime(
            listen_port=free_port(),
            block_private=block_private,
            use_wireguard=use_wireguard,
            use_reverse=use_reverse,
        )
        runtime.listen_host = listen_host
        runtime.channels_engaged = channels_engaged
        return runtime

    def test_no_yield_when_block_private_disabled(self) -> None:
        runtime = self._runtime(
            block_private=False, listen_host=ANY_HOST, use_reverse=True
        )
        self.assertFalse(runtime._effective_block_private())

    def test_no_yield_when_disengaged(self) -> None:
        """engaged 闸门：未接通时让路不发生（空操作、保留原值）。"""
        runtime = self._runtime(
            listen_host=ANY_HOST, use_reverse=True, channels_engaged=False
        )
        self.assertTrue(runtime._effective_block_private())

    def test_yield_only_when_reverse_enabled_and_bound_to_any(self) -> None:
        """bind 环回时 reverse 不让路（局域网流量到不了环回 socket），
        bind ANY_HOST 时让路生效（与 wireguard 让路条件同式）。"""
        loopback = self._runtime(listen_host=LOOPBACK_HOST, use_reverse=True)
        any_host = self._runtime(listen_host=ANY_HOST, use_reverse=True)
        self.assertTrue(loopback._effective_block_private())
        self.assertFalse(any_host._effective_block_private())

    def test_wireguard_still_yields_regardless_of_listen_host(self) -> None:
        """wireguard 客户端来自固定 10.0.0.x 段，让路与绑定地址无关
        —— 这是 wireguard 先例，reverse 的让路条件按「绑定非环回」是新增的。"""
        runtime = self._runtime(
            listen_host=LOOPBACK_HOST, use_wireguard=True
        )
        self.assertFalse(runtime._effective_block_private())

    def test_neither_yields_keeps_user_value(self) -> None:
        """两条让路都不成立时原值下发；用户偏好保留。"""
        runtime = self._runtime(listen_host=LOOPBACK_HOST)
        self.assertTrue(runtime._effective_block_private())


if __name__ == "__main__":
    unittest.main()
