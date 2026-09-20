"""DNS 解析选项的内核侧验收（.plans/dns-options.md §7）。

分三组，按「越靠近原生越不需要内核」排：

* `DnsOptionsAddonTests` —— 原生 ``DnsResolver`` 的挂载、选项注册与**无校验器**
  机理。坏 IP 串静默放行那条用例是守卫必须自建的根据：种子侧
  `_apply_dns_options` 的回退逻辑全靠它成立，上游加校验器时这条会先红。
* `DnsOptionsGateTests` —— 纯函数：`dns_option_updates` 翻译矩阵 +
  `_validate_dns_name_servers` 校验矩阵（界面提交与内核种子共用的唯一闸门）。
* `DnsOptionsKernelTests` —— 跑真内核：种子（含坏值回退）、热更（None 不
  改动 / ``[]`` 清空）、失败回滚、configure 清缓存（「新查询即刻生效」）。
"""

import asyncio
import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime
from ferret.core.mitm.bindings import DnsResolver, Options, StripDnsHttpsRecords
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.runtime import (
    _validate_dns_name_servers,
    dns_option_updates,
)

from ._qt import start_runtime


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DnsOptionsAddonTests(unittest.TestCase):
    """原生 ``DnsResolver`` 在 ferret 链上的位置，与两个选项的注册时机。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def _addon(self) -> DnsResolver:
        addon = self.master.addons.get("dnsresolver")
        assert isinstance(addon, DnsResolver)
        return addon

    def test_the_addon_is_mounted(self) -> None:
        self.assertIn(self._addon(), self.master.addons.chain)

    def test_strip_dns_https_records_is_mounted(self) -> None:
        """配套件（方案 §3.5）：剥 DNS 应答里的 HTTPS / SVCB 记录，防 ECH /
        IP-hint 让流量绕开指向目标 —— 与本功能互补，无动作但要钉住别被拆。"""
        self.assertIsInstance(
            self.master.addons.get("stripdnshttpsrecords"), StripDnsHttpsRecords
        )

    def test_the_options_exist_only_after_the_addon_is_added(self) -> None:
        """所以 DNS 配置只能等 Master 建好再写 —— `_apply_dns_options` 的存在
        理由（与 sticky / anticache / upstream_auth 完全同一个约束）。"""
        self.assertNotIn("dns_name_servers", Options().keys())
        self.assertNotIn("dns_use_hosts_file", Options().keys())
        self.assertIn("dns_name_servers", self.master.options)
        self.assertIn("dns_use_hosts_file", self.master.options)

    def test_the_defaults_match_upstream(self) -> None:
        """[] = 跟随系统 DNS；True = 解析时查 hosts（开关方向不反转）。"""
        self.assertEqual(self.master.options.dns_name_servers, [])
        self.assertIs(self.master.options.dns_use_hosts_file, True)

    def test_bad_ip_strings_are_silently_accepted_by_the_native_option(self) -> None:
        """**钉子：无校验器。** ``dns_name_servers`` 注册时只有 typespec，
        ``_Option.set`` 只查类型不查值 —— 坏 IP 串过 ``options.update`` 静默
        放行、不抛 ``OptionsError``。守卫必须自建在种子 / 提交侧（方案 §5.3），
        与 block 那条「configure 抛错兜底」的类比不成立。"""
        self.master.options.update(dns_name_servers=["not-an-ip"])  # 不抛
        self.assertEqual(self.master.options.dns_name_servers, ["not-an-ip"])


class DnsOptionsGateTests(unittest.TestCase):
    """两个纯函数的翻译与校验矩阵（无内核、无 Qt 界面）。"""

    def test_option_updates_translates_both_options(self) -> None:
        self.assertEqual(
            dns_option_updates(["223.5.5.5"], False),
            {"dns_name_servers": ["223.5.5.5"], "dns_use_hosts_file": False},
        )
        self.assertEqual(
            dns_option_updates([], True),
            {"dns_name_servers": [], "dns_use_hosts_file": True},
        )

    def test_option_updates_returns_a_copy(self) -> None:
        """调用方随后 mutate 原列表不许污染已下发的更新字典。"""
        servers = ["223.5.5.5"]
        updates = dns_option_updates(servers, True)
        servers.append("evil")
        self.assertEqual(updates["dns_name_servers"], ["223.5.5.5"])

    def test_validate_accepts_ipv4_and_ipv6(self) -> None:
        self.assertEqual(
            _validate_dns_name_servers(["223.5.5.5", "2400:3200::1"]),
            ["223.5.5.5", "2400:3200::1"],
        )

    def test_validate_strips_surrounding_whitespace(self) -> None:
        self.assertEqual(_validate_dns_name_servers(["  223.5.5.5\t"]), ["223.5.5.5"])

    def test_validate_drops_blank_entries(self) -> None:
        """空行 / 纯空白行忽略 —— 对话框里允许用空行分隔。"""
        self.assertEqual(_validate_dns_name_servers(["", "   "]), [])
        self.assertEqual(
            _validate_dns_name_servers(["223.5.5.5", "", "  ", "119.29.29.29"]),
            ["223.5.5.5", "119.29.29.29"],
        )

    def test_validate_passes_an_empty_list_through(self) -> None:
        """空列表放行 = 清空回系统 DNS（与 None = 不改动相区分）。"""
        self.assertEqual(_validate_dns_name_servers([]), [])

    def test_validate_rejects_bad_strings_with_the_offending_text(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            _validate_dns_name_servers(["223.5.5.5", "bad-input"])
        self.assertIn("bad-input", str(ctx.exception))


class DnsOptionsKernelTests(unittest.TestCase):
    """跑真内核：种子 / 热更 / 回滚 / 缓存清理，`_qt.py` 原语等就绪。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def _runtime(self, **kwargs) -> MitmRuntime:
        runtime = MitmRuntime(listen_port=free_port(), **kwargs)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        return runtime

    def test_the_options_are_seeded_before_traffic(self) -> None:
        runtime = self._runtime(
            dns_name_servers=["223.5.5.5", "119.29.29.29"],
            dns_use_hosts_file=False,
        )
        master = runtime._master
        assert master is not None
        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)),
            ["223.5.5.5", "119.29.29.29"],
        )
        self.assertIs(runtime.call(lambda: master.options.dns_use_hosts_file), False)

    def test_bad_seeded_values_fall_back_to_system_dns(self) -> None:
        """历史落盘坏值的唯一拦截点：种子侧守卫回退 ``[]``（= 系统 DNS），
        不炸启动 —— ``options.update`` 对坏 IP 串不抛 ``OptionsError``，
        见 `DnsOptionsAddonTests` 那条无校验器钉子。"""
        runtime = self._runtime(dns_name_servers=["bad-input"])
        master = runtime._master
        assert master is not None
        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)), []
        )

    def test_hot_update_changes_the_running_options(self) -> None:
        runtime = self._runtime(dns_name_servers=["223.5.5.5"])
        master = runtime._master
        assert master is not None

        runtime.apply_dns_options(name_servers=["119.29.29.29"], use_hosts_file=False)

        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)),
            ["119.29.29.29"],
        )
        self.assertIs(runtime.call(lambda: master.options.dns_use_hosts_file), False)

    def test_none_leaves_that_side_untouched(self) -> None:
        """None = 不改动该项（与 bool 开关的 None 语义对齐）；两字段独立。"""
        runtime = self._runtime(dns_name_servers=["223.5.5.5"], dns_use_hosts_file=True)
        master = runtime._master
        assert master is not None

        runtime.apply_dns_options(use_hosts_file=False)

        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)),
            ["223.5.5.5"],
        )
        self.assertIs(runtime.call(lambda: master.options.dns_use_hosts_file), False)

    def test_an_empty_list_clears_back_to_system_dns(self) -> None:
        """清空自定义 DNS（回系统）必须显式传 ``[]``，None 做不到这件事。"""
        runtime = self._runtime(dns_name_servers=["223.5.5.5"])
        master = runtime._master
        assert master is not None

        runtime.apply_dns_options(name_servers=[])

        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)), []
        )
        self.assertEqual(runtime.dns_name_servers, [])

    def test_bad_values_raise_and_leave_the_copy_untouched(self) -> None:
        """坏值在动内存副本之前就被拒（回滚无从谈起）；内核 option 原样。"""
        runtime = self._runtime(dns_name_servers=["223.5.5.5"])
        master = runtime._master
        assert master is not None

        with self.assertRaises(ValueError):
            runtime.apply_dns_options(name_servers=["bad-input"])

        self.assertEqual(runtime.dns_name_servers, ["223.5.5.5"])
        self.assertEqual(
            runtime.call(lambda: list(master.options.dns_name_servers)),
            ["223.5.5.5"],
        )

    def test_hot_update_clears_the_resolver_cache(self) -> None:
        """configure 监测两选项变化后清 ``resolver`` / ``name_servers`` 两个
        缓存（方案 §3.4）——「新查询即刻生效」的机理；断 resolver 那个，
        两个清空在同一条 configure 路径上。"""
        runtime = self._runtime(dns_name_servers=["223.5.5.5"])
        master = runtime._master
        assert master is not None

        addon = runtime.call(lambda: master.addons.get("dnsresolver"))
        assert isinstance(addon, DnsResolver)
        runtime.call(lambda: addon.resolver())  # 造一次缓存
        self.assertEqual(runtime.call(lambda: addon.resolver.cache_info().currsize), 1)

        runtime.apply_dns_options(name_servers=["119.29.29.29"])
        self.assertEqual(runtime.call(lambda: addon.resolver.cache_info().currsize), 0)


if __name__ == "__main__":
    unittest.main()
