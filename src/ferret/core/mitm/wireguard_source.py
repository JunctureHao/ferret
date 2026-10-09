"""WireGuard 入口归属：在内核线程把实例身份写入流量，不保存密钥或路径。"""

from __future__ import annotations

from ferret.core.mitm.bindings import Flow, ProxyMode, connection

WIREGUARD_DEVICE_ID = "ferret.wireguard.device_id"
WIREGUARD_DEVICE_NAME = "ferret.wireguard.device_name"


def clear_wireguard_source(flow: Flow) -> None:
    """客户端重放使用新入口；复制流量后、入表前清除原设备归属。"""
    flow.metadata.pop(WIREGUARD_DEVICE_ID, None)
    flow.metadata.pop(WIREGUARD_DEVICE_NAME, None)


def wireguard_source(flow: Flow) -> tuple[str, str]:
    """只读已保存的标量身份；旧文件与畸形 metadata 不推测设备。"""
    if flow.is_replay == "request":
        return "", ""
    device_id = flow.metadata.get(WIREGUARD_DEVICE_ID)
    name = flow.metadata.get(WIREGUARD_DEVICE_NAME)
    return (
        device_id if isinstance(device_id, str) else "",
        name if isinstance(name, str) else "",
    )


def is_wireguard_flow(flow: Flow) -> bool:
    mode = getattr(flow.client_conn, "proxy_mode", None)
    return (
        flow.is_replay != "request"
        and isinstance(mode, ProxyMode)
        and mode.type_name == "wireguard"
    )


class WireGuardSourceAddon:
    """全 spec 映射入口，不能用所有实例重复的隧道 IP 或内层目标地址识别。

    连接首次建立时固定归属；流量首次标记后固定历史名称。设备改名、删除、
    替换注册表都不能把历史会话转给后来者。导入文件的非 live 流量仅保留原标记。
    """

    def __init__(self) -> None:
        self._devices: dict[str, tuple[str, str]] = {}
        self._connections: dict[str, tuple[str, str] | None] = {}

    def set_devices(self, devices: dict[str, tuple[str, str]]) -> None:
        self._devices = dict(devices)

    def _identity(self, client: connection.Client) -> tuple[str, str] | None:
        mode = client.proxy_mode
        if not isinstance(mode, ProxyMode) or mode.type_name != "wireguard":
            return None
        return self._devices.get(mode.full_spec)

    def client_connected(self, client: connection.Client) -> None:
        self._connections[client.id] = self._identity(client)

    def client_disconnected(self, client: connection.Client) -> None:
        self._connections.pop(client.id, None)

    def _tag(self, flow: Flow) -> None:
        if flow.is_replay == "request":
            clear_wireguard_source(flow)
            return
        if WIREGUARD_DEVICE_ID in flow.metadata or not flow.live:
            return
        client = flow.client_conn
        identity = (
            self._connections[client.id]
            if client.id in self._connections
            else self._identity(client)
        )
        if identity is not None:
            flow.metadata[WIREGUARD_DEVICE_ID], flow.metadata[WIREGUARD_DEVICE_NAME] = (
                identity
            )

    # 所有协议都早于 View 的入表钩子。response/error 覆盖只观察到后半段的情况；
    # 同一 HTTPFlow 的 WebSocket 帧沿用握手时身份，不重新查设备注册表。
    requestheaders = _tag
    request = _tag
    responseheaders = _tag
    response = _tag
    error = _tag
    http_connect = _tag
    tcp_start = _tag
    udp_start = _tag
    dns_request = _tag
    dns_response = _tag
    dns_error = _tag
