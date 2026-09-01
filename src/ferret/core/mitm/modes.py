"""Capture channel specs: the three ways traffic is allowed to reach the kernel.

三条通道对应原生 ``mode`` 选项（``mode_specs.ProxyMode``）里的条目，可任意组合：

- **系统代理（regular）** —— 内核的常驻底盘，负责监听 TCP 端口；本机客户端与
  局域网设备都连它。「开始/停止抓包」控制的是系统代理注册表与写入闸门，
  regular 监听本身永远在。
- **本地重定向（local）** —— mitmproxy_rs 在 OS 层按进程重定向本机流量，客户端
  零配置。Windows 上由提权子进程 windows-redirector.exe 实现（首次开启弹 UAC）。
  它不监听任何端口，spec 只认进程名 / PID（逗号分隔，``!`` 取反）。
- **WireGuard（wireguard）** —— 内核作为 WireGuard 服务端收别的设备接入的流量；
  UDP 51820，密钥文件由上游写在 confdir（``wireguard.conf``）。

环回豁免是这套组合的安全边界：系统代理把本机流量送到 ``127.0.0.1:port``，而
local 的 WinDivert 过滤器 ``!loopback && ...``（上游 main2.rs，本机
windows-redirector.exe 内嵌字符串逐字一致）在驱动层放行全部环回流量，两条通道
并存不会二次代理/自环。**别把系统代理地址写成局域网 IP**：不带 loopback 标志的
流量会被 local 截到 —— ferret 恒写 ``127.0.0.1``（见 ``MitmFacade.local_client_host``）。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import ProxyMode, rs_wireguard
from ferret.core.network import ANY_HOST

WIREGUARD_HOST = ANY_HOST
"""WireGuard 服务端绑定地址：VPN 端点必须让局域网设备可达，环回绑定毫无意义。"""

WIREGUARD_PORT = 51820
"""WireGuard 默认端口（上游 ``WireGuardMode.default_port``）。"""


def local_mode_spec(local_spec: str) -> str:
    """拼 ``local`` 模式串。过滤串留空 = 截全部本机进程（自身 PID 由上游自动排除）。"""
    cleaned = local_spec.strip()
    return "local" if not cleaned else f"local:{cleaned}"


def wireguard_mode_spec() -> str:
    return f"wireguard@{WIREGUARD_HOST}:{WIREGUARD_PORT}"


def capture_mode_specs(
    *, use_local: bool, local_spec: str, use_wireguard: bool
) -> list[str]:
    """完整 ``mode`` 选项列表。

    regular 恒在第一位：它是系统代理与 compose 的底盘，其余通道按启用勾选拼接。
    """
    specs = ["regular"]
    if use_local:
        specs.append(local_mode_spec(local_spec))
    if use_wireguard:
        specs.append(wireguard_mode_spec())
    return specs


def validate_mode_specs(specs: list[str]) -> None:
    """逐条过原生解析器；任何一条不合法都以可展示的文案抛 ``ValueError``。

    ``ProxyMode.parse`` 会把 spec 里的进程过滤（local）和监听地址语法（wireguard
    的 ``@``）全部校验一遍 —— 提交 ``options.update`` 之前先在这里拦一道，坏值
    就不会走到「内核已受理一半」的境地。
    """
    for spec in specs:
        try:
            ProxyMode.parse(spec)
        except ValueError as exc:
            # exc 是 mitmproxy 的技术性报错原文（英文、不可译），整句外壳在这里翻译。
            raise ValueError(
                QCoreApplication.translate(
                    "CaptureModes", "Invalid capture channel: {}"
                ).format(exc)
            ) from exc


def validate_local_spec(local_spec: str) -> None:
    """只校验本地重定向的进程过滤串（设置对话框实时反馈用）。"""
    validate_mode_specs([local_mode_spec(local_spec)])


def wireguard_client_config(conf_path: Path, lan_address: str | None) -> str:
    """从上游落盘的 ``wireguard.conf``（JSON）生成可导入的客户端配置文本。

    上游 `WireGuardServerInstance` 启动时把服务端/客户端密钥写进 confdir 的
    ``wireguard.conf``，并在日志里打印同样的配置；这里读同一份文件给界面一个
    「复制给手机用」的入口。端点优先用局域网地址，探测不到就退回环回
    （同机测试仍可用）。文件缺失/损坏抛 ``FileNotFoundError`` / ``ValueError``，
    由调用方转成界面提示。
    """
    data = json.loads(conf_path.read_text(encoding="utf-8"))
    server_key, client_key = data["server_key"], data["client_key"]
    rs_wireguard.pubkey(server_key)  # 校验密钥格式，坏文件在这里炸而不是进了剪贴板
    endpoint_host = lan_address or "127.0.0.1"
    return textwrap.dedent(
        f"""
        [Interface]
        PrivateKey = {client_key}
        Address = 10.0.0.1/32
        DNS = 10.0.0.53

        [Peer]
        PublicKey = {rs_wireguard.pubkey(server_key)}
        AllowedIPs = 0.0.0.0/0
        Endpoint = {endpoint_host}:{WIREGUARD_PORT}
        """
    ).strip()
