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
        previous = runtime._channel_intents()
        with self.assertRaises(ValueError):
            runtime.apply_channels(
                use_reverse=True, reverse_target="not a url", reverse_port=8081
            )
        # 快照断言一次，新增意图值只需改 _CHANNEL_INTENTS 一处（逐条展开的写法
        # 在字段变多后必然漏掉某一条，这正是抽出这对方法的动机）。
        self.assertEqual(runtime._channel_intents(), previous)


class UpstreamIntentTests(unittest.TestCase):
    """上游代理意图值、首槽替换与凭证拼装（.plans/upstream-mode.md §4.4）。

    上游**不是**第五条通道：它换掉 ``mode[0]``，凭证走另一条正交的
    ``upstream_auth`` 选项。这一组只验纯状态机，不跑真内核。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_intents_have_upstream_disabled_and_empty_fields(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.assertFalse(runtime.use_upstream)
        self.assertEqual(runtime.upstream_target, "")
        self.assertEqual(runtime.upstream_username, "")
        self.assertEqual(runtime.upstream_password, "")

    def test_mode_specs_omit_upstream_when_disengaged(self) -> None:
        """未接通时首槽仍是 regular：与四条通道同一道 engaged 闸门。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        self.assertFalse(runtime.channels_engaged)
        self.assertEqual(runtime._mode_specs(), ["regular"])

    def test_mode_specs_replace_the_head_slot_when_engaged(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            use_wireguard=True,
        )
        runtime.channels_engaged = True
        specs = runtime._mode_specs()
        self.assertEqual(specs[0], "upstream:http://proxy.corp:8080")
        # 替换不是追加：regular 不得同时在场（都回退全局 listen_port，
        # 同时下发必被 proxyserver 的地址查重拒）。
        self.assertNotIn("regular", specs)

    def test_upstream_auth_is_none_when_username_is_empty(self) -> None:
        """空用户名必须回 ``None``，**不能**是空串。

        ``parse_upstream_auth`` 的正则是 ``.+:`` —— 实测 ``upstream_auth=""``
        抛 ``OptionsError: Invalid upstream auth specification:``，而这条路径在
        「用户只填地址、不填凭证」时天天走。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_is_none_when_upstream_is_off(self) -> None:
        """上游关掉时凭证一并失效。

        不只是「省一次赋值」：``UpstreamAuth.requestheaders``
        （``upstream_auth.py:56-58``）在 auth 非空时会给 **reverse 通道**的请求
        补 ``Authorization`` 头。关掉上游还留着凭证，等于把企业代理密码持续发给
        反代目标。这一条是那个串台面的主闸门。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=False,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_is_none_when_the_target_is_empty(self) -> None:
        """勾了上游但没填地址 → 凭证也必须失效。

        判据要与「首槽位是否真的换成了 upstream」对齐：``capture_mode_specs``
        见到空目标会保持 ``regular``（空 spec 推进内核只会让系统代理整条死掉），
        上游因此并未生效。若此处仍回凭证，配上 reverse 就是把企业代理密码白送给
        反代目标 —— 与 `test_upstream_auth_is_none_when_upstream_is_off` 钉的是
        同一个串台面，只是入口换成了「开着但没生效」。

        对话框拦得住「勾了没填」，但手改配置文件、或直接调
        ``apply_channels(use_upstream=True)`` 不带目标都绕得过去。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="   ",
            upstream_username="alice",
            upstream_password="secret",
            use_reverse=True,
            reverse_target="https://example.com",
        )
        runtime.channels_engaged = True

        # 前提：首槽位确实还是 regular，上游没进 mode。
        self.assertEqual(runtime._mode_specs()[0], "regular")
        self.assertIsNone(runtime._upstream_auth())

    def test_upstream_auth_joins_user_and_password(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:secret")

    def test_password_containing_colon_is_passed_through(self) -> None:
        """密码含冒号原样拼：服务端按**首个**冒号切，所以只有用户名不能含冒号
        —— 这正是 UI 分两个输入框收、而不是让用户自己拼 ``user:pass`` 的理由。
        """
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="a:b:c",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:a:b:c")

    def test_empty_password_still_emits_a_credential(self) -> None:
        """用户名非空、密码为空是合法的（``.+:`` 只要求冒号前非空）。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
        )
        self.assertEqual(runtime._upstream_auth(), "alice:")

    def test_apply_channels_rejects_bad_upstream_and_rolls_back(self) -> None:
        """坏上游地址在下发前被原生解析器拒，十项意图值整体回滚。"""
        runtime = MitmRuntime(
            listen_port=free_port(),
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
        )
        runtime.channels_engaged = True
        previous = runtime._channel_intents()
        with self.assertRaises(ValueError):
            runtime.apply_channels(
                use_upstream=True,
                upstream_target="ftp://proxy.corp:8080",
                upstream_username="alice",
                upstream_password="secret",
            )
        self.assertEqual(runtime._channel_intents(), previous)
        # 凭证也在回滚范围里：失败的提交不该把用户名密码留在内核里。
        self.assertEqual(runtime.upstream_username, "")
        self.assertEqual(runtime.upstream_password, "")


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
        runtime = self._runtime(listen_host=LOOPBACK_HOST, use_wireguard=True)
        self.assertFalse(runtime._effective_block_private())

    def test_neither_yields_keeps_user_value(self) -> None:
        """两条让路都不成立时原值下发；用户偏好保留。"""
        runtime = self._runtime(listen_host=LOOPBACK_HOST)
        self.assertTrue(runtime._effective_block_private())

    def test_upstream_does_not_change_the_yield_verdict(self) -> None:
        """上游代理**不让路**，理由干净：Block 管的是客户端**来源** IP，上游
        只换出口，监听地址与来源判定一字未动（计划 §6）。开上游时让路判据
        必须与不开时逐项相同。
        """
        for listen_host in (LOOPBACK_HOST, ANY_HOST):
            for use_reverse in (False, True):
                with self.subTest(listen_host=listen_host, use_reverse=use_reverse):
                    off = self._runtime(
                        listen_host=listen_host, use_reverse=use_reverse
                    )
                    on = self._runtime(listen_host=listen_host, use_reverse=use_reverse)
                    on.use_upstream = True
                    on.upstream_target = "http://proxy.corp:8080"
                    self.assertEqual(
                        on._effective_block_private(),
                        off._effective_block_private(),
                    )


if __name__ == "__main__":
    unittest.main()
