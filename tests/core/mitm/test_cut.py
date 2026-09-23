"""`FerretCutAddon`：大正文边收边截（.plans/1-cut-flow-size.md）。

mitmproxy 对 stream callable 的调用约定（每个 chunk 过一趟 callable、转发其
返回值、流末以空 chunk 收尾）就是这里模拟的节奏 —— 与 `test_sse_addon.py`
同一个办法：不起代理，构造 flow 后手动拨 `flow.response.stream`。

钉住的契约：

* **tee 不是变换**：转发路径拿到的必须还是原 chunk（转发永远完整）。
* **边收边截**：只攒前 N 字节，流末把前缀写回 `response.data.content`，并打
  `ferret.body_truncated` / `ferret.body_full_size` 两个 metadata 键
  （导出确认的计数来源）。`full_size` 是实测字节数，不是 Content-Length 的自报值。
* **让路**：未开开关 / 未超阈值 / 无 Content-Length / SSE 已占 stream 槽 /
  重写就地作答 / 断点命中 → stream 原样、无 metadata。
* **热更不回溯**：`set_options` 换的是下一条流的判定快照，已挂载的 tap 按旧值收完。
* **forget/clear 后的迟到 chunk 不许炸**（行已删、流还在收，抢在一起是常态）。
"""

import socket
import unittest

from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    BODY_FULL_SIZE_KEY,
    BODY_TRUNCATED_KEY,
    DEFAULT_BODY_CUT_SIZE,
    MAX_BODY_CUT_SIZE,
    MIN_BODY_CUT_SIZE,
    REWRITE_ANSWERED_KEY,
    FerretCutAddon,
    HTTPFlow,
    MitmRuntime,
    MitmRuntimeState,
    clamp_body_cut_size,
    truncated_body_count,
)

from ._qt import start_runtime


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def big_flow(content_length: int | None) -> HTTPFlow:
    """一条响应头已到、body 待流式转发的流量；``None`` = 无 Content-Length。"""
    flow = tflow.tflow(resp=True)
    assert flow.response is not None
    if content_length is None:
        flow.response.headers.pop("content-length", None)
    else:
        flow.response.headers["content-length"] = str(content_length)
    return flow


def pump(flow: HTTPFlow, chunks: list[bytes]) -> bytes:
    """按 mitmproxy 的约定拨 stream callable，返回转发侧收到的总字节。"""
    assert flow.response is not None
    stream = flow.response.stream
    assert callable(stream)
    forwarded = bytearray()
    for chunk in chunks:
        piece = stream(chunk)
        # tee 是旁路观察，转发侧拿回的永远是原 chunk（bytes），不是变换。
        assert isinstance(piece, bytes)
        forwarded += piece
    stream(b"")
    return bytes(forwarded)


class ClampTests(unittest.TestCase):
    def test_out_of_range_values_are_clamped(self) -> None:
        self.assertEqual(clamp_body_cut_size(1), MIN_BODY_CUT_SIZE)
        self.assertEqual(clamp_body_cut_size(MAX_BODY_CUT_SIZE * 2), MAX_BODY_CUT_SIZE)

    def test_garbage_falls_back_to_the_default(self) -> None:
        """配置是用户可手改的纯文本，坏值退回默认而不是炸启动。"""
        self.assertEqual(clamp_body_cut_size("junk"), DEFAULT_BODY_CUT_SIZE)
        self.assertEqual(clamp_body_cut_size(None), DEFAULT_BODY_CUT_SIZE)


class FerretCutAddonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addon = FerretCutAddon()
        self.addon.set_options(enabled=True, max_size=8)

    def test_disabled_means_untouched(self) -> None:
        self.addon.set_options(enabled=False, max_size=8)
        flow = big_flow(100)
        self.addon.responseheaders(flow)
        assert flow.response is not None
        self.assertFalse(callable(flow.response.stream))

    def test_over_threshold_keeps_only_the_prefix(self) -> None:
        flow = big_flow(12)
        self.addon.responseheaders(flow)
        forwarded = pump(flow, [b"hello ", b"world", b"!"])

        # 转发侧一字不动；存储侧只有前 8 字节。
        self.assertEqual(forwarded, b"hello world!")
        assert flow.response is not None
        self.assertEqual(flow.response.data.content, b"hello wo")
        self.assertEqual(flow.metadata[BODY_TRUNCATED_KEY], True)
        # full_size 是实测收到的字节数，不是 Content-Length 的自报值。
        self.assertEqual(flow.metadata[BODY_FULL_SIZE_KEY], 12)

    def test_a_lying_content_length_still_reports_the_real_size(self) -> None:
        """服务器少发（连接中断）：自报 100 实收 12，metadata 记 12。"""
        flow = big_flow(100)
        self.addon.responseheaders(flow)
        pump(flow, [b"hello world!"])

        self.assertEqual(flow.metadata[BODY_FULL_SIZE_KEY], 12)

    def test_under_threshold_is_untouched(self) -> None:
        flow = big_flow(8)
        self.addon.responseheaders(flow)
        assert flow.response is not None
        self.assertFalse(callable(flow.response.stream))
        self.assertNotIn(BODY_TRUNCATED_KEY, flow.metadata)

    def test_a_missing_content_length_is_untouched(self) -> None:
        """未知长度的流式 body 不截（计划 §2：留给后续）。"""
        flow = big_flow(None)
        self.addon.responseheaders(flow)
        assert flow.response is not None
        self.assertFalse(callable(flow.response.stream))

    def test_a_malformed_content_length_is_untouched(self) -> None:
        flow = big_flow(0)
        assert flow.response is not None
        flow.response.headers["content-length"] = "not-a-number"
        self.addon.responseheaders(flow)
        self.assertFalse(callable(flow.response.stream))

    def test_an_occupied_stream_slot_is_left_alone(self) -> None:
        """SSE 的 tee 已占槽（链上 SSE 在 cut 之前）：一个槽位养不起两个 tee。"""
        flow = big_flow(100)
        assert flow.response is not None
        sentinel = lambda chunk: chunk
        flow.response.stream = sentinel
        self.addon.responseheaders(flow)
        self.assertIs(flow.response.stream, sentinel)

    def test_a_rewrite_answered_flow_is_untouched(self) -> None:
        """重写就地作答的静态替换体不是流式语义（与 SSE 同款让路）。"""
        flow = big_flow(100)
        flow.metadata[REWRITE_ANSWERED_KEY] = True
        self.addon.responseheaders(flow)
        assert flow.response is not None
        self.assertFalse(callable(flow.response.stream))

    def test_an_intercepted_flow_is_untouched(self) -> None:
        """断点命中让路：装着 tee 时响应期的 body 编辑既到不了客户端、也会被
        流末写回冲掉 —— 断点本来就要求完整 body。"""
        addon = FerretCutAddon(should_intercept=lambda flow: True)
        addon.set_options(enabled=True, max_size=8)
        flow = big_flow(100)
        addon.responseheaders(flow)
        assert flow.response is not None
        self.assertFalse(callable(flow.response.stream))

    def test_a_hot_update_applies_to_new_flows_only(self) -> None:
        """已挂载的 tap 按旧阈值收完（快照在 responseheaders 取定），新流按新值。"""
        old = big_flow(100)
        self.addon.responseheaders(old)
        self.addon.set_options(enabled=True, max_size=4)
        new = big_flow(100)
        self.addon.responseheaders(new)

        pump(old, [b"hello world!"])
        pump(new, [b"hello world!"])

        assert old.response is not None and new.response is not None
        self.assertEqual(old.response.data.content, b"hello wo")
        self.assertEqual(new.response.data.content, b"hell")

    def test_forget_survives_late_chunks(self) -> None:
        """行删掉之后 chunk 才到（抢在一起）不许炸：没有账就静默丢。"""
        flow = big_flow(100)
        self.addon.responseheaders(flow)
        assert flow.response is not None
        stream = flow.response.stream
        assert callable(stream)

        self.addon.forget(flow.id)
        stream(b"hello ")
        stream(b"world!")
        stream(b"")
        # 没人补回 body、没人打 metadata。
        self.assertNotEqual(flow.response.data.content, b"hello wo")
        self.assertNotIn(BODY_TRUNCATED_KEY, flow.metadata)

    def test_clear_wipes_every_tap(self) -> None:
        flows = [big_flow(100) for _ in range(3)]
        for flow in flows:
            self.addon.responseheaders(flow)
        self.addon.clear()
        self.assertEqual(self.addon._taps, {})
        self.assertEqual(self.addon._flows, {})


class TruncatedBodyCountTests(unittest.TestCase):
    def test_counts_only_marked_flows(self) -> None:
        plain, cut = tflow.tflow(resp=True), tflow.tflow(resp=True)
        cut.metadata[BODY_TRUNCATED_KEY] = True
        self.assertEqual(truncated_body_count([plain, cut]), 1)


class MitmRuntimeBodyCutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_default_is_off(self) -> None:
        runtime = MitmRuntime()
        self.assertFalse(runtime.body_cut_enabled)
        self.assertEqual(runtime.body_cut_size, DEFAULT_BODY_CUT_SIZE)

    def test_the_size_is_clamped_on_the_way_in(self) -> None:
        self.assertEqual(MitmRuntime(body_cut_size=1).body_cut_size, MIN_BODY_CUT_SIZE)

    def test_stored_before_start_and_seeded_into_the_master(self) -> None:
        runtime = MitmRuntime(
            listen_port=free_port(), body_cut_enabled=True, body_cut_size=65536
        )
        self.addCleanup(runtime.stop)
        start_runtime(runtime)
        self.assertEqual(runtime.state, MitmRuntimeState.RUNNING)

        master = runtime._master
        assert master is not None
        # 内核启动前就该带上快照，第一条连接进来时截断必须已经生效。
        self.assertTrue(runtime.call(lambda: master.cut._enabled))
        self.assertEqual(runtime.call(lambda: master.cut._max_size), 65536)

    def test_hot_update_reaches_the_running_master(self) -> None:
        runtime = MitmRuntime(listen_port=free_port())
        self.addCleanup(runtime.stop)
        start_runtime(runtime)

        runtime.apply_body_cut(True, 32768)

        master = runtime._master
        assert master is not None
        self.assertTrue(runtime.call(lambda: master.cut._enabled))
        self.assertEqual(runtime.call(lambda: master.cut._max_size), 32768)

    def test_stored_only_while_stopped(self) -> None:
        """内核没跑时只存不下发，下次启动由 _run_master 补上。"""
        runtime = MitmRuntime()
        runtime.apply_body_cut(True, 32768)
        self.assertTrue(runtime.body_cut_enabled)
        self.assertEqual(runtime.body_cut_size, 32768)
        self.assertEqual(runtime.state, MitmRuntimeState.STOPPED)


if __name__ == "__main__":
    unittest.main()
