"""mTLS 客户端证书的内核侧验收（.plans/mtls-client-certs.md §6）。

分五组，按「越靠近原生越不需要内核」排：

* `ClientCertsOptionTests` —— 原生 `client_certs` 的注册时机。它写在
  `Options.__init__` 里、构造期就在，与同族的 `request_client_cert`（`TlsConfig.load`
  才注册）恰好相反 —— 这条差异必须钉住，防后人照 `proxyauth` 的「加了 addon 才有」
  误推同族约束。
* `ClientCertsTranslationTests` —— 纯函数 `client_certs_option_updates` 的翻译矩阵。
  它只产一个键（对照 `ssl_option_updates` 必须捎带 `upstream_cert`），且**不展开 `~`**。
* `ClientCertsGateTests` —— 闸门 `client_certs_error` 与盘点 `inspect_client_certs`：
  五连判、目录模式只查存在性、加密私钥两种写法都得认、私钥/证书不配对必须拦。
  全程临时目录，不碰 `get_certs_dir()`。
* `ClientCertsSeedTests` —— 跑真内核：种子默认值、坏值跳过且不炸启动、种子也清缓存。
* `ClientCertsHotUpdateTests` —— 跑真内核：热更、None 不改动、坏值在动内存副本之前
  就被拦、失败回滚、清缓存（mock 钉调用点 + 真 `cache_info` 钉行为）、与
  `ssl_insecure` 正交。
"""

import asyncio
import datetime
import functools
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, client_certs_error, inspect_client_certs
from ferret.core.mitm.bindings import Options, certs, net_tls
from ferret.core.mitm.certificate import (
    CLIENT_CERTS_DIR_NAME,
    build_trusted_ca_bundle,
    client_certs_suggest_dir,
    inspect_client_cert_file,
)
from ferret.core.mitm.master import FerretMaster
from ferret.core.mitm.runtime import client_certs_option_updates

from ._qt import start_runtime

# 原生出厂值（`Options.__init__`，mitmproxy 12.2.3）：一个路径，不是列表。
NATIVE_DEFAULT: str | None = None

_PASSPHRASE = b"ferret-test"

# 造一对 RSA 密钥要几百毫秒，本文件要用好几对 —— 按下标懒加载并全局复用。
_KEYPAIRS: list[tuple] = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def keypair(index: int = 0) -> tuple:
    """第 index 对（私钥, 证书）。借 ferret 自己用的 `certs.create_ca`，不手搓。"""
    while len(_KEYPAIRS) <= index:
        _KEYPAIRS.append(
            certs.create_ca("Ferret Test", f"host{len(_KEYPAIRS)}.example", 2048)
        )
    return _KEYPAIRS[index]


def key_pem(key, *, pkcs8: bool = True, passphrase: bytes | None = None) -> bytes:
    fmt = (
        serialization.PrivateFormat.PKCS8
        if pkcs8
        else serialization.PrivateFormat.TraditionalOpenSSL
    )
    enc = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return key.private_bytes(serialization.Encoding.PEM, fmt, enc)


def cert_pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def expired_cert(key):
    """自签一张三十天前就过期的证书（`certs.create_ca` 不给有效期参数）。"""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "stale.example")])
    end = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - datetime.timedelta(
        days=30
    )
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(end - datetime.timedelta(days=365))
        .not_valid_after(end)
        .sign(key, hashes.SHA256())
    )


def write_good(target: Path, *, key_first: bool = True, index: int = 0) -> str:
    """一份可用的客户端证书：同一次签发的私钥 + 证书拼在一个 .pem 里。"""
    key, cert = keypair(index)
    blocks = [key_pem(key), cert_pem(cert)]
    target.write_bytes(b"".join(blocks if key_first else blocks[::-1]))
    return str(target)


