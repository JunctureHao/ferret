"""通过客户端源端口反查进程信息（名称、路径、PID）。"""

from __future__ import annotations

import psutil


class ProcessInfo:
    """进程信息容器"""

    __slots__ = ("exe", "name", "pid")

    def __init__(self, name: str = "", exe: str = "", pid: int = 0):
        self.name = name
        self.exe = exe
        self.pid = pid

    def to_dict(self) -> dict:
        return {
            "App Name": self.name,
            "App ID": self.exe.rsplit("\\", 1)[-1] if self.exe else "",
            "App Path": self.exe,
            "Process ID": self.pid,
        }

    def is_valid(self) -> bool:
        return self.pid > 0


def resolve_process(client_addr: tuple[str, int]) -> ProcessInfo | None:
    """根据客户端地址 (ip, port) 反查发起请求的进程。

    原理：
      1. 获取系统所有 TCP 连接
      2. 找到本地端口 == client_port 且状态为 ESTABLISHED 的条目
      3. 通过该条目的 pid 查询进程详情
    """
    if not client_addr:
        return None

    _client_ip, client_port = client_addr

    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return None

    for conn in connections:
        if conn.laddr and conn.laddr.port == client_port and conn.pid and conn.pid > 0:
            try:
                proc = psutil.Process(conn.pid)
                return ProcessInfo(
                    name=proc.name(),
                    exe=proc.exe(),
                    pid=conn.pid,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return None

    return None
