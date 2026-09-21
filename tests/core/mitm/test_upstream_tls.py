"""上游 TLS 信任的内核侧验收（.plans/upstream-tls.md §6）。

分五组，按「越靠近原生越不需要内核」排：

* `UpstreamTlsAddonTests` —— 原生 `TlsConfig` 的挂载与三个选项的注册时机。
  **与 DNS 相反**：它们写在 `Options.__init__` 里、构造期就存在，理论上能进
  `Options(...)` 构造参数 —— 这条差异必须钉住，防后人照 `dns_*` 的「加了 addon
  才有」误推同族约束，也防上游哪天把它们挪进 addon。
* `UpstreamTlsGateTests` —— 纯函数 `ssl_option_updates` 的翻译矩阵，外加两条
  反面钉子：空串会让原生抛 `RuntimeError`（所以必须回 `None`）、拼接链在
  `upstream_cert` 关着时会让原生抛 `OptionsError`（所以必须带上 `True`）。
* `TrustedCaBundleTests` —— 合并产物：内容寻址的文件名（`create_proxy_server_context`
  按**路径字符串** lru_cache，同名不同内容 = 热更静默失效）、旧指纹清理、坏文件
  归类。全程临时目录，不碰 `get_certs_dir()`。
* `UpstreamTlsSeedTests` —— 跑真内核：种子默认值、坏文件回退公共根且不炸启动。
* `UpstreamTlsHotUpdateTests` —— 跑真内核：热更、None 不改动、失败两边一起回滚、
  未运行时只改副本、`ssl_insecure` 翻转不吃掉信任文件列表。
"""

import asyncio
import functools
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import certifi
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, inspect_trusted_ca_files
from ferret.core.mitm.bindings import Options, OptionsError
from ferret.core.mitm.certificate import (
    TRUSTED_CA_PREFIX,
    TRUSTED_CA_SUFFIX,
    SystemCertificateService,
    build_trusted_ca_bundle,
)
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.runtime import ssl_option_updates

from ._qt import start_runtime

