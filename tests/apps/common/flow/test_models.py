import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gzip

from cryptography import x509
from mitmproxy import certs
from mitmproxy.http import Headers
from mitmproxy.test import tflow
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from ferret.apps.common.flow.models import (
    DURATION_MS_ROLE,
    FULL_URL_ROLE,
    MIME_ROLE,
    SIZE_BYTES_ROLE,
    SORT_ROLE,
    STATUS_KIND_ROLE,
    FlowProxyModel,
    FlowTableModel,
    format_duration,
    head_size,
    wire_size,
)


class FlowTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def completed_flow(*, duration_ms=128, content_type="application/json"):
        flow = tflow.tflow(resp=True)
        flow.request.timestamp_start = 100.0
        flow.response.timestamp_end = 100.0 + duration_ms / 1000  # type: ignore
        flow.response.headers["Content-Type"] = content_type  # type: ignore
        flow.request.raw_content = b"req"
        flow.response.raw_content = b"response"  # type: ignore
        return flow

    def model_with(self, *flows) -> FlowTableModel:
        model = FlowTableModel(None)  # type: ignore
        model._rows = list(flows)
        return model

    def test_columns_and_semantic_roles(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)

        self.assertEqual(
            model.HEADERS, ("#", "Method", "URL", "Status", "Type", "Size", "Time")
        )
        self.assertEqual(model.data(model.index(0, 2)), flow.request.pretty_url)
        self.assertEqual(model.data(model.index(0, 3)), 200)
        self.assertEqual(model.data(model.index(0, 4)), "JSON")
        self.assertEqual(model.data(model.index(0, 5)), "11b")
        self.assertEqual(model.data(model.index(0, 6)), "128 ms")
        self.assertEqual(model.data(model.index(0, 3), STATUS_KIND_ROLE), "success")
        self.assertEqual(
            model.data(model.index(0, 2), FULL_URL_ROLE), flow.request.pretty_url
        )
        self.assertEqual(model.data(model.index(0, 4), MIME_ROLE), "application/json")
        self.assertAlmostEqual(model.data(model.index(0, 6), DURATION_MS_ROLE), 128)
        self.assertEqual(model.data(model.index(0, 5), SIZE_BYTES_ROLE), 11)

    def test_pending_and_error_states_keep_text(self) -> None:
        pending = tflow.tflow()
        error = tflow.tflow(err=True)
        model = self.model_with(pending, error)

        self.assertEqual(model.data(model.index(0, 3)), "Pending")
        self.assertEqual(model.data(model.index(0, 3), STATUS_KIND_ROLE), "pending")
        self.assertEqual(model.data(model.index(1, 3)), "Error")
        self.assertEqual(model.data(model.index(1, 3), STATUS_KIND_ROLE), "error")

    def test_duration_formats_boundaries(self) -> None:
        self.assertEqual(format_duration(0.2), "< 1 ms")
        self.assertEqual(format_duration(128), "128 ms")
        self.assertEqual(format_duration(1420), "1.42 s")

    def test_sort_uses_numeric_duration(self) -> None:
        slow = self.completed_flow(duration_ms=1200)
        fast = self.completed_flow(duration_ms=90)
        model = self.model_with(slow, fast)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(6, Qt.SortOrder.AscendingOrder)

        first = proxy.index(0, 6)
        self.assertAlmostEqual(first.data(DURATION_MS_ROLE), 90)
        self.assertIsInstance(first.data(SORT_ROLE), float)

    def test_proxy_row_numbers_stay_contiguous_after_sorting(self) -> None:
        flows = [
            self.completed_flow(duration_ms=duration) for duration in (300, 100, 200)
        ]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)
        proxy.sort(6, Qt.SortOrder.AscendingOrder)

        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [2, 3, 1],
        )

    def test_number_column_sorts_by_stable_sequence(self) -> None:
        flows = [self.completed_flow(duration_ms=value) for value in (300, 100, 200)]
        model = self.model_with(*flows)
        proxy = FlowProxyModel(None)  # type: ignore
        proxy.setSourceModel(model)

        proxy.sort(0, Qt.SortOrder.DescendingOrder)
        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [3, 2, 1],
        )

        proxy.sort(0, Qt.SortOrder.AscendingOrder)
        self.assertEqual(
            [proxy.data(proxy.index(row, 0)) for row in range(3)],
            [1, 2, 3],
        )

    def test_url_combines_host_and_path(self) -> None:
        flow = self.completed_flow()
        model = self.model_with(flow)
        self.assertEqual(model.data(model.index(0, 2)), flow.request.pretty_url)

    def test_horizontal_headers_are_left_aligned(self) -> None:
        model = FlowTableModel(None)  # type: ignore
        expected = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        for column in range(model.columnCount()):
            self.assertEqual(
                model.headerData(
                    column,
                    Qt.Orientation.Horizontal,
                    Qt.ItemDataRole.TextAlignmentRole,
                ),
                expected,
            )

    def test_cell_alignment_unchanged(self) -> None:
        # 单元格对齐不得因表头左对齐改动而改变
        flow = self.completed_flow()
        model = self.model_with(flow)

        # 第 0 列 (#) 单元格右对齐
        self.assertEqual(
            model.data(model.index(0, 0), Qt.ItemDataRole.TextAlignmentRole),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
        )
        # 第 1 列 (Method) 单元格居中
        self.assertEqual(
            model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole),
            int(Qt.AlignmentFlag.AlignCenter),
        )
        # 第 2 列 (URL) 单元格默认左对齐（模型未指定，返回 None）
        self.assertIsNone(
            model.data(model.index(0, 2), Qt.ItemDataRole.TextAlignmentRole),
        )


