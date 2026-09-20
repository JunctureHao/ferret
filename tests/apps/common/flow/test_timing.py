"""时序瀑布的推导：`phases()` 是纯函数，七种情形矩阵全部不起窗口直测。

段值与泳道归属的口径全部对着 mitmproxy 12.2.3 的真实计时能力裁
（出处 `.plans/timing-waterfall.md` §3）：

- eager 下 HTTPS 的连接/TLS 早于请求 ⇒ ``lane="pre"``；明文与 compose 回放的
  连接嵌在等待里 ⇒ ``lane="nested"``。两种情形下总耗时都是**一次减法**
  （``p1 - q0``），绝不因嵌套段或请求前段变大 —— 原生 HAR 的 ``"time"``
  求和就是这么错的，这里逐情形钉死；
- DNS 没有独立计时（getaddrinfo 与 TCP 握手包在同一个 await 里），
  「DNS + 连接」是合成段；目标是 IP 字面量才敢说「这段不含 DNS」；
- ``st``（TCP 握手完成）只有 TCP 通道才写，UDP/QUIC 下连接段与服务端 TLS
  段（分母是它）一起缺席，不拿别的时刻冒充。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.timing import Phase, TimingModel, phases


def detail(
    *,
    c0=None,
    ct=None,
    s0=None,
    st=None,
    ss=None,
    q0=None,
    q1=None,
    p0=None,
    p1=None,
    address=None,
) -> dict:
    """按 `phases()` 的取值别名拼一份详情字典（缺省即缺键）。"""
    data = {
        "Front Connection Start": c0,
        "Front TLS Handshake": ct,
        "Back Connection Start": s0,
        "Back TCP Handshake": st,
        "Back TLS Handshake": ss,
        "req_time": q0,
        "req_timestamp_end": q1,
        "res_timestamp_start": p0,
        "res_time": p1,
    }
    if address is not None:
        data["Back Address"] = address
    return {key: value for key, value in data.items() if value is not None}


def by_key(model: TimingModel, key: str) -> Phase:
    """段 key → Phase。不存在就当场断言失败 —— 测试里「该有却没有」本身就是失败；
    返回值不带 None，后续 `.lane` / `.ms` 不用再判空（ty 也能收窄）。"""
    found = [phase for phase in model.phases if phase.key == key]
    assert len(found) == 1, f"{key} 出现了 {len(found)} 次"
    return found[0]


def phase_keys(model: TimingModel) -> set[str]:
    return {phase.key for phase in model.phases}


class PhasesTests(unittest.TestCase):
    def test_https_eager_puts_connection_phases_before_the_request(self) -> None:
        """连接三段全在请求前；总耗时是一次减法，≠ 各段之和（钉死 HAR 双记缺陷）。"""
        model = phases(
            detail(
                c0=100.0,
                ct=100.0124,
                s0=100.0,
                st=100.0861,
                ss=100.1501,
                q0=100.2030,
                q1=100.2042,
                p0=100.5832,
                p1=100.6150,
            )
        )

        for key in ("client_tls", "connect", "tls"):
            self.assertEqual(by_key(model, key).lane, "pre", key)
        for key in ("send", "wait", "receive"):
            self.assertEqual(by_key(model, key).lane, "main", key)

        # 总耗时 = p1 - q0，与表格 Time 列同一条减法。
        assert model.total_ms is not None
        assert model.ttfb_ms is not None
        self.assertAlmostEqual(model.total_ms, 412.0)
        self.assertAlmostEqual(model.ttfb_ms, 380.2)
        # 请求前块的提前量：q0 - 最后一个 pre 段的结束。
        assert model.pre_delta_ms is not None
        self.assertAlmostEqual(model.pre_delta_ms, (100.2030 - 100.1501) * 1000)
        # 各段之和把请求前的连接成本也算进去 —— 必然大于总耗时，绝不能求和。
        phase_sum = sum(phase.ms for phase in model.phases)
        self.assertGreater(phase_sum, model.total_ms)

    def test_plaintext_http_nests_the_connection_inside_the_wait(self) -> None:
        """明文下连接在请求转发时才拨：连接段 lane="nested"，总耗时不多算一遍。"""
        model = phases(
            detail(
                q0=100.0,
                q1=100.0012,
                s0=100.0050,
                st=100.0911,
                ss=100.1551,
                p0=100.3802,
                p1=100.4120,
            )
        )

        self.assertEqual(by_key(model, "connect").lane, "nested")
        self.assertEqual(by_key(model, "tls").lane, "nested")
        self.assertEqual(model.lane("pre"), [])
        # 总耗时仍是 p1 - q0 一次减法：连接段嵌在等待之内，不再加一遍。
        assert model.total_ms is not None
        self.assertAlmostEqual(model.total_ms, 412.0)
        self.assertAlmostEqual(by_key(model, "wait").ms, 379.0)
        self.assertAlmostEqual(by_key(model, "connect").ms, 86.1)
        self.assertAlmostEqual(by_key(model, "tls").ms, 64.0)
        self.assertGreater(sum(phase.ms for phase in model.phases), model.total_ms)

    def test_a_compose_replay_reports_a_zero_send_phase_not_a_missing_one(self) -> None:
        """回放把 `request.timestamp_start = timestamp_end` 写成同一个值 ——
        发送段是 0 ms，不是「测不到」。"""
        model = phases(
            detail(
                q0=100.0,
                q1=100.0,
                s0=100.0,
                st=100.0861,
                ss=100.1501,
                p0=100.4,
                p1=100.412,
            )
        )

        send = by_key(model, "send")
        self.assertEqual(send.ms, 0.0)

    def test_a_flow_without_a_server_connection_omits_the_connection_group(self) -> None:
        """`s0 is None`（网关拦下 / 未发出）：连接三段缺席，请求三段照常。"""
        model = phases(
            detail(
                c0=100.0,
                ct=100.0124,
                q0=100.2,
                q1=100.2012,
                p0=100.58,
                p1=100.615,
            )
        )

        for key in ("connect", "tls"):
            self.assertNotIn(key, phase_keys(model))
        for key in ("client_tls", "send", "wait", "receive"):
            self.assertIn(key, phase_keys(model))
        assert model.total_ms is not None
        self.assertAlmostEqual(model.total_ms, 415.0)

    def test_a_udp_channel_reports_no_connect_and_no_tls_phase(self) -> None:
        """`st is None`（UDP/QUIC，上游只对 TCP 写 timestamp_tcp_setup）：
        连接段与服务端 TLS 段（分母是 st）一起缺席，不抛异常。"""
        model = phases(
            detail(
                s0=100.0,
                ss=100.1501,
                q0=100.203,
                q1=100.2042,
                p0=100.5832,
                p1=100.615,
            )
        )

        self.assertEqual(phase_keys(model), {"send", "wait", "receive"})
        assert model.total_ms is not None
        self.assertAlmostEqual(model.total_ms, 412.0)

    def test_an_unfinished_response_still_reports_the_ttfb(self) -> None:
        """响应未收完（SSE/流式，p1 is None）：接收段与总耗时缺席，首字节仍可得。"""
        model = phases(
            detail(
                q0=100.0,
                q1=100.0012,
                p0=100.3802,
            )
        )

        self.assertNotIn("receive", phase_keys(model))
        self.assertIsNone(model.total_ms)
        assert model.ttfb_ms is not None
        self.assertAlmostEqual(model.ttfb_ms, 380.2)
        self.assertIn("wait", phase_keys(model))

    def test_an_empty_dict_produces_an_all_none_model(self) -> None:
        """详情面板先于数据构造，`phases({})` 必须安静。"""
        model = phases({})

        self.assertEqual(model.phases, ())
        self.assertIsNone(model.total_ms)
        self.assertIsNone(model.ttfb_ms)
        self.assertIsNone(model.pre_delta_ms)
        self.assertIsNone(model.target_is_ip)


class TargetIpTests(unittest.TestCase):
    """「DNS + 连接」是合成段 —— 只有目标是 IP 字面量时才敢说「不含 DNS」。"""

    def test_an_ip_literal_target_has_no_dns_in_the_connect_phase(self) -> None:
        for address in ("1.2.3.4:443", "[::1]:443"):
            with self.subTest(address=address):
                model = phases(detail(address=address))
                self.assertIs(model.target_is_ip, True)

    def test_a_domain_target_may_contain_dns(self) -> None:
        model = phases(detail(address="example.com:443"))
        self.assertIs(model.target_is_ip, False)

    def test_a_missing_target_says_nothing(self) -> None:
        model = phases(detail())
        self.assertIsNone(model.target_is_ip)


class TimingPaneTests(unittest.TestCase):
    """控件冒烟：「时序」组 lead 块的显隐与结构。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_the_pane_hides_itself_when_there_is_nothing_to_say(self) -> None:
        """空字典（面板先于数据构造）/ 一个时间戳都没有：整块让位；
        有数据时回来，且不带自己的组头（卡片提供）与重复的「时刻明细」卡。"""
        from ferret.apps.common.flow.timing import TimingPane

        pane = TimingPane()
        pane.set_data({})
        self.assertTrue(pane.isHidden())
        pane.set_data(
            detail(
                c0=100.0,
                ct=100.0124,
                s0=100.0,
                st=100.0861,
                ss=100.1501,
                q0=100.203,
                q1=100.2042,
                p0=100.5832,
                p1=100.615,
                address="example.com:443",
            )
        )
        self.assertFalse(pane.isHidden())
        self.assertFalse(hasattr(pane, "detail_card"))
        self.assertFalse(hasattr(pane, "title_label"))
        pane.deleteLater()


if __name__ == "__main__":
    unittest.main()
