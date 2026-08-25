"""详情字典的产出：证书、双向 TLS、Cookie 属性、trailers、四种大小口径。

这些用例原来钉在 `tests/apps/common/flow/test_models.py` 上，随着产出逻辑搬进
`core/mitm/detail.py` 一起搬过来 —— 它们验的从来是「从一条真 flow 上读得出什么」，
和表格模型没关系。

刻意不起 `QApplication`：`QCoreApplication.translate` 是静态方法，没有实例时原样
返回源文本，正好用来验标记本身。译文由 `tests/core/test_i18n.py` 双向盯着。
"""

import gzip
import tempfile
import unittest
import warnings
from pathlib import Path

from cryptography import x509
from mitmproxy import certs
from mitmproxy.http import Headers
from mitmproxy.net import server_spec
from mitmproxy.test import tflow

from ferret.core.mitm import (
    build_flow_detail,
    head_size,
    infer_state,
    wire_size,
)


def certificate():
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


class CertificateFieldTests(unittest.TestCase):
    def test_the_certificate_group_reports_what_the_certificate_says(self) -> None:
        """改造前只写有效期两项，主体/签发者十二项一个都不产出。"""
        cert, chain = certificate()
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = [cert, *chain]
        data = build_flow_detail(flow)

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
        cert, chain = certificate()
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = [cert, *chain]
        data = build_flow_detail(flow)

        self.assertNotIn("Fingerprint SHA1", data)
        digest = data["Fingerprint SHA256"]
        self.assertEqual(digest, cert.fingerprint().hex(":").upper())
        self.assertEqual(len(digest.split(":")), 32)

    def test_a_flow_without_a_certificate_produces_no_certificate_keys(self) -> None:
        """没证书就一个键都不写 —— 界面那侧靠「没有这个键」决定整组不显示。"""
        flow = tflow.tflow(resp=True)
        flow.server_conn.certificate_list = []
        data = build_flow_detail(flow)

        for key in ("Subject Common Name", "Not Before", "Fingerprint SHA256"):
            self.assertNotIn(key, data)


class ConnectionFieldTests(unittest.TestCase):
    def test_both_sides_of_the_tls_handshake_are_reported(self) -> None:
        """客户端一侧历来完全看不到，只有服务端那六项。"""
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

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
        data = build_flow_detail(flow)

        self.assertNotIn("TLS Version", data)
        self.assertNotIn("Client TLS Version", data)

    def test_the_flow_id_no_longer_squats_on_the_connection_key(self) -> None:
        """`Connection ID` 历来存的是 `flow.id`，真正的连接 id 无处可放。"""
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

        self.assertEqual(data["Flow ID"], flow.id)
        self.assertEqual(data["Connection ID"], flow.client_conn.id)
        self.assertEqual(data["Back Connection ID"], flow.server_conn.id)
        self.assertNotEqual(data["Connection ID"], flow.id)

    def test_both_connections_report_their_state_and_handshake_times(self) -> None:
        """连接状态、传输协议、三个握手/结束时刻历来一个都没产出。"""
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

        self.assertEqual(
            data["Front Connection State"], flow.client_conn.state.name.lower()
        )
        self.assertEqual(
            data["Back Connection State"], flow.server_conn.state.name.lower()
        )
        self.assertEqual(data["Front Transport Protocol"], "tcp")
        # 时间戳产出裸浮点，格式化是 `fields.py` 那一层的事。
        self.assertEqual(
            data["Front TLS Handshake"], flow.client_conn.timestamp_tls_setup
        )
        self.assertEqual(data["Back Connection End"], flow.server_conn.timestamp_end)
        # TCP 建连只有服务端一侧有。
        self.assertEqual(
            data["Back TCP Handshake"], flow.server_conn.timestamp_tcp_setup
        )
        self.assertNotIn("Front TCP Handshake", data)

    def test_the_upstream_spec_is_formatted_without_touching_named_fields(self) -> None:
        """`ServerSpec` 是 ``tuple[scheme, (host, port)]`` 的别名，没有 `.scheme`。"""
        flow = tflow.tflow(resp=True)
        flow.server_conn.via = server_spec.parse(
            "http://1.2.3.4:8080", default_scheme="http"
        )
        data = build_flow_detail(flow)

        self.assertEqual(data["Back Via"], "http://1.2.3.4:8080")

    def test_no_key_is_produced_from_the_deprecated_client_address_alias(self) -> None:
        """`Client.address` 是 `peername` 的废弃别名 —— 读一下就抛警告，值还重复。

        `Back Address` 是另一码事：服务端的 `address` 是**请求的**目标，
        `peername` 是解析后的地址，走 CDN 时两者不同。
        """
        flow = tflow.tflow(resp=True)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            data = build_flow_detail(flow)

        self.assertNotIn("Front Address", data)
        self.assertEqual(data["Back Address"], "address:22")
        self.assertEqual(
            [w for w in caught if "Client.address" in str(w.message)],
            [],
        )