class ClientCertsOptionTests(unittest.TestCase):
    """原生 `client_certs` 的注册时机与它在 TLS 链上的落点。"""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.addCleanup(self.loop.close)

    def test_the_option_exists_at_construction_time(self) -> None:
        """它写在 `Options.__init__` 里 —— 所以能直接进构造参数，种子不必等 addon
        加载完（对照 `proxyauth` / `dns_*` 那批必须等 `Master` 建好）。"""
        options = Options()
        self.assertIn("client_certs", options.keys())
        self.assertEqual(options.client_certs, NATIVE_DEFAULT)
        self.assertEqual(Options(client_certs="x.pem").client_certs, "x.pem")

    def test_its_sibling_is_registered_by_the_addon(self) -> None:
        """同族的 `request_client_cert` 归 `TlsConfig.load` 注册 —— 钉住这条差异，
        免得后人把两者的注册时机混为一谈。"""
        self.assertNotIn("request_client_cert", Options().keys())
        master = FerretMaster(event_loop=self.loop)
        self.assertIn("request_client_cert", master.options.keys())

    def test_the_option_flows_into_the_cached_context_factory(self) -> None:
        """原生把 `client_certs` 解析成路径后喂给 `create_proxy_server_context`，
        而它是模块级 `lru_cache` —— 键里带 `client_cert` 正是必须手动清缓存的由来。"""
        import inspect as _inspect

        params = _inspect.signature(net_tls.create_proxy_server_context).parameters
        self.assertIn("client_cert", params)
        self.assertTrue(hasattr(net_tls.create_proxy_server_context, "cache_clear"))


class ClientCertsTranslationTests(unittest.TestCase):
    """纯函数 `client_certs_option_updates`：一个键、空串归一成 None、不展开 `~`。"""

    def test_a_path_passes_through_verbatim(self) -> None:
        """存什么下发什么：原生 `addons/core.py` 与 `tlsconfig.py` 各自 expanduser，
        我们抢着展开只会让 CONFIG 与 options 对不上。"""
        self.assertEqual(
            client_certs_option_updates("~/certs/c.pem"),
            {"client_certs": "~/certs/c.pem"},
        )

    def test_empty_becomes_none(self) -> None:
        for raw in ("", "   ", "\t\n"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    client_certs_option_updates(raw), {"client_certs": None}
                )

    def test_only_one_key_is_emitted(self) -> None:
        """对照 `ssl_option_updates`：那边必须捎带 `upstream_cert` 才过得了原生
        `Core.configure`，这边没有任何联动项 —— 多写一个键反而是噪声。"""
        self.assertEqual(list(client_certs_option_updates("c.pem")), ["client_certs"])

    def test_each_call_builds_a_fresh_dict(self) -> None:
        first = client_certs_option_updates("c.pem")
        second = client_certs_option_updates("c.pem")
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_the_empty_string_is_harmless_upstream_but_still_normalized(self) -> None:
        """原生 `tls_start_server` 用 `if ctx.options.client_certs:` 判真，空串恰好
        无害（对照 `ssl_verify_upstream_trusted_ca` 的空串会抛 `RuntimeError`）。
        归一成 None 是纪律不是救火：让「未启用」在 options 里只有一种写法。"""
        options = Options()
        options.update(client_certs="")  # 不抛
        self.assertEqual(options.client_certs, "")
        self.assertIsNone(client_certs_option_updates("")["client_certs"])