class RowDataTests(unittest.TestCase):
    """详情字典的产出：证书、双向 TLS、Cookie 属性、trailers、四种大小口径。

    这些是「界面显示了 12 行字面 ``-``」「表格说 900b 详情说 4.0k」两类毛病的源头 ——
    毛病都在产出这一侧，所以钉在这里，而不是钉在某个控件的渲染结果上。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def row_data(flow) -> dict:
        model = FlowTableModel(None)  # type: ignore
        model._rows = [flow]
        return model.get_row_data(0)

    @classmethod
    def certificate(cls):
        """真造一张证书，而不是拿假对象糊 —— 要验的正是「从真证书上读得出来」。"""
        tmp = tempfile.mkdtemp()
        certs.CertStore.create_store(
            Path(tmp), "mitmproxy", 2048, organization="mitmproxy", cn="mitmproxy CA"
        )
        store = certs.CertStore.from_store(Path(tmp), "mitmproxy", 2048)
        entry = store.get_cert(
            "example.com",
            [x509.DNSName("example.com"), x509.DNSName("www.example.com")],
            "Example Org",
        )
        return entry.cert, list(entry.chain_certs)

    def test_the_certificate_group_reports_what_the_certificate_says(self) -> None:
        """改造前 models 只写有效期两项，主体/签发者十二项一个都不产出。"""
        cert, chain = self.certificate()
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = [cert, *chain]
        data = self.row_data(flow)

        self.assertEqual(data["Subject Common Name"], "example.com")
        self.assertEqual(data["Subject Organization"], "Example Org")
        self.assertEqual(data["Issuer Common Name"], "mitmproxy CA")
        self.assertEqual(data["Issuer Organization"], "mitmproxy")
        self.assertEqual(data["Certificate Key"], "RSA 2048")
        self.assertEqual(data["Certificate Expired"], "false")
        self.assertEqual(data["Certificate Is CA"], "false")
        self.assertEqual(
            data["Certificate Alt Names"], ["example.com", "www.example.com"]
        )
        self.assertEqual(data["Certificate Chain Depth"], 1 + len(chain))
        self.assertEqual(
            data["Not Before"], cert.notbefore.strftime("%Y-%m-%d %H:%M:%S.000")
        )

    def test_the_fingerprint_key_says_the_algorithm_it_actually_holds(self) -> None:
        """`Cert.fingerprint()` 给的是 SHA-256（32 字节）。

        键名历来写作 `Fingerprint SHA1` —— 因为从来没有值，名字错了也没人发现。
        """
        cert, chain = self.certificate()
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = [cert, *chain]
        data = self.row_data(flow)

        self.assertNotIn("Fingerprint SHA1", data)
        digest = data["Fingerprint SHA256"]
        self.assertEqual(digest, cert.fingerprint().hex(":").upper())
        self.assertEqual(len(digest.split(":")), 32)

    def test_a_flow_without_a_certificate_produces_no_certificate_keys(self) -> None:
        """没证书就一个键都不写 —— 界面那侧靠「没有这个键」决定整组不显示。"""
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = []
        data = self.row_data(flow)

        for key in ("Subject Common Name", "Not Before", "Fingerprint SHA256"):
            self.assertNotIn(key, data)

    def test_both_sides_of_the_tls_handshake_are_reported(self) -> None:
        """客户端一侧历来完全看不到，只有服务端那六项。"""
        flow = tflow.tflow(resp=True)
        data = self.row_data(flow)

        self.assertEqual(data["TLS Version"], flow.server_conn.tls_version)
        self.assertEqual(data["Client TLS Version"], flow.client_conn.tls_version)
        self.assertEqual(data["Client TLS SNI"], flow.client_conn.sni)
        self.assertEqual(
            data["Client Proxy Mode"], flow.client_conn.proxy_mode.full_spec
        )

    def test_a_connection_without_tls_produces_no_tls_keys(self) -> None:
        flow = tflow.tflow(resp=True)
        # `tls_established` 是只读派生属性，清掉握手时间戳才是「没走 TLS」。
        flow.client_conn.timestamp_tls_setup = None
        flow.server_conn.timestamp_tls_setup = None
        data = self.row_data(flow)

        self.assertNotIn("TLS Version", data)
        self.assertNotIn("Client TLS Version", data)

    def test_response_cookies_keep_their_attributes_and_duplicates(self) -> None:
        """`Set-Cookie` 允许同名重复，压成字典会把后一条盖掉，属性也整个丢了。"""
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers.add("Set-Cookie", "sid=1; Path=/; HttpOnly")
        flow.response.headers.add("Set-Cookie", "sid=2; Path=/admin")
        cookies = self.row_data(flow)["Response Cookies"]

        self.assertEqual([c["name"] for c in cookies], ["sid", "sid"])
        self.assertEqual([c["value"] for c in cookies], ["1", "2"])
        self.assertEqual(cookies[0]["attrs"], {"Path": "/", "HttpOnly": ""})
        self.assertEqual(cookies[1]["attrs"], {"Path": "/admin"})

    def test_trailers_show_up_only_when_the_message_has_them(self) -> None:
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        self.assertNotIn("Response Trailers", self.row_data(flow))

        flow.response.trailers = Headers([(b"x-checksum", b"deadbeef")])
        self.assertEqual(
            self.row_data(flow)["Response Trailers"], {"x-checksum": "deadbeef"}
        )

    def test_header_bytes_are_the_real_wire_head_not_a_python_repr(self) -> None:
        """改造前量的是 ``len(str(headers))`` —— `multidict.__repr__` 的长度。

        那串东西含引号、``b`` 前缀和 ``Headers[...]`` 外壳，又缺请求行和结尾空行，
        和线上字节没有任何关系。
        """
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        data = self.row_data(flow)

        self.assertEqual(data["req_headers_size"], head_size(flow.request))
        self.assertEqual(data["res_headers_size"], head_size(flow.response))
        # 真实头部含请求行且以 CRLF 空行收尾 —— repr 两样都没有，所以两个数字不相等。
        self.assertNotEqual(data["req_headers_size"], len(str(flow.request.headers)))
        self.assertGreater(data["req_headers_size"], 0)

    def test_the_wire_and_decoded_sizes_are_two_separate_numbers(self) -> None:
        """gzip 响应上，「线上」和「解压后」差好几倍，混成一个数就谁也说不清。"""
        payload = b"x" * 4096
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        compressed = gzip.compress(payload)
        flow.response.raw_content = compressed
        data = self.row_data(flow)

        self.assertEqual(data["res_wire_size"], len(compressed))
        self.assertEqual(data["res_decoded_size"], len(payload))
        self.assertLess(data["res_wire_size"], data["res_decoded_size"])

    def test_the_table_size_column_and_the_detail_wire_rows_agree(self) -> None:
        """两处数字必须一致 —— 而且是结构上一致：同走 `wire_size()` 一个函数。

        改造前表格量压缩后、详情量解压后，同一条 gzip 响应两处能差好几倍，
        用户没法判断哪个是真的。
        """
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"y" * 2048)
        model = FlowTableModel(None)  # type: ignore
        model._rows = [flow]
        data = model.get_row_data(0)

        column = model.data(model.index(0, 5), SIZE_BYTES_ROLE)
        self.assertEqual(data["req_wire_size"] + data["res_wire_size"], column)

    def test_the_totals_add_the_headers_to_the_wire_bytes(self) -> None:
        flow = tflow.tflow(resp=True)
        data = self.row_data(flow)

        self.assertEqual(
            data["req_total_size"], data["req_headers_size"] + data["req_wire_size"]
        )
        self.assertEqual(
            data["res_total_size"], data["res_headers_size"] + data["res_wire_size"]
        )
        self.assertEqual(
            data["total_size"], data["req_total_size"] + data["res_total_size"]
        )

    def test_a_request_only_flow_still_totals_its_request(self) -> None:
        """只抓到请求时早先显示「请求 384b / 合计 0b」，两个数字自己打自己。"""
        flow = tflow.tflow()
        data = self.row_data(flow)

        self.assertGreater(data["req_total_size"], 0)
        self.assertEqual(data["total_size"], data["req_total_size"])
        self.assertNotIn("res_wire_size", data)

    def test_the_flow_id_no_longer_squats_on_the_connection_key(self) -> None:
        """`Connection ID` 历来存的是 `flow.id`，真正的连接 id 无处可放。"""
        flow = tflow.tflow(resp=True)
        data = self.row_data(flow)

        self.assertEqual(data["Flow ID"], flow.id)
        self.assertEqual(data["Connection ID"], flow.client_conn.id)
        self.assertEqual(data["Back Connection ID"], flow.server_conn.id)
        self.assertNotEqual(data["Connection ID"], flow.id)

    def test_wire_size_treats_a_missing_message_as_zero(self) -> None:
        self.assertEqual(wire_size(None), 0)

    def test_the_size_tooltip_states_the_caliber(self) -> None:
        """列宽只放得下一个总数，口径得靠 tooltip 说清。"""
        flow = tflow.tflow(resp=True)
        model = FlowTableModel(None)  # type: ignore
        model._rows = [flow]
        tooltip = model.data(model.index(0, 5), Qt.ItemDataRole.ToolTipRole)

        self.assertIn("wire", tooltip)
        self.assertIn("Request", tooltip)
        self.assertIn("Response", tooltip)


if __name__ == "__main__":
    unittest.main()