class MessageFieldTests(unittest.TestCase):
    def test_response_cookies_keep_their_attributes_and_duplicates(self) -> None:
        """`Set-Cookie` 允许同名重复，压成字典会把后一条盖掉，属性也整个丢了。"""
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        flow.response.headers.add("Set-Cookie", "sid=1; Path=/; HttpOnly")
        flow.response.headers.add("Set-Cookie", "sid=2; Path=/admin")
        cookies = build_flow_detail(flow)["Response Cookies"]

        self.assertEqual([c["name"] for c in cookies], ["sid", "sid"])
        self.assertEqual([c["value"] for c in cookies], ["1", "2"])
        self.assertEqual(cookies[0]["attrs"], {"Path": "/", "HttpOnly": ""})
        self.assertEqual(cookies[1]["attrs"], {"Path": "/admin"})

    def test_an_urlencoded_form_body_is_produced_apart_from_the_query_string(
        self,
    ) -> None:
        """两个都被叫「参数」，一个在 URL 上一个在 body 里 —— 混一页就分不清了。"""
        flow = tflow.tflow(resp=True)
        self.assertNotIn("Request Form", build_flow_detail(flow))

        flow.request.headers["Content-Type"] = "application/x-www-form-urlencoded"
        flow.request.content = b"user=jun&tag=a&tag=b"
        data = build_flow_detail(flow)

        # 同名键按 ", " 合并，和头部/查询串一个口径。
        self.assertEqual(data["Request Form"], {"user": "jun", "tag": "a, b"})

    def test_a_multipart_body_stays_out_of_the_form_key(self) -> None:
        """multipart 的键值是 `bytes`，值还可能是整个上传文件 —— 归 Body 页管。"""
        flow = tflow.tflow(resp=True)
        flow.request.headers["Content-Type"] = "multipart/form-data; boundary=ferret"
        flow.request.content = (
            b"--ferret\r\n"
            b'Content-Disposition: form-data; name="user"\r\n\r\n'
            b"jun\r\n"
            b"--ferret--\r\n"
        )

        # 解析得出来（不是空表单），但刻意不产出这个键。
        self.assertTrue(flow.request.multipart_form)
        self.assertNotIn("Request Form", build_flow_detail(flow))

    def test_trailers_show_up_only_when_the_message_has_them(self) -> None:
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        self.assertNotIn("Response Trailers", build_flow_detail(flow))

        flow.response.trailers = Headers([(b"x-checksum", b"deadbeef")])
        self.assertEqual(
            build_flow_detail(flow)["Response Trailers"], {"x-checksum": "deadbeef"}
        )


class SizeCaliberTests(unittest.TestCase):
    """四种口径：头部 / 线上（压缩后）/ 解压后 / 合计。"""

    def test_header_bytes_are_the_real_wire_head_not_a_python_repr(self) -> None:
        """改造前量的是 ``len(str(headers))`` —— `multidict.__repr__` 的长度。

        那串东西含引号、``b`` 前缀和 ``Headers[...]`` 外壳，又缺请求行和结尾空行，
        和线上字节没有任何关系。
        """
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        data = build_flow_detail(flow)

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
        data = build_flow_detail(flow)

        self.assertEqual(data["res_wire_size"], len(compressed))
        self.assertEqual(data["res_decoded_size"], len(payload))
        self.assertLess(data["res_wire_size"], data["res_decoded_size"])

    def test_the_totals_add_the_headers_to_the_wire_bytes(self) -> None:
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

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
        data = build_flow_detail(flow)

        self.assertGreater(data["req_total_size"], 0)
        self.assertEqual(data["total_size"], data["req_total_size"])
        self.assertNotIn("res_wire_size", data)

    def test_wire_size_treats_a_missing_message_as_zero(self) -> None:
        self.assertEqual(wire_size(None), 0)