class ClientCertsGateTests(unittest.TestCase):
    """闸门与盘点共用同一个解析函数，所以判定必然一致。全程临时目录。"""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    # --- 放行 ---

    def test_an_empty_path_is_always_accepted(self) -> None:
        """空 = 清除，永远放行。"""
        self.assertEqual(client_certs_error(""), "")
        self.assertEqual(client_certs_error("   "), "")

    def test_both_block_orders_are_accepted(self) -> None:
        """私钥在前、证书在前都行：原生按块找，不在乎顺序。"""
        for key_first in (True, False):
            with self.subTest(key_first=key_first):
                path = write_good(self.dir / f"{key_first}.pem", key_first=key_first)
                self.assertEqual(client_certs_error(path), "")
                entry = inspect_client_cert_file(path)
                self.assertEqual(entry.error, "")
                self.assertEqual(entry.cn, "host0.example")
                self.assertEqual(entry.cert_count, 1)

    def test_a_chain_file_counts_every_certificate(self) -> None:
        """叶子 + 中间证书拼在一份里是常态：第一张当叶子，其余算链（同原生
        `use_certificate_chain_file`），卡片显示「共 N 张」。"""
        key, cert = keypair(0)
        _, other = keypair(1)
        target = self.dir / "chain.pem"
        target.write_bytes(key_pem(key) + cert_pem(cert) + cert_pem(other))
        self.assertEqual(client_certs_error(str(target)), "")
        self.assertEqual(inspect_client_cert_file(str(target)).cert_count, 2)

    def test_a_home_relative_path_is_not_rejected(self) -> None:
        """闸门自己 expanduser —— 不展开就会把好路径判成「不存在」。"""
        write_good(self.dir / "c.pem")
        with mock.patch.dict(
            os.environ, {"USERPROFILE": str(self.dir), "HOME": str(self.dir)}
        ):
            self.assertEqual(client_certs_error("~/c.pem"), "")

    # --- 拦下 ---

    def test_a_missing_path_is_rejected(self) -> None:
        missing = str(self.dir / "gone.pem")
        reason = client_certs_error(missing)
        self.assertIn(missing, reason)

    def test_a_file_without_a_key_is_rejected(self) -> None:
        """只导出了证书是最常见的误操作 —— 原生会拿它去 `use_privatekey_file`
        然后报一句看不懂的 OpenSSL 错。"""
        _, cert = keypair(0)
        target = self.dir / "certonly.pem"
        target.write_bytes(cert_pem(cert))
        self.assertIn("没有私钥", client_certs_error(str(target)))

    def test_a_file_without_a_certificate_is_rejected(self) -> None:
        key, _ = keypair(0)
        target = self.dir / "keyonly.pem"
        target.write_bytes(key_pem(key))
        self.assertIn("没有证书", client_certs_error(str(target)))

    def test_a_non_pem_file_is_rejected(self) -> None:
        target = self.dir / "junk.pem"
        target.write_bytes(b"not a pem at all\n")
        self.assertNotEqual(client_certs_error(str(target)), "")

    def test_both_encrypted_key_formats_are_rejected(self) -> None:
        """**判据是 `TypeError` 而不是字符串匹配。** 传统格式加密之后块名仍然是
        `RSA PRIVATE KEY`，靠找 `ENCRYPTED PRIVATE KEY` 只能认出 PKCS#8 那一半 ——
        漏掉的那一半会带着一把解不开的私钥进内核，握手时才失败。
        """
        key, cert = keypair(0)
        for pkcs8 in (True, False):
            with self.subTest(pkcs8=pkcs8):
                target = self.dir / f"enc{pkcs8}.pem"
                raw = key_pem(key, pkcs8=pkcs8, passphrase=_PASSPHRASE)
                target.write_bytes(raw + cert_pem(cert))
                if not pkcs8:
                    self.assertNotIn(b"ENCRYPTED PRIVATE KEY", raw)
                reason = client_certs_error(str(target))
                self.assertIn("已加密", reason)
                # 文案要能直接抄去终端跑，所以必须带上真实路径。
                self.assertIn("openssl rsa -in", reason)
                self.assertIn(str(target), reason)

    def test_a_mismatched_pair_is_rejected(self) -> None:
        """OpenSSL 对这种组合**一声不吭**：`use_privatekey_file` 与
        `use_certificate_chain_file` 都成功，只是那把私钥被静默丢掉，最终表现为
        一次「没出示证书」的失败握手 —— 不在这里拦，用户无从排查。"""
        key, _ = keypair(0)
        _, cert = keypair(1)
        target = self.dir / "mismatch.pem"
        target.write_bytes(key_pem(key) + cert_pem(cert))
        self.assertIn("不匹配", client_certs_error(str(target)))

    # --- 目录模式 ---

    def test_a_directory_is_accepted_even_with_bad_files_inside(self) -> None:
        """目录里的坏文件只进盘点、不拦保存：判据同上游信任组的「坏文件回退公共
        根」—— 一份坏证书不该让整项配不上。"""
        write_good(self.dir / "good.example.pem")
        (self.dir / "bad.example.pem").write_bytes(b"junk")
        self.assertEqual(client_certs_error(str(self.dir)), "")
        summary = inspect_client_certs(str(self.dir))
        self.assertTrue(summary.is_dir)
        self.assertEqual([item.name for item in summary.good], ["good.example.pem"])
        self.assertEqual([item.name for item in summary.bad], ["bad.example.pem"])

    def test_an_empty_directory_is_accepted(self) -> None:
        """刚建好还没往里放东西是正常中间态。"""
        empty = self.dir / "empty"
        empty.mkdir()
        self.assertEqual(client_certs_error(str(empty)), "")
        self.assertEqual(inspect_client_certs(str(empty)).entries, ())

    def test_only_pem_files_in_the_top_level_are_scanned(self) -> None:
        """原生只拼 `<目录>/<主机名>.pem` —— 别的后缀和子目录永远匹配不到，
        盘出来只会让用户以为配好了。"""
        write_good(self.dir / "a.example.pem")
        write_good(self.dir / "b.example.crt")
        nested = self.dir / "sub"
        nested.mkdir()
        write_good(nested / "c.example.pem")
        summary = inspect_client_certs(str(self.dir))
        self.assertEqual([item.name for item in summary.entries], ["a.example.pem"])

    def test_a_huge_directory_is_truncated(self) -> None:
        """路径可能被指到盘根或一个 UNC 共享，而盘点跑在界面线程上。"""
        for index in range(3):
            write_good(self.dir / f"h{index}.pem")
        with mock.patch("ferret.core.mitm.certificate.CLIENT_CERTS_SCAN_LIMIT", 2):
            summary = inspect_client_certs(str(self.dir))
        self.assertTrue(summary.truncated)
        self.assertEqual(len(summary.entries), 2)

    # --- 盘点自身的姿态 ---

    def test_an_expired_certificate_stays_usable(self) -> None:
        """有的服务器根本不校验客户端证书有效期 —— 只在界面上标一下，不替它拒。"""
        key, _ = keypair(0)
        target = self.dir / "stale.pem"
        target.write_bytes(key_pem(key) + cert_pem(expired_cert(key)))
        self.assertEqual(client_certs_error(str(target)), "")
        summary = inspect_client_certs(str(target))
        self.assertEqual(summary.bad, ())
        self.assertEqual([item.name for item in summary.expired], ["stale.pem"])

    def test_inspect_reports_a_missing_path_without_raising(self) -> None:
        """界面每次 showEvent 都调它，落盘路径随时可能已经被删。"""
        summary = inspect_client_certs(str(self.dir / "gone.pem"))
        self.assertFalse(summary.exists)
        self.assertFalse(summary.is_dir)
        self.assertEqual(summary.entries, ())

    def test_inspect_writes_nothing(self) -> None:
        write_good(self.dir / "a.pem")
        before = sorted(item.name for item in self.dir.iterdir())
        inspect_client_certs(str(self.dir))
        inspect_client_certs(str(self.dir / "a.pem"))
        self.assertEqual(sorted(item.name for item in self.dir.iterdir()), before)

    def test_the_suggested_directory_is_not_created(self) -> None:
        """「创建推荐目录」按钮自己建 —— 算路径的函数不该有副作用。"""
        suggested = client_certs_suggest_dir(self.dir)
        self.assertEqual(suggested, self.dir / CLIENT_CERTS_DIR_NAME)
        self.assertFalse(suggested.exists())


