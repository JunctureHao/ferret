"""Listen-address vocabulary shared by the kernel, the system proxy and the UI.

抓包代理牵扯三个**不同**的地址，它们不能共用一个变量：

- **绑定地址** —— `LOOPBACK_HOST` 或 `ANY_HOST`，交给 socket bind，决定「谁能连进来」。
- **本机接入地址** —— 恒为 `LOOPBACK_HOST`。系统代理和本机客户端用这个。
  `0.0.0.0` 是 `INADDR_ANY`，环回接口本来就在它的监听范围内，所以绑定地址放开成
  「所有网卡」时，本机这条路径**完全不需要跟着变**。
- **局域网展示地址** —— `detect_lan_address()`，给手机等别的设备填的，**只用于显示和
  复制**。绝不能写进系统代理：它随 DHCP 续租 / 换网 / VPN 上下线而变，一旦失效，
  症状是「所有请求都失败」，而用户根本看不出是代理地址过期。
"""

from __future__ import annotations

import ipaddress
import socket

LOOPBACK_HOST = "127.0.0.1"
"""本机环回地址。系统代理、本机客户端接入点永远是这个值。"""

ANY_HOST = "0.0.0.0"
"""INADDR_ANY：监听所有 IPv4 网卡（含环回），局域网设备因此可以连进来。"""

LISTEN_HOSTS = (LOOPBACK_HOST, ANY_HOST)
"""允许写进配置的绑定地址。顺序即优先级，非法值会被纠正成第一个。"""

DEFAULT_PORT = 8080
"""默认监听端口。"""

PORT_MIN = 1024
"""下界取 1024：1023 及以下在类 Unix 上要 root，且都是保留端口。"""

PORT_MAX = 65535
"""上界即 16 位端口号上限。"""

# RFC 5737 TEST-NET-1：保证不对应任何真实主机。UDP connect 只查路由表选源地址，
# 不发任何数据包，所以拿它探测既不产生流量也不依赖外网可达。
_ROUTE_PROBE_TARGET = ("192.0.2.1", 9)


def is_lan_exposed(listen_host: str) -> bool:
    """绑定地址是否让局域网设备可达。"""
    return listen_host == ANY_HOST


def normalize_listen_host(listen_host: str | None) -> str:
    """把任意输入收敛到受支持的绑定地址；不认识的一律退回环回（更安全的那个）。"""
    return listen_host if listen_host in LISTEN_HOSTS else LOOPBACK_HOST


def normalize_listen_port(port: object) -> int:
    """把任意输入收敛成合法端口；不是整数或超范围就退回默认值。

    配置文件是纯文本，用户手改坏了不能让应用起不来 —— 所以这里只收敛不抛异常。
    `bool` 要单独挡掉：它是 `int` 的子类，`True` 会被算成端口 1。
    """
    if isinstance(port, bool) or not isinstance(port, int):
        return DEFAULT_PORT
    return port if PORT_MIN <= port <= PORT_MAX else DEFAULT_PORT


def detect_lan_address() -> str | None:
    """返回本机在局域网里的 IPv4 地址，拿不到就返回 None。

    走「UDP connect 一个不可路由地址、再读 getsockname」这个套路：内核按路由表选出
    默认路由所在网卡的源地址 —— 正是别的设备访问本机要用的那个。整个过程不发包。

    拿不到时**不猜**：多网卡机器上 `gethostbyname(gethostname())` 很可能返回
    Hyper-V / WSL / VPN 虚拟网卡的地址，把错地址显示给用户比显示「未知」更糟。
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.2)
            probe.connect(_ROUTE_PROBE_TARGET)
            host = probe.getsockname()[0]
    except OSError:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified:
        return None
    return host