class DetailShapeTests(unittest.TestCase):
    """字典本身的形状：原生状态归 `raw_state`，面板要的几项显式产出。"""

    def test_the_native_state_lives_under_one_key_instead_of_the_top_level(
        self,
    ) -> None:
        """改造前是 `dict(flow.get_state())` 打底再叠加工字段。

        那等于把原生小写键和面板自己的 CamelCase 键塞进同一层命名空间，谁盖谁全看
        叠加顺序。现在原生状态整体在 `raw_state` 里 —— 「原始状态」页要的就是它。
        """
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

        raw = data["raw_state"]
        self.assertEqual(raw, flow.get_state())
        # 原生子树原样保留，不打平。
        for key in ("client_conn", "server_conn", "request", "response"):
            self.assertIn(key, raw)
        # 顶层不再有那批小写原生键。
        for key in ("type", "version", "intercepted", "client_conn", "request"):
            self.assertNotIn(key, data)

    def test_the_keys_the_ui_reads_by_name_are_produced_explicitly(self) -> None:
        """`id` / `comment` 早先是从 `get_state()` 里蹭来的，右键菜单一直在读。"""
        flow = tflow.tflow(resp=True)
        flow.comment = "看一下这条"
        flow.marked = ":star:"
        data = build_flow_detail(flow)

        self.assertEqual(data["id"], flow.id)
        self.assertEqual(data["comment"], "看一下这条")
        self.assertEqual(data["marked"], ":star:")
        self.assertEqual(data["is_replay"], "")
        # `live` / `marked` 面板按字符串渲染，布尔在这一层就定死成 "true"/"false"。
        self.assertEqual(data["live"], "true")
        flow.live = False
        self.assertEqual(build_flow_detail(flow)["live"], "false")

    def test_an_unanswered_flow_reports_pending_instead_of_a_status_code(self) -> None:
        flow = tflow.tflow()
        data = build_flow_detail(flow)

        self.assertEqual(data["state"], "request")
        self.assertEqual(data["Status Code"], "Pending...")
        self.assertNotIn("curl_command", data)

    def test_an_errored_flow_carries_the_reason_text(self) -> None:
        """出错原因今天只活在表格 tooltip 里，详情面板拿不到。"""
        flow = tflow.tflow(err=True)
        data = build_flow_detail(flow)

        self.assertEqual(data["state"], "error")
        self.assertEqual(data["Status Code"], "Error")
        assert flow.error is not None
        self.assertEqual(data["Error Message"], flow.error.msg)
        self.assertEqual(data["Error Time"], flow.error.timestamp)

    def test_the_flow_metadata_group_is_produced_from_the_flow_itself(self) -> None:
        """`version` 只在原生状态里有，`modified()` 是方法不是属性 —— 两个都容易写错。"""
        flow = tflow.tflow(resp=True)
        flow.metadata["proxyserver"] = "regular"
        data = build_flow_detail(flow)

        self.assertEqual(data["Flow Type"], "http")
        self.assertEqual(data["Flow Version"], flow.get_state()["version"])
        self.assertEqual(data["Flow Created"], flow.timestamp_created)
        self.assertEqual(data["Intercepted"], "false")
        self.assertEqual(data["Modified"], "false")
        self.assertEqual(data["Flow Metadata"], {"proxyserver": "regular"})

        flow.request.headers["x-added"] = "1"
        self.assertEqual(build_flow_detail(flow)["Modified"], "false")
        # `modified()` 比的是 `flow.backup()` 存下的那份状态 —— 没备份过就永远是
        # false，改多少字段都一样。断点写回那条路径上 mitmproxy 会先 `backup()`。
        flow.backup()
        flow.request.headers["x-added"] = "2"
        self.assertEqual(build_flow_detail(flow)["Modified"], "true")

    def test_the_response_reports_who_compressed_the_body(self) -> None:
        """「线上 3.1 KB / 解压后 12 KB」旁边得说清是谁压的，否则看着像 bug。"""
        flow = tflow.tflow(resp=True)
        assert flow.response is not None
        self.assertEqual(build_flow_detail(flow)["Response Content-Encoding"], "")

        flow.response.headers["Content-Encoding"] = "gzip"
        flow.response.raw_content = gzip.compress(b"x" * 64)
        self.assertEqual(build_flow_detail(flow)["Response Content-Encoding"], "gzip")

    def test_a_completed_flow_carries_its_curl_command(self) -> None:
        flow = tflow.tflow(resp=True)
        data = build_flow_detail(flow)

        self.assertEqual(data["state"], "complete")
        self.assertIn("curl", data["curl_command"])

    def test_infer_state_only_ever_answers_three_things(self) -> None:
        """产出侧和 `fields._STATE_LABELS` 都按这三种来 —— 多写的分支永远进不去。"""
        self.assertEqual(infer_state(tflow.tflow()), "request")
        self.assertEqual(infer_state(tflow.tflow(resp=True)), "complete")
        self.assertEqual(infer_state(tflow.tflow(err=True)), "error")


if __name__ == "__main__":
    unittest.main()