class ClientCertsKernelTestCase(unittest.TestCase):
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
        # 种子链上同跑的上游信任那一刀会写合并产物，转到临时目录里去。
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


class ClientCertsSeedTests(ClientCertsKernelTestCase):
    """种子：默认值、真路径、坏值只跳过不炸启动。"""

    def test_the_default_matches_upstream(self) -> None:
        self.assertEqual(self._option(self._runtime(), "client_certs"), NATIVE_DEFAULT)

    def test_a_valid_path_is_seeded_verbatim(self) -> None:
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime(client_certs_path=path)
        self.assertEqual(self._option(runtime, "client_certs"), path)

    def test_a_directory_is_seeded_too(self) -> None:
        write_good(self.source_dir / "host.example.pem")
        runtime = self._runtime(client_certs_path=str(self.source_dir))
        self.assertEqual(self._option(runtime, "client_certs"), str(self.source_dir))

    def test_a_missing_path_is_skipped_with_a_warning(self) -> None:
        """落盘的那个路径随时可能被删 / 挪走。「今天没法出示客户端证书」远不如
        「应用起不来」严重 —— 整项跳过，保持原生 None，界面自己打成失效态。"""
        missing = str(self.source_dir / "gone.pem")
        with self.assertLogs("ferret.mitmproxy", "WARNING") as captured:
            runtime = self._runtime(client_certs_path=missing)
        self.assertIsNone(self._option(runtime, "client_certs"))
        self.assertTrue(any("客户端证书" in line for line in captured.output))
        # 内存副本保留用户意图，不因一次跳过就被抹掉。
        self.assertEqual(runtime.client_certs_path, missing)

    def test_an_encrypted_key_is_skipped_at_startup(self) -> None:
        """闸门在种子侧走的是同一个函数，所以历史落盘的坏内容也拦得住。"""
        key, cert = keypair(0)
        target = self.source_dir / "enc.pem"
        target.write_bytes(key_pem(key, passphrase=_PASSPHRASE) + cert_pem(cert))
        with self.assertLogs("ferret.mitmproxy", "WARNING"):
            runtime = self._runtime(client_certs_path=str(target))
        self.assertIsNone(self._option(runtime, "client_certs"))

    def test_the_seed_path_clears_the_context_cache(self) -> None:
        """**T-E4。** 缓存挂在模块上，跨 `MitmRuntime.restart` 存活 ——「停止抓包
        → 换证书 → 重新开始」单靠重启内核清不掉，种子这一步必须自己清。"""
        path = write_good(self.source_dir / "c.pem")
        with mock.patch(
            "ferret.core.mitm.runtime.clear_proxy_server_context_cache"
        ) as spy:
            self._runtime(client_certs_path=path)
        spy.assert_called()

    def test_the_seed_clears_even_when_the_path_is_rejected(self) -> None:
        """清缓存是无条件的：跳过下发不等于内核里没有上一轮留下的 context。"""
        with (
            mock.patch(
                "ferret.core.mitm.runtime.clear_proxy_server_context_cache"
            ) as spy,
            self.assertLogs("ferret.mitmproxy", "WARNING"),
        ):
            self._runtime(client_certs_path=str(self.source_dir / "gone.pem"))
        spy.assert_called()