# 三个选项的原生出厂值（`Options.__init__`，mitmproxy 12.2.3）。
NATIVE_DEFAULTS: dict[str, object] = {
    "ssl_insecure": False,
    "ssl_verify_upstream_trusted_ca": None,
    "add_upstream_certs_to_client_chain": False,
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_ca_pem(directory: Path) -> Path:
    """生成一张真 CA 证书并返回它的 PEM 路径（借 ferret 自己的生成器，不手搓）。"""
    service = SystemCertificateService(directory)
    service.ensure()
    return service.cert_path


class UpstreamTlsAddonTests(unittest.TestCase):
    """原生 `TlsConfig` 在 ferret 链上的位置，与三个选项的注册时机。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)
        self.master = FerretMaster(event_loop=self.loop)

    def test_the_tlsconfig_addon_is_mounted(self) -> None:
        """`FerretTlsConfig` 继承并挂载原生 `TlsConfig`（只为改证书文件名），
        `tls_start_server` 三个钩子本来就在跑 —— 本功能零 addon 工作。"""
        from mitmproxy.addons.tlsconfig import TlsConfig

        from ferret.core.mitm.addons import FerretTlsConfig

        addon = self.master.addons.get("ferrettlsconfig")
        self.assertIsInstance(addon, FerretTlsConfig)
        self.assertIsInstance(addon, TlsConfig)

    def test_the_options_exist_at_construction_time(self) -> None:
        """**钉子：与 `dns_*` 相反。** 这三个写在 `Options.__init__` 里，不建
        Master 就有 —— 别照 DNS 那条「加了 addon 才有」推同族约束。

        仍走 `_apply_*` 播种是另一回事：合并产物要现算，且必须支持运行中热更。
        上游哪天把它们挪进 addon，这条会先红。
        """
        keys = Options().keys()
        for name in NATIVE_DEFAULTS:
            with self.subTest(name=name):
                self.assertIn(name, keys)

    def test_the_defaults_match_upstream(self) -> None:
        for name, value in NATIVE_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(self.master.options, name), value)

    def test_upstream_cert_defaults_to_enabled(self) -> None:
        """拼接链的硬依赖：出厂就是 True，且 ferret 从不暴露 —— 现状安全。
        `ssl_option_updates` 显式带上它是防上游翻默认值，不是补现状的洞。"""
        self.assertIs(self.master.options.upstream_cert, True)

    def test_splicing_without_upstream_cert_raises_options_error(self) -> None:
        """**钉子：`Core.configure` 的闸门。** 拼接链在 `upstream_cert` 关着时
        被原生拒掉，且抛的就是 `OptionsError`（不是 `ConfigurationError`）——
        现有 `except (ValueError, OptionsError)` 那套姿态天然覆盖。

        顺带钉住原生**自己会回滚**：拒掉之后两个值都还是原样，所以 ferret 侧
        只需要回滚自己的内存副本。
        """
        with self.assertRaises(OptionsError):
            self.master.options.update(
                add_upstream_certs_to_client_chain=True, upstream_cert=False
            )
        self.assertIs(self.master.options.add_upstream_certs_to_client_chain, False)
        self.assertIs(self.master.options.upstream_cert, True)


class UpstreamTlsGateTests(unittest.TestCase):
    """纯函数 `ssl_option_updates` 的翻译矩阵（无内核、无 Qt 界面）。"""

    def test_it_translates_all_three_options(self) -> None:
        self.assertEqual(
            ssl_option_updates(True, "/tmp/bundle.pem", False),
            {
                "ssl_insecure": True,
                "ssl_verify_upstream_trusted_ca": "/tmp/bundle.pem",
                "add_upstream_certs_to_client_chain": False,
            },
        )

    def test_an_empty_bundle_becomes_none_not_an_empty_string(self) -> None:
        """**T3：空串 ≠ 未设置。** 见下一条用例的反面证据。"""
        for empty in (None, ""):
            with self.subTest(empty=empty):
                updates = ssl_option_updates(False, empty, False)
                self.assertIsNone(updates["ssl_verify_upstream_trusted_ca"])

    def test_an_empty_ca_pemfile_makes_upstream_raise(self) -> None:
        """**T3 的反面：为什么非 `None` 不可。**

        `load_verify_locations("", None)` 抛 `SSL.Error`，被 `net/tls.py` 重包成
        `RuntimeError` 砸在**握手路径**上 —— 那不是 `OptionsError`，
        `options.update` 外面那层 try/except 拦不住。唯一的办法是不让它产生。
        """
        from mitmproxy.net import tls

        kwargs = {
            "method": tls.Method.TLS_CLIENT_METHOD,
            "min_version": tls.Version.UNBOUNDED,
            "max_version": tls.Version.UNBOUNDED,
            "cipher_list": None,
            "ecdh_curve": None,
            "verify": tls.Verify.VERIFY_PEER,
            "ca_path": None,
            "client_cert": None,
            "legacy_server_connect": False,
        }
        with self.assertRaises(RuntimeError):
            tls.create_proxy_server_context(ca_pemfile="", **kwargs)
        # None 才是「未设置」，此时原生自己回落 certifi。
        tls.create_proxy_server_context(ca_pemfile=None, **kwargs)

    def test_splicing_carries_upstream_cert(self) -> None:
        """拼接链为真时必须一并显式带上 `upstream_cert: True`（见 addon 组那条
        `OptionsError` 钉子）。"""
        updates = ssl_option_updates(False, None, True)
        self.assertIs(updates["upstream_cert"], True)

    def test_upstream_cert_is_absent_when_not_splicing(self) -> None:
        """不拼接时不写这个键：ferret 从不暴露它，没理由替用户按住一个值。"""
        self.assertNotIn("upstream_cert", ssl_option_updates(True, None, False))

    def test_it_returns_a_fresh_dict(self) -> None:
        first = ssl_option_updates(False, None, False)
        first["ssl_insecure"] = "polluted"
        self.assertIs(ssl_option_updates(False, None, False)["ssl_insecure"], False)


class TrustedCaBundleTests(unittest.TestCase):
    """合并产物与只读盘点，全程临时目录（绝不碰 `get_certs_dir()`）。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.certs_dir = Path(tmp.name) / "certs"
        self.certs_dir.mkdir()
        source = tempfile.TemporaryDirectory()
        self.addCleanup(source.cleanup)
        self.source_dir = Path(source.name)

    def _build(self, files: list[str]) -> tuple[str | None, list[str]]:
        return build_trusted_ca_bundle(files, certs_dir=self.certs_dir)

    def _bundles(self) -> list[Path]:
        return sorted(self.certs_dir.glob(f"{TRUSTED_CA_PREFIX}*{TRUSTED_CA_SUFFIX}"))

    def test_no_files_means_no_artifact(self) -> None:
        """空输入 → `(None, [])` = 不下发 ca_pemfile = 原生 certifi 行为。"""
        self.assertEqual(self._build([]), (None, []))
        self.assertEqual(self._bundles(), [])

    def test_blank_paths_are_ignored(self) -> None:
        """对话框里允许空行分隔（与 DNS 那边逐行忽略空白同一姿态）。"""
        self.assertEqual(self._build(["", "   "]), (None, []))

    def test_a_good_file_produces_a_fingerprinted_artifact(self) -> None:
        pem = make_ca_pem(self.source_dir)
        path, bad = self._build([str(pem)])
        assert path is not None
        self.assertEqual(bad, [])
        name = Path(path).name
        self.assertTrue(name.startswith(TRUSTED_CA_PREFIX))
        self.assertTrue(name.endswith(TRUSTED_CA_SUFFIX))
        self.assertEqual(self._bundles(), [Path(path)])

    def test_the_artifact_keeps_the_public_roots(self) -> None:
        """D1 的核心承诺：合并而不是替换 —— 公共根仍在库内，百度不受影响。"""
        pem = make_ca_pem(self.source_dir)
        path, _ = self._build([str(pem)])
        assert path is not None
        blob = Path(path).read_bytes()
        self.assertIn(pem.read_bytes().strip(), blob)
        public = Path(certifi.where()).read_bytes().strip()
        self.assertIn(public, blob)

    def test_the_same_input_yields_the_same_path(self) -> None:
        """**T4 上半：内容寻址。** 同内容同路径，重复下发不动 mtime。"""
        pem = make_ca_pem(self.source_dir)
        first, _ = self._build([str(pem)])
        assert first is not None
        stamp = Path(first).stat().st_mtime_ns
        second, _ = self._build([str(pem)])
        self.assertEqual(second, first)
        self.assertEqual(Path(first).stat().st_mtime_ns, stamp)

    def test_changed_content_yields_a_new_path(self) -> None:
        """**T4 下半：`create_proxy_server_context` 按路径字符串 lru_cache。**

        文件名固定的话，用户换一把根（内容变、路径没变）会永久命中旧 context ——
        热更静默失效且无从排查。改名即改键，这条红了就是那个陷阱回来了。
        """
        service = SystemCertificateService(self.source_dir)
        service.ensure()
        first, _ = self._build([str(service.cert_path)])
        service.regenerate()  # 同一个路径，换了一张证书
        second, _ = self._build([str(service.cert_path)])
        self.assertNotEqual(second, first)

    def test_the_stale_fingerprint_is_pruned(self) -> None:
        """换根之后目录里只剩当前那一份，不攒孤儿。"""
        service = SystemCertificateService(self.source_dir)
        service.ensure()
        self._build([str(service.cert_path)])
        service.regenerate()
        current, _ = self._build([str(service.cert_path)])
        self.assertEqual(self._bundles(), [Path(str(current))])

    def test_clearing_the_list_prunes_every_artifact(self) -> None:
        pem = make_ca_pem(self.source_dir)
        self._build([str(pem)])
        self.assertEqual(self._build([]), (None, []))
        self.assertEqual(self._bundles(), [])

    def test_only_its_own_prefix_is_pruned(self) -> None:
        """`CA_ARTIFACTS` 那族是 `{APP_NAME}-*`，两边永不相交 —— 清理不许越界。"""
        service = SystemCertificateService(self.certs_dir)
        service.ensure()
        keep = self.certs_dir / "unrelated.pem"
        keep.write_bytes(b"not ours")
        self._build([])
        self.assertTrue(keep.exists())
        self.assertTrue(service.cert_path.exists())

    def test_unreadable_files_are_reported_not_raised(self) -> None:
        """一把坏证书不该拖死内核启动：坏文件只进返回值。"""
        missing = str(self.source_dir / "nope.pem")
        junk = self.source_dir / "junk.pem"
        junk.write_bytes(b"hello, not a certificate")
        path, bad = self._build([missing, str(junk)])
        self.assertIsNone(path)
        self.assertEqual(bad, [missing, str(junk)])

    def test_good_files_survive_alongside_bad_ones(self) -> None:
        """整批里能用的照常生效，只有全坏才回退公共根。"""
        pem = make_ca_pem(self.source_dir)
        junk = self.source_dir / "junk.pem"
        junk.write_bytes(b"hello")
        path, bad = self._build([str(pem), str(junk)])
        self.assertIsNotNone(path)
        self.assertEqual(bad, [str(junk)])

    def test_a_half_bad_bundle_file_is_rejected_whole(self) -> None:
        """同一个文件里只要有一块解不动就整份判坏：半份信任库比没有更难排查。"""
        pem = make_ca_pem(self.source_dir)
        mixed = self.source_dir / "mixed.pem"
        mixed.write_bytes(
            pem.read_bytes()
            + b"\n-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n"
        )
        path, bad = self._build([str(mixed)])
        self.assertIsNone(path)
        self.assertEqual(bad, [str(mixed)])

    def test_inspect_counts_every_certificate_in_a_bundle(self) -> None:
        """用户手上的文件常是多张拼起来的 —— `Cert.from_pem` 只解第一张，所以
        自己按块切开逐张解（卡片要显示「共 N 张」）。"""
        first = make_ca_pem(self.source_dir)
        second_dir = self.source_dir / "second"
        second_dir.mkdir()
        second = make_ca_pem(second_dir)
        stacked = self.source_dir / "stacked.pem"
        stacked.write_bytes(first.read_bytes() + second.read_bytes())
        summary = inspect_trusted_ca_files([str(stacked)])
        self.assertEqual(summary.cert_count, 2)
        self.assertEqual(summary.configured, 1)

    def test_inspect_writes_nothing(self) -> None:
        """界面每次刷新都调它 —— 写盘的是 `build_trusted_ca_bundle`，不是它。"""
        pem = make_ca_pem(self.source_dir)
        inspect_trusted_ca_files([str(pem)])
        self.assertEqual(self._bundles(), [])


class UpstreamTlsKernelTestCase(unittest.TestCase):
    """跑真内核的共同底座：临时证书目录 + 产物不外溢。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.certs_dir = Path(tmp.name) / "certs"
        self.certs_dir.mkdir()
        self.source_dir = Path(tmp.name) / "src"
        self.source_dir.mkdir()
        # 种子与热更都调不带 certs_dir 的 `build_trusted_ca_bundle`（生产上就该
        # 落 `get_certs_dir()`）—— 转发到真函数、只把目录换成临时的，既测到真
        # 代码又不往用户目录里写东西。
        patcher = mock.patch(
            "ferret.core.mitm.runtime.build_trusted_ca_bundle",
            functools.partial(build_trusted_ca_bundle, certs_dir=self.certs_dir),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _runtime(self, **kwargs) -> MitmRuntime:
        runtime = MitmRuntime(listen_port=free_port(), **kwargs)
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        return runtime

    def _option(self, runtime: MitmRuntime, name: str) -> object:
        master = runtime._master
        assert master is not None
        return runtime.call(lambda: getattr(master.options, name))


class UpstreamTlsSeedTests(UpstreamTlsKernelTestCase):
    """种子：默认值、真产物、坏值回退，全都不许炸启动。"""

    def test_the_defaults_match_upstream(self) -> None:
        runtime = self._runtime()
        for name, value in NATIVE_DEFAULTS.items():
            with self.subTest(name=name):
                self.assertEqual(self._option(runtime, name), value)

    def test_the_options_are_seeded_before_traffic(self) -> None:
        pem = make_ca_pem(self.source_dir)
        runtime = self._runtime(
            ssl_insecure=True,
            ssl_trusted_ca_files=[str(pem)],
            add_upstream_certs_to_client_chain=True,
        )
        self.assertIs(self._option(runtime, "ssl_insecure"), True)
        self.assertIs(self._option(runtime, "add_upstream_certs_to_client_chain"), True)
        bundle = self._option(runtime, "ssl_verify_upstream_trusted_ca")
        assert isinstance(bundle, str)
        self.assertTrue(Path(bundle).exists())

    def test_bad_seeded_files_fall_back_to_the_public_roots(self) -> None:
        """历史落盘的文件可能早被删了 —— 回退公共根（option 回 `None`），
        而不是把上游信任库搞成空库，更不是让内核起不来。"""
        runtime = self._runtime(
            ssl_trusted_ca_files=[str(self.source_dir / "gone.pem")]
        )
        self.assertIsNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))

    def test_an_empty_list_seeds_none_not_an_empty_string(self) -> None:
        """T3 在种子侧的落点。"""
        runtime = self._runtime(ssl_trusted_ca_files=[])
        self.assertIsNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))


class UpstreamTlsHotUpdateTests(UpstreamTlsKernelTestCase):
    """热更：None 不改动、成功同步、失败两边回滚、未运行只改副本。"""

    def test_hot_update_changes_the_running_options(self) -> None:
        runtime = self._runtime()
        pem = make_ca_pem(self.source_dir)

        runtime.apply_ssl_options(insecure=True, trusted_ca_files=[str(pem)])

        self.assertIs(self._option(runtime, "ssl_insecure"), True)
        self.assertIsNotNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))
        self.assertIs(runtime.ssl_insecure, True)
        self.assertEqual(runtime.ssl_trusted_ca_files, [str(pem)])

    def test_none_leaves_that_option_untouched(self) -> None:
        """三个参数互相独立，None = 不改动该项。"""
        pem = make_ca_pem(self.source_dir)
        runtime = self._runtime(ssl_insecure=True, ssl_trusted_ca_files=[str(pem)])

        runtime.apply_ssl_options(add_upstream_certs=True)

        self.assertIs(self._option(runtime, "ssl_insecure"), True)
        self.assertEqual(runtime.ssl_trusted_ca_files, [str(pem)])
        self.assertIs(self._option(runtime, "add_upstream_certs_to_client_chain"), True)

    def test_an_empty_list_clears_back_to_the_public_roots(self) -> None:
        """清空信任文件必须显式传 `[]`，None 做不到这件事。"""
        pem = make_ca_pem(self.source_dir)
        runtime = self._runtime(ssl_trusted_ca_files=[str(pem)])
        self.assertIsNotNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))

        runtime.apply_ssl_options(trusted_ca_files=[])

        self.assertIsNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))
        self.assertEqual(runtime.ssl_trusted_ca_files, [])

    def test_flipping_insecure_keeps_the_trusted_files(self) -> None:
        """**T-D5：让路姿态。** 开「不校验上游」只是让信任库暂时无从生效，
        配置值原样保留，关回去即刻复效 —— 与 `block_private` 同一约定。
        """
        pem = make_ca_pem(self.source_dir)
        runtime = self._runtime(ssl_trusted_ca_files=[str(pem)])
        bundle = self._option(runtime, "ssl_verify_upstream_trusted_ca")

        runtime.apply_ssl_options(insecure=True)
        self.assertEqual(runtime.ssl_trusted_ca_files, [str(pem)])
        self.assertEqual(
            self._option(runtime, "ssl_verify_upstream_trusted_ca"), bundle
        )

        runtime.apply_ssl_options(insecure=False)
        self.assertIs(self._option(runtime, "ssl_insecure"), False)
        self.assertEqual(
            self._option(runtime, "ssl_verify_upstream_trusted_ca"), bundle
        )

    def test_bad_files_do_not_fail_the_update(self) -> None:
        """坏文件不是坏值：整批里没一个能用就回退公共根，但保存照样成功 ——
        界面自己调 `inspect_trusted_ca_files` 复算并显示「N 个文件已失效」。"""
        missing = str(self.source_dir / "gone.pem")
        runtime = self._runtime()

        runtime.apply_ssl_options(trusted_ca_files=[missing])

        self.assertEqual(runtime.ssl_trusted_ca_files, [missing])
        self.assertIsNone(self._option(runtime, "ssl_verify_upstream_trusted_ca"))

    def test_a_rejected_update_rolls_both_sides_back(self) -> None:
        """内核拒掉时内存副本必须跟着回滚，绝不留下「界面显示已生效、内核其实
        没收到」。触发点借 `Core.configure` 那道真闸门（见 addon 组的钉子）：
        把翻译函数换成一个**漏带** `upstream_cert` 的版本，正是
        `ssl_option_updates` 每次都显式带上它的理由。"""
        runtime = self._runtime()
        broken = {
            "add_upstream_certs_to_client_chain": True,
            "upstream_cert": False,
        }
        with (
            mock.patch(
                "ferret.core.mitm.runtime.ssl_option_updates", return_value=broken
            ),
            self.assertRaises(ValueError),
        ):
            runtime.apply_ssl_options(add_upstream_certs=True)

        self.assertIs(runtime.add_upstream_certs_to_client_chain, False)
        self.assertIs(
            self._option(runtime, "add_upstream_certs_to_client_chain"), False
        )

    def test_the_copy_is_aligned_while_the_kernel_is_down(self) -> None:
        """内核没跑只对齐内存副本，下次启动由 `_apply_ssl_options` 播种。
        顺带钉住**不写产物**：没人读的 PEM 不该出现在证书目录里。"""
        pem = make_ca_pem(self.source_dir)
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)

        runtime.apply_ssl_options(insecure=True, trusted_ca_files=[str(pem)])

        self.assertIs(runtime.ssl_insecure, True)
        self.assertEqual(runtime.ssl_trusted_ca_files, [str(pem)])
        self.assertEqual(
            list(self.certs_dir.glob(f"{TRUSTED_CA_PREFIX}*{TRUSTED_CA_SUFFIX}")), []
        )


if __name__ == "__main__":
    unittest.main()
