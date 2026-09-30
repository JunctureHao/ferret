"""#16 回归：上游/第三方 WARNING+ 进 ferret.log，INFO 不进，ferret 树不重复。

经真实 ``init_logging`` + 真实根 logger 传播链验证（ferret.log 落临时目录，
不写用户配置目录）。上游故障的实际来源：master 的 "Unhandled error in task"
（mitmproxy.master）、proxyserver 实例启动失败、tlsconfig/serverplayback/certs
告警——全部靠根 logger 上的 ``_UpstreamFaultForwarder`` 捞进 ferret 的 sink。
"""

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ferret.core import log as ferret_log


class UpstreamFaultChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        # init_logging 的幂等守卫看模块单例，要换临时目录重挂就得先整体复位；
        # 快照原状态，teardown 原样还回，不影响同进程其他用例。
        self._saved = (
            ferret_log._emitter,
            ferret_log._handler,
            ferret_log._root_forwarder,
        )
        ferret_log._emitter = None
        ferret_log._handler = None
        ferret_log._root_forwarder = None

        self._ferret_logger = logging.getLogger("ferret")
        self._ferret_handlers = list(self._ferret_logger.handlers)
        self._ferret_propagate = self._ferret_logger.propagate
        self._ferret_logger.handlers.clear()

        self._root = logging.getLogger()
        self._root_handlers = list(self._root.handlers)
        self._root_level = self._root.level
        self._root.handlers.clear()

        self._tmp = tempfile.TemporaryDirectory()
        with patch.object(
            ferret_log, "get_config_dir", return_value=Path(self._tmp.name)
        ):
            ferret_log.init_logging()
        self.log_file = Path(self._tmp.name) / "ferret.log"
        # 顺序不能乱：先摘 handler 再关句柄（Windows 下不关文件句柄，
        # TemporaryDirectory 清不掉），然后还原 logger 原状态，最后删临时目录。
        self.addCleanup(self._teardown)

    def _teardown(self) -> None:
        ours = set(self._ferret_logger.handlers) | set(self._root.handlers)
        for h in self._ferret_logger.handlers[:]:
            self._ferret_logger.removeHandler(h)
        for h in self._root.handlers[:]:
            self._root.removeHandler(h)
        for h in ours:
            h.close()
        self._ferret_logger.handlers = self._ferret_handlers
        self._ferret_logger.propagate = self._ferret_propagate
        self._root.handlers = self._root_handlers
        self._root.setLevel(self._root_level)
        (
            ferret_log._emitter,
            ferret_log._handler,
            ferret_log._root_forwarder,
        ) = self._saved
        self._tmp.cleanup()

    def _read_log(self) -> str:
        for h in self._ferret_logger.handlers:
            h.flush()
        return self.log_file.read_text(encoding="utf-8")

    def test_upstream_error_reaches_ferret_log_and_ring_buffer(self) -> None:
        # master._asyncio_exception_handler 的真实 logger 名与文案
        logging.getLogger("mitmproxy.master").error("Unhandled error in task.")
        content = self._read_log()
        self.assertIn("Unhandled error in task.", content)
        self.assertIn("mitmproxy.master", content)
        # 转发进的是同一批 sink：环形缓冲（UI 回填源）也得有
        ring = ferret_log._handler
        assert ring is not None
        names = [r.name for r in ring.recent()]
        self.assertIn("mitmproxy.master", names)

    def test_upstream_warning_reaches_ferret_log(self) -> None:
        logging.getLogger("mitmproxy.addons.tlsconfig").warning("tls fault")
        self.assertIn("tls fault", self._read_log())

    def test_upstream_info_stays_out_even_if_root_level_drops(self) -> None:
        # 级别闸钉在转发器上：即便有人把根调到 INFO（basicConfig 之类），
        # 第三方 INFO 噪音也不进 ferret.log。
        self._root.setLevel(logging.INFO)
        logging.getLogger("mitmproxy.proxy.mode_servers").info(
            "regular listening at 127.0.0.1:8080"
        )
        logging.getLogger("hpack").info("hpack noise")
        content = self._read_log()
        self.assertNotIn("regular listening", content)
        self.assertNotIn("hpack noise", content)

    def test_ferret_tree_is_not_forwarded_twice(self) -> None:
        # ferret 树 propagate=False：自身记录只走自己的 handler，不经根再转发一遍
        ferret_log.get_logger("mitmproxy").error("own record")
        self.assertEqual(self._read_log().count("own record"), 1)

    def test_root_logger_records_are_forwarded(self) -> None:
        # 上游废弃 API（ctx.log.error）直奔根 logger，同样要被捞住
        logging.getLogger().error("legacy ctx.log error")
        self.assertIn("legacy ctx.log error", self._read_log())


if __name__ == "__main__":
    unittest.main()