class ClientCertsHotUpdateTests(ClientCertsKernelTestCase):
    """热更：闸门先判、None 不改动、失败回滚、清缓存、与 `ssl_insecure` 正交。"""

    def test_hot_update_changes_the_running_options(self) -> None:
        runtime = self._runtime()
        path = write_good(self.source_dir / "c.pem")

        runtime.apply_client_certs(path=path)

        self.assertEqual(self._option(runtime, "client_certs"), path)
        self.assertEqual(runtime.client_certs_path, path)

    def test_none_leaves_the_option_untouched(self) -> None:
        """None = 不改动该项；清除必须显式传空串。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime(client_certs_path=path)

        runtime.apply_client_certs()

        self.assertEqual(self._option(runtime, "client_certs"), path)
        self.assertEqual(runtime.client_certs_path, path)

    def test_an_empty_string_clears_the_option(self) -> None:
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime(client_certs_path=path)

        runtime.apply_client_certs(path="")

        self.assertIsNone(self._option(runtime, "client_certs"))
        self.assertEqual(runtime.client_certs_path, "")

    def test_a_bad_path_is_rejected_before_anything_moves(self) -> None:
        """**闸门在动内存副本之前判。** 与上游信任那刀不同：信任文件解不动可以
        回退公共根，客户端证书解不动没有降级余地 —— 放过去就是一次「配了却不
        出示」的静默失败。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime(client_certs_path=path)
        _, cert = keypair(1)
        broken = self.source_dir / "certonly.pem"
        broken.write_bytes(cert_pem(cert))

        with self.assertRaises(ValueError):
            runtime.apply_client_certs(path=str(broken))

        self.assertEqual(runtime.client_certs_path, path)
        self.assertEqual(self._option(runtime, "client_certs"), path)

    def test_a_rejected_update_rolls_the_copy_back(self) -> None:
        """内核拒掉时内存副本必须跟着回滚，绝不留下「界面显示已生效、内核其实没
        收到」。`client_certs` 本身喂不出这种拒绝（`Optional[str]` 什么路径都收），
        所以把翻译函数换成一个类型不对的版本来触发真闸门。"""
        path = write_good(self.source_dir / "c.pem")
        other = write_good(self.source_dir / "d.pem")
        runtime = self._runtime(client_certs_path=path)

        with (
            mock.patch(
                "ferret.core.mitm.runtime.client_certs_option_updates",
                return_value={"client_certs": 12345},
            ),
            self.assertRaises(TypeError),
        ):
            runtime.apply_client_certs(path=other)

        self.assertEqual(runtime.client_certs_path, path)
        self.assertEqual(self._option(runtime, "client_certs"), path)

    def test_the_copy_is_aligned_while_the_kernel_is_down(self) -> None:
        """内核没跑只对齐内存副本，下次启动由 `_apply_client_certs` 播种 ——
        缓存也在那一步清，所以这里不清不漏。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)

        runtime.apply_client_certs(path=path)

        self.assertEqual(runtime.client_certs_path, path)

    # --- 缓存 ---

    def test_both_the_set_and_the_clear_path_drop_the_cache(self) -> None:
        """**T-D4。** 清除那条同样要清：关掉功能之后不该还有残留 context 在出示。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime()
        with mock.patch(
            "ferret.core.mitm.runtime.clear_proxy_server_context_cache"
        ) as spy:
            runtime.apply_client_certs(path=path)
            self.assertEqual(spy.call_count, 1)
            runtime.apply_client_certs(path="")
            self.assertEqual(spy.call_count, 2)

    def test_a_rejected_path_does_not_touch_the_cache(self) -> None:
        """闸门拦下的那次连内存副本都没动，白清一遍缓存等于平白重建所有连接的
        上下文。"""
        runtime = self._runtime()
        with (
            mock.patch(
                "ferret.core.mitm.runtime.clear_proxy_server_context_cache"
            ) as spy,
            self.assertRaises(ValueError),
        ):
            runtime.apply_client_certs(path=str(self.source_dir / "gone.pem"))
        spy.assert_not_called()

    def test_the_cache_is_really_emptied(self) -> None:
        """**T-D4b：不止钉调用点，钉行为。** `create_proxy_server_context` 是模块级
        `@lru_cache(256)`，键里只有 `client_cert` 的**路径字符串** —— 路径不变、原地
        换掉文件内容，旧上下文会被继续复用。这里先把缓存填热，再跑一次热更，断言
        `currsize` 归零。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime()

        net_tls.create_proxy_server_context.cache_clear()
        net_tls.create_proxy_server_context(
            method=net_tls.Method.TLS_CLIENT_METHOD,
            min_version=net_tls.Version.TLS1_2,
            max_version=net_tls.Version.TLS1_3,
            cipher_list=None,
            ecdh_curve=None,
            verify=net_tls.Verify.VERIFY_NONE,
            ca_path=None,
            ca_pemfile=None,
            client_cert=path,
            legacy_server_connect=False,
        )
        self.assertEqual(net_tls.create_proxy_server_context.cache_info().currsize, 1)

        runtime.apply_client_certs(path=path)

        self.assertEqual(net_tls.create_proxy_server_context.cache_info().currsize, 0)

    # --- 与相邻选项的关系 ---

    def test_ssl_insecure_and_client_certs_are_orthogonal(self) -> None:
        """**T-D5。** 「不校验上游证书」管的是我们怎么验对面，客户端证书管的是我们
        出示什么 —— 两条互不让路，翻一个不该动另一个。"""
        path = write_good(self.source_dir / "c.pem")
        runtime = self._runtime(client_certs_path=path)

        runtime.apply_ssl_options(insecure=True)
        self.assertEqual(self._option(runtime, "client_certs"), path)
        self.assertEqual(runtime.client_certs_path, path)

        runtime.apply_client_certs(path="")
        self.assertIs(self._option(runtime, "ssl_insecure"), True)
        self.assertIs(runtime.ssl_insecure, True)


if __name__ == "__main__":
    unittest.main()
