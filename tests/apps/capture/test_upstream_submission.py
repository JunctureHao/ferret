"""上游代理提交路径的前置校验（.plans/upstream-mode.md §7 第 12 条后半）。

`CapturesInterface.__show_proxy_port_dialog` 里有三道上游相关的闸门，语义各不
相同，必须分开钉：

===============  ========  ====================================================
情形             动作      理由
===============  ========  ====================================================
勾了没填地址     **拦**    空目标换掉首槽只会让系统代理整条死掉
地址指回自己     **拦**    原生兜底写的是 "Request destination unknown"，且要等
                           到有流量才出现 —— 提交时就该说清楚
凭证 + 反代同开  **警告**  原生 UpstreamAuth 分不开两种模式，用户知情后仍可能
                           就是要这么用，所以可继续
===============  ========  ====================================================

这条路径原来没有任何测试。它不需要真的建起 `CapturesInterface`（那要拖一整套
控制器与表格模型），方法体只碰 `self.controller` / `self.tr()` / `self.window()`
/ `self._ui_state` / `self._refresh_command_bar`，用一个替身承载即可；对话框与
`show_warning` 是模块级名字，按名替换。
"""

import os
import unittest
from typing import ClassVar
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QWidget

from ferret.apps.capture import views
from ferret.core.network import LOOPBACK_HOST

# 名字改写后的真实方法名：私有槽（双下划线）在类体里被改写成这个。
SHOW_DIALOG = views.CapturesInterface._CapturesInterface__show_proxy_port_dialog  # ty: ignore[unresolved-attribute]


class FakeController:
    """只实现 `__show_proxy_port_dialog` 会读到的那一小片控制器接口。"""

    def __init__(self) -> None:
        self.current_port = 8080
        self.is_capturing = False
        self.current_host = LOOPBACK_HOST
        self.block_global = True
        self.block_private = False
        self.use_local = True
        self.local_spec = ""
        self.use_wireguard = True
        self.use_reverse = False
        self.reverse_target = ""
        self.reverse_port = 8081
        self.use_upstream = False
        self.upstream_target = ""
        self.upstream_username = ""
        self.upstream_password = ""
        self.local_endpoint = "127.0.0.1:8080"
        self.is_lan_exposed = False
        self.channel_updates: list[dict] = []
        self.proxy_updates: list[dict] = []
        # upstream_targets_self 的替身行为由用例按需改写。
        self.self_loop = False
        self.target_error: str | None = None

    def lan_address(self) -> str:
        return "192.168.1.9"

    def system_proxy_enabled(self) -> bool:
        return True

    def wireguard_client_config(self) -> str:
        return "[Interface]"

    def upstream_targets_self(self, target, *, listen_host, listen_port) -> bool:
        if self.target_error is not None:
            raise ValueError(self.target_error)
        return self.self_loop

    def update_channels(self, **kwargs) -> None:
        self.channel_updates.append(kwargs)

    def update_proxy_settings(self, **kwargs) -> None:
        self.proxy_updates.append(kwargs)


class FakeDialog:
    """`ProxyPortDialog` 的替身：`exec()` 恒真，getter 回预设值。"""

    instances: ClassVar[list["FakeDialog"]] = []
    values: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        FakeDialog.instances.append(self)

    def exec(self) -> bool:
        return True

    def _get(self, name, default):
        return FakeDialog.values.get(name, default)

    def get_port(self) -> int:
        return self._get("port", 8080)

    def get_listen_host(self) -> str:
        return self._get("listen_host", LOOPBACK_HOST)

    def get_block_global(self) -> bool:
        return True

    def get_block_private(self) -> bool:
        return False

    def get_use_system_proxy(self) -> bool:
        return True

    def get_use_local(self) -> bool:
        return False

    def get_local_spec(self) -> str:
        return ""

    def get_use_wireguard(self) -> bool:
        return False

    def get_use_reverse(self) -> bool:
        return self._get("use_reverse", False)

    def get_reverse_target(self) -> str:
        return self._get("reverse_target", "")

    def get_reverse_port(self) -> int:
        return self._get("reverse_port", 8081)

    def get_use_upstream(self) -> bool:
        return self._get("use_upstream", False)

    def get_upstream_target(self) -> str:
        return self._get("upstream_target", "")

    def get_upstream_username(self) -> str:
        return self._get("upstream_username", "")

    def get_upstream_password(self) -> str:
        return self._get("upstream_password", "")


class Host(QWidget):
    """承载那个方法的最小替身：它只碰这几样东西。"""

    def __init__(self, controller: FakeController) -> None:
        super().__init__()
        self.controller = controller
        self._ui_state = mock.MagicMock()
        self.refreshes = 0

    def _refresh_command_bar(self) -> None:
        self.refreshes += 1


class UpstreamSubmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        FakeDialog.instances = []
        FakeDialog.values = {}
        self.controller = FakeController()
        self.host = Host(self.controller)
        self.addCleanup(self.host.deleteLater)
        self.warnings: list[tuple] = []

        patches = [
            mock.patch.object(views, "ProxyPortDialog", FakeDialog),
            mock.patch.object(
                views,
                "show_warning",
                lambda title, body, parent=None: self.warnings.append((title, body)),
            ),
            # replace(_ui_state, ...) 要真 dataclass，这里的替身不是，绕开它。
            mock.patch.object(views, "replace", lambda obj, **kw: obj),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def submit(self, **values) -> None:
        FakeDialog.values = values
        SHOW_DIALOG(self.host)

    def test_a_plain_submission_passes_all_four_values_through(self) -> None:
        self.submit(
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
        )
        self.assertEqual(self.warnings, [])
        self.assertEqual(len(self.controller.channel_updates), 1)
        sent = self.controller.channel_updates[0]
        self.assertTrue(sent["use_upstream"])
        self.assertEqual(sent["upstream_target"], "http://proxy.corp:8080")
        self.assertEqual(sent["upstream_username"], "alice")
        self.assertEqual(sent["upstream_password"], "secret")

    def test_the_four_values_are_backfilled_into_the_dialog(self) -> None:
        self.controller.use_upstream = True
        self.controller.upstream_target = "http://proxy.corp:8080"
        self.controller.upstream_username = "alice"
        self.controller.upstream_password = "secret"

        self.submit(use_upstream=True, upstream_target="http://proxy.corp:8080")

        opened = FakeDialog.instances[0].kwargs
        self.assertTrue(opened["use_upstream"])
        self.assertEqual(opened["upstream_target"], "http://proxy.corp:8080")
        self.assertEqual(opened["upstream_username"], "alice")
        self.assertEqual(opened["upstream_password"], "secret")

    def test_checked_without_an_address_is_blocked(self) -> None:
        """空目标是硬拦：换掉首槽会让系统代理这条主通道整条死掉。"""
        self.submit(use_upstream=True, upstream_target="")

        self.assertEqual(len(self.warnings), 1)
        self.assertIn("没填地址", self.warnings[0][1])
        self.assertEqual(self.controller.channel_updates, [])

    def test_a_self_loop_address_is_blocked(self) -> None:
        """自环硬拦，且必须在提交前 —— 原生兜底要等到有流量才报，
        报的还是 "Request destination unknown" 这种看不懂的话。"""
        self.controller.self_loop = True
        self.submit(use_upstream=True, upstream_target="http://127.0.0.1:8080")

        self.assertEqual(len(self.warnings), 1)
        self.assertIn("指回 ferret 自己的监听口", self.warnings[0][1])
        self.assertEqual(self.controller.channel_updates, [])

    def test_the_self_loop_check_uses_the_pending_listen_values(self) -> None:
        """判据要用**待提交**的监听地址端口，不是运行中的那一份 —— 用户可能
        正好在这次提交里把端口改成与上游相同。"""
        seen: dict = {}

        def spy(target, *, listen_host, listen_port):
            seen.update(target=target, listen_host=listen_host, listen_port=listen_port)
            return False

        self.controller.upstream_targets_self = spy  # ty: ignore[invalid-assignment]
        self.submit(
            use_upstream=True,
            upstream_target="http://proxy:9999",
            port=9000,
            listen_host="0.0.0.0",
        )

        self.assertEqual(seen["listen_port"], 9000)
        self.assertEqual(seen["listen_host"], "0.0.0.0")

    def test_an_unparsable_address_is_reported_not_raised(self) -> None:
        """坏地址在这里就被解析器拒了，走警告而不是异常穿出去。"""
        self.controller.target_error = "抓包通道无效：bad"
        self.submit(use_upstream=True, upstream_target="ftp://proxy:8080")

        self.assertEqual(len(self.warnings), 1)
        self.assertIn("抓包通道无效", self.warnings[0][1])
        self.assertEqual(self.controller.channel_updates, [])

    def test_credentials_plus_reverse_warns_but_still_submits(self) -> None:
        """串台只警告不拦：原生分不开，用户知情后仍可能就是要这么用。"""
        self.submit(
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            upstream_username="alice",
            upstream_password="secret",
            use_reverse=True,
            reverse_target="https://example.com",
        )

        self.assertEqual(len(self.warnings), 1)
        self.assertIn("反代目标", self.warnings[0][1])
        # 警告归警告，提交照常发生 —— 这正是它与前两道的区别。
        self.assertEqual(len(self.controller.channel_updates), 1)
        self.assertTrue(self.controller.channel_updates[0]["use_upstream"])

    def test_reverse_without_upstream_credentials_does_not_warn(self) -> None:
        """没填用户名就没有凭证可串，不该无故惊扰用户。"""
        self.submit(
            use_upstream=True,
            upstream_target="http://proxy.corp:8080",
            use_reverse=True,
            reverse_target="https://example.com",
        )

        self.assertEqual(self.warnings, [])
        self.assertEqual(len(self.controller.channel_updates), 1)

    def test_turning_the_upstream_off_skips_every_check(self) -> None:
        """关的动作一律放行：哪怕地址栏里留着历史坏值，也不该卡住提交。"""
        self.controller.target_error = "抓包通道无效：bad"
        self.submit(use_upstream=False, upstream_target="ftp://proxy:8080")

        self.assertEqual(self.warnings, [])
        self.assertEqual(len(self.controller.channel_updates), 1)
        self.assertFalse(self.controller.channel_updates[0]["use_upstream"])


if __name__ == "__main__":
    unittest.main()
