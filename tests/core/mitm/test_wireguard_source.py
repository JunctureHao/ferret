"""同内层地址的 WireGuard 流量归属、历史快照与原生会话/过滤兼容。"""

from __future__ import annotations

import io
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mitmproxy import flowfilter
from mitmproxy.io import FlowReader, FlowWriter
from mitmproxy.proxy.mode_specs import ProxyMode
from mitmproxy.test import tflow

from ferret.core.mitm import HTTPFlow, build_flow_summary, flow_row
from ferret.core.mitm.wireguard_source import (
    WIREGUARD_DEVICE_ID,
    WIREGUARD_DEVICE_NAME,
    WireGuardSourceAddon,
    clear_wireguard_source,
)

SPEC_A = "wireguard:device-a.conf@0.0.0.0:51820"
SPEC_B = "wireguard:device-b.conf@0.0.0.0:51821"


class WireGuardSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addon = WireGuardSourceAddon()
        self.addon.set_devices({SPEC_A: ("a", "Phone A"), SPEC_B: ("b", "Phone B")})

    @staticmethod
    def flow(spec: str = SPEC_A):
        flow = tflow.tflow(resp=True)
        flow.client_conn.peername = ("10.0.0.1", 12345)
        flow.client_conn.proxy_mode = ProxyMode.parse(spec)
        return flow

    def test_same_inner_addresses_use_instance_identity(self) -> None:
        a, b = self.flow(SPEC_A), self.flow(SPEC_B)
        for flow in (a, b):
            self.addon.client_connected(flow.client_conn)
            self.addon.requestheaders(flow)
        self.assertEqual(a.client_conn.peername, b.client_conn.peername)
        self.assertEqual(flow_row(a).wireguard_device_id, "a")
        self.assertEqual(flow_row(b).wireguard_device_id, "b")
        snapshot = flow_row(a)
        a.metadata[WIREGUARD_DEVICE_NAME] = "Changed after snapshot"
        self.assertEqual(snapshot.wireguard_device_name, "Phone A")
        self.assertNotEqual(snapshot.conn_id, flow_row(b).conn_id)

    def test_connection_and_history_keep_identity_after_registry_replacement(
        self,
    ) -> None:
        old = self.flow()
        self.addon.client_connected(old.client_conn)
        self.addon.requestheaders(old)
        self.addon.set_devices({SPEC_A: ("new", "New Phone")})
        self.addon.response(old)
        subsequent = tflow.tflow(client_conn=old.client_conn)
        self.addon.requestheaders(subsequent)
        new_connection = self.flow()
        self.addon.client_connected(new_connection.client_conn)
        self.addon.requestheaders(new_connection)
        for flow in (old, subsequent):
            self.assertEqual(flow.metadata[WIREGUARD_DEVICE_ID], "a")
            self.assertEqual(flow.metadata[WIREGUARD_DEVICE_NAME], "Phone A")
        self.assertEqual(new_connection.metadata[WIREGUARD_DEVICE_ID], "new")
        self.addon.client_disconnected(old.client_conn)
        self.assertNotIn(old.client_conn.id, self.addon._connections)

    def test_legacy_and_unrecognised_connections_are_not_reattributed(self) -> None:
        old = self.flow()
        old.live = False
        self.addon.requestheaders(old)
        self.assertEqual(flow_row(old).wireguard_device_id, "")
        self.assertTrue(flow_row(old).is_wireguard)
        self.assertEqual(build_flow_summary(old)["WireGuard Device"], "未知设备")
        self.addon.set_devices({})
        unknown = self.flow()
        self.addon.client_connected(unknown.client_conn)
        self.addon.set_devices({SPEC_A: ("new", "New Phone")})
        self.addon.requestheaders(unknown)
        self.assertNotIn(WIREGUARD_DEVICE_ID, unknown.metadata)

    def test_client_replay_clears_device_but_server_playback_keeps_real_client(
        self,
    ) -> None:
        flow = self.flow()
        self.addon.requestheaders(flow)
        flow.is_replay = "response"
        self.addon.response(flow)
        self.assertEqual(flow_row(flow).wireguard_device_id, "a")
        replay = flow.copy()
        replay.is_replay = "request"
        self.addon.requestheaders(replay)
        self.assertNotIn(WIREGUARD_DEVICE_ID, replay.metadata)
        self.assertNotIn(WIREGUARD_DEVICE_NAME, replay.metadata)
        self.assertFalse(flow_row(replay).is_wireguard)
        self.assertNotIn("WireGuard Device", build_flow_summary(replay))
        self.assertEqual(flow_row(flow).wireguard_device_id, "a")
        clear_wireguard_source(replay)

    def test_dns_tcp_and_udp_record_device_before_native_view_hooks(self) -> None:
        for factory, hook in (
            (tflow.tdnsflow, self.addon.dns_request),
            (tflow.ttcpflow, self.addon.tcp_start),
            (tflow.tudpflow, self.addon.udp_start),
        ):
            with self.subTest(factory=factory.__name__):
                flow = factory()
                flow.live = True
                flow.client_conn.proxy_mode = ProxyMode.parse(SPEC_B)
                hook(flow)
                row = flow_row(flow)
                self.assertEqual(row.wireguard_device_id, "b")
                self.assertEqual(row.wireguard_device_name, "Phone B")

    def test_native_filter_and_session_roundtrip_preserve_saved_source(self) -> None:
        flow = self.flow()
        self.addon.requestheaders(flow)
        data = io.BytesIO()
        FlowWriter(data).add(flow)
        data.seek(0)
        restored = next(iter(FlowReader(data).stream()))
        assert isinstance(restored, HTTPFlow)
        self.addon.set_devices({SPEC_A: ("new", "New Phone")})
        self.addon.response(restored)
        self.assertEqual(flow_row(restored).wireguard_device_name, "Phone A")
        self.assertEqual(build_flow_summary(restored)["WireGuard Device ID"], "a")
        self.assertTrue(flowfilter.match('~meta "Phone A"', restored))
        self.assertFalse(flowfilter.match('~meta "Phone B"', restored))
        self.assertEqual(
            set(restored.metadata), {WIREGUARD_DEVICE_ID, WIREGUARD_DEVICE_NAME}
        )

    def test_regular_source_stays_empty_and_bad_metadata_stays_scalar(self) -> None:
        regular = self.flow("regular")
        self.addon.requestheaders(regular)
        self.assertFalse(flow_row(regular).is_wireguard)
        self.assertNotIn("WireGuard Device", build_flow_summary(regular))
        regular.metadata[WIREGUARD_DEVICE_ID] = {"unexpected": "data"}
        regular.metadata[WIREGUARD_DEVICE_NAME] = ["not", "a", "name"]
        row = flow_row(regular)
        self.assertEqual(row.wireguard_device_id, "")
        self.assertEqual(row.wireguard_device_name, "")


if __name__ == "__main__":
    unittest.main()
