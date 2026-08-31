"""`FerretSseAddon`：tee 层 —— 检测、逐块解析、body 补回、内存闸。

mitmproxy 对 stream callable 的调用约定（`proxy/layers/http/__init__.py`：每个
chunk 过一趟 callable、转发其返回值，流末以空 chunk 收尾）就是这里模拟的节奏 ——
不起代理，构造 flow 后手动拨 `flow.response.stream`，与 `test_ws_bridge.py` 直接调
钩子是同一个办法。

钉住的契约：

* **检测与非 SSE 零影响**：`responseheaders` 里 content-type 命中才换 callable，
  其余流量 `stream` 原样是 `False`，一个信号都不发。
* **过界的只有 flow_id + 值对象**（AGENTS.md §3 红线）：tee 跑在 mitm 线程上，
  发 flow 过去等于让 Qt 侧读活 flow。
* **事件实时逐块吐**：第一块凑齐就 emit，攒到流末才发的话 tee 等于白做 ——
  那正是原生 `store_streamed_bodies` 的死穴。
* **流末补回 body**：响应体页 / 保存 / HAR 导出都读 `response.data.content`。
* **内存闸**：buf 超 `SSE_BODY_LIMIT` 停止增长（留前缀），解析推送继续 ——
  几小时寿命的端点全攒着就是给内核埋内存炸弹。
* **存档恒存全量**（`sse_ended` 之后详情页还会整取一次），`forget` / `clear`
  随 View 的删/清走。
"""

import unittest
from unittest.mock import patch

from mitmproxy.http import Response
from mitmproxy.test import tflow
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import (
    FerretSseAddon,
    HTTPFlow,
    MitmRuntime,
    SseEvent,
    parse_sse,
)


def response_of(flow: HTTPFlow) -> Response:
    """tflow(resp=True) 造的流量必有响应；断言只为类型收窄。"""
    response = flow.response
    assert response is not None
    return response


def sse_flow(content_type: str = "text/event-stream") -> HTTPFlow:
    """一条响应头已到、body 待流式转发的事件流流量。"""
    flow = tflow.tflow(resp=True)
    response_of(flow).headers["content-type"] = content_type
    return flow


def pump(flow: HTTPFlow, chunks: list[bytes]) -> None:
    """按 mitmproxy 的约定拨 stream callable：chunk 逐段、空 chunk 收尾。"""
    stream = response_of(flow).stream
    assert callable(stream)
    for chunk in chunks:
        stream(chunk)
    stream(b"")


class FerretSseAddonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self) -> None:
        self.runtime = MitmRuntime()
        self.addon = FerretSseAddon(self.runtime)
        self.started: list[tuple] = []
        self.events: list[tuple] = []
        self.ended: list[tuple] = []
        self.runtime.sse_started.connect(lambda *a: self.started.append(a))
        self.runtime.sse_event.connect(lambda *a: self.events.append(a))
        self.runtime.sse_ended.connect(lambda *a: self.ended.append(a))

    # —— 检测 ——

    def test_an_event_stream_gets_a_callable_and_announces_itself(self) -> None:
        flow = sse_flow()
        self.addon.responseheaders(flow)

        self.assertTrue(callable(response_of(flow).stream))
        self.assertEqual(self.started, [(flow.id,)])

    def test_a_parameterised_content_type_still_counts(self) -> None:
        flow = sse_flow("text/event-stream; charset=utf-8")
        self.addon.responseheaders(flow)
        self.assertTrue(callable(response_of(flow).stream))

    def test_a_non_event_stream_is_untouched(self) -> None:
        """非 SSE 流量零影响：不换 callable、不发信号、不留存档。"""
        flow = sse_flow("application/json")
        self.addon.responseheaders(flow)

        self.assertFalse(callable(response_of(flow).stream))
        self.assertEqual(self.started, [])
        self.assertEqual(self.addon.events(flow.id), [])

    # —— tee 与补回 ——

    def test_tee_events_match_parse_sse_and_the_body_comes_back(self) -> None:
        """跨块切分（行中 / CRLF 中 / 块中）不能丢也不能多。"""
        body = ': ok\r\n\r\ndata: {"a": 1}\r\n\r\ndata: tail'
        chunks = [
            b": ok\r",
            b"\n\r",
            b'\ndata: {"a',
            b'": 1}\r',
            b"\n\r\n",
            b"data: tail",
        ]
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, chunks)

        self.assertEqual(self.addon.events(flow.id), parse_sse(body))
        # body 补回的是原始字节，不是解码后的文本。
        self.assertEqual(response_of(flow).data.content, body.encode())
        self.assertEqual(self.ended, [(flow.id,)])

    def test_chunks_pass_through_untouched(self) -> None:
        """callable 是 tee 不是变换：转发路径拿到的必须还是原 chunk。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        stream = response_of(flow).stream
        assert callable(stream)

        for chunk in (b"data: a\n\n", b"data: b"):
            self.assertEqual(stream(chunk), chunk)
        self.assertEqual(stream(b""), b"")

    def test_events_are_emitted_as_they_arrive(self) -> None:
        """第一块凑齐就过界 —— 攒到流末才发，tee 就白做了。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        stream = response_of(flow).stream
        assert callable(stream)

        stream(b"data: first\n\n")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1].data, "first")

        stream(b"data: second\n\n")
        self.assertEqual(len(self.events), 2)
        # 流末空 chunk 之后才收尾，中途没有 ended。
        self.assertEqual(self.ended, [])
        stream(b"")
        self.assertEqual(self.ended, [(flow.id,)])

    def test_the_empty_chunk_flushes_the_unterminated_tail(self) -> None:
        """流末没凑满一块的残余也要派发 —— 少一条就是凭空丢数据。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: tail"])

        self.assertEqual([e.data for e in self.addon.events(flow.id)], ["tail"])
        self.assertEqual(self.ended, [(flow.id,)])

    def test_a_heartbeat_is_teed_like_any_other_event(self) -> None:
        """addon 不筛选心跳（收成系统条是界面的事）—— 这里只管如实送出。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b": keep-alive\n\n"])

        self.assertEqual(self.events[0][1].comment, "keep-alive")
        self.assertEqual(self.events[0][1].data, "")

    # —— 红线与存档 ——

    def test_nothing_but_values_crosses_the_boundary(self) -> None:
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: x\n\n"])

        payloads = [
            arg for args in (*self.started, *self.events, *self.ended) for arg in args
        ]
        self.assertTrue(payloads)
        for payload in payloads:
            with self.subTest(payload=type(payload).__name__):
                self.assertIsInstance(payload, str | SseEvent)
                self.assertFalse(hasattr(payload, "request"))
                self.assertFalse(hasattr(payload, "response"))

    def test_the_archive_survives_the_end_of_the_stream(self) -> None:
        """`sse_ended` 触发的详情刷新还要整取一次 —— 收尾不许顺手清档。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: a\n\ndata: b\n\n"])

        self.assertEqual([e.data for e in self.addon.events(flow.id)], ["a", "b"])

    def test_events_returns_a_copy(self) -> None:
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: a\n\n"])

        archive = self.addon.events(flow.id)
        archive.clear()
        self.assertEqual(len(self.addon.events(flow.id)), 1)

    def test_two_flows_keep_separate_archives(self) -> None:
        first, second = sse_flow(), sse_flow()
        self.addon.responseheaders(first)
        self.addon.responseheaders(second)
        stream_a, stream_b = response_of(first).stream, response_of(second).stream
        assert callable(stream_a) and callable(stream_b)

        stream_a(b"data: a\n\n")
        stream_b(b"data: b\n\n")
        stream_a(b"data: a2\n\n")
        stream_a(b"")
        stream_b(b"")

        self.assertEqual([e.data for e in self.addon.events(first.id)], ["a", "a2"])
        self.assertEqual([e.data for e in self.addon.events(second.id)], ["b"])
        self.assertEqual(
            [fid for fid, _ in self.events], [first.id, second.id, first.id]
        )

    def test_forget_drops_the_archive_and_survives_late_chunks(self) -> None:
        """行删掉之后 chunk 才到（抢在一起）不许炸：没有存档就静默丢。"""
        flow = sse_flow()
        self.addon.responseheaders(flow)
        stream = response_of(flow).stream
        assert callable(stream)
        stream(b"data: a\n\n")

        self.addon.forget(flow.id)
        self.assertEqual(self.addon.events(flow.id), [])

        stream(b"data: late\n\n")
        stream(b"")
        self.assertEqual(self.addon.events(flow.id), [])

    def test_clear_wipes_every_flow(self) -> None:
        flows = [sse_flow() for _ in range(3)]
        for flow in flows:
            self.addon.responseheaders(flow)
            pump(flow, [b"data: x\n\n"])

        self.addon.clear()
        for flow in flows:
            self.assertEqual(self.addon.events(flow.id), [])

    # —— 内存闸与编码 ——

    def test_the_body_buffer_stops_growing_past_the_limit(self) -> None:
        """超限后 buf 留前缀、解析推送继续、事件一个不少。"""
        flow = sse_flow()
        with patch("ferret.core.mitm.sse.SSE_BODY_LIMIT", 8):
            self.addon.responseheaders(flow)
            stream = response_of(flow).stream
            assert callable(stream)
            stream(b"data: a")  # 7 字节，限内
            stream(b"\n\n")  # 再来就超了：buf 冻结，解析照常
            stream(b"data: tail")
            stream(b"")

        self.assertEqual(response_of(flow).data.content, b"data: a")
        self.assertEqual([e.data for e in self.addon.events(flow.id)], ["a", "tail"])

    def test_a_non_utf8_charset_is_honoured(self) -> None:
        flow = sse_flow("text/event-stream; charset=gbk")
        self.addon.responseheaders(flow)
        pump(flow, ["data: 股价\n\n".encode("gbk")])

        self.assertEqual(self.events[0][1].data, "股价")

    def test_a_malformed_charset_falls_back_to_utf8(self) -> None:
        """服务器自报的编码名可以是任何乱码 —— 退回宽松解，别让整条流消失。"""
        flow = sse_flow("text/event-stream; charset=nonexistent-charset")
        self.addon.responseheaders(flow)
        pump(flow, ["data: café\n\n".encode()])

        self.assertEqual(self.events[0][1].data, "café")


class BridgelessAddonTests(unittest.TestCase):
    """bridge 未注入（master 装配早于 runtime）：推送关掉，存档照常。"""

    def setUp(self) -> None:
        self.addon = FerretSseAddon()

    def test_archiving_works_without_a_bridge(self) -> None:
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: a\n\ndata: b\n\n"])

        self.assertEqual([e.data for e in self.addon.events(flow.id)], ["a", "b"])

    def test_the_body_still_comes_back_without_a_bridge(self) -> None:
        flow = sse_flow()
        self.addon.responseheaders(flow)
        pump(flow, [b"data: a\n\n"])

        self.assertEqual(response_of(flow).data.content, b"data: a\n\n")


if __name__ == "__main__":
    unittest.main()
