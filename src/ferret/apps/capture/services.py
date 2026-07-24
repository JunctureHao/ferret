"""轻量级 mitmproxy 主控 — 只跑代理 + 收流量，去除 CLI/终端/Web 等附加件。

对标 mitmproxy.tools.dump.DumpMaster，但：
- 不创建终端日志 (termlog)
- 不挂载 dumper（CLI 输出）、readfile/stdin、keepserving、errorcheck 等 CLI/Web 组件
- 仅挂载代理服务所需的最小 addon，让外部 addon 直接收 HTTPFlow 事件
"""

from __future__ import annotations

import asyncio

from mitmproxy.addons.core import Core
from mitmproxy.addons.dns_resolver import DnsResolver
from mitmproxy.addons.next_layer import NextLayer
from mitmproxy.addons.proxyserver import Proxyserver
from mitmproxy.addons.tlsconfig import TlsConfig
from mitmproxy.master import Master
from mitmproxy.options import Options


class CaptureMaster(Master):
    """最小化的抓包主控。

    用法：
        opts = Options(listen_host="127.0.0.1", listen_port=8080)
        master = CaptureMaster(opts)
        master.addons.add(my_traffic_addon)
        await master.run()          # 在 asyncio 事件循环里运行
        # 停止：master.shutdown()
    """

    def __init__(
        self,
        opts: Options | None = None,
        event_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        # with_termlog=False → 不要终端日志
        super().__init__(opts, event_loop=event_loop, with_termlog=False)

        # 只挂载代理服务必需的最小 addon（5 个底座）：
        # core（事件派发）/ proxyserver（起端口转发）/ tlsconfig（HTTPS 解密）/
        # next_layer（协议分层）/ dns_resolver（DNS 解析）。
        # 其余能力型 addon（改包/映射/拦截/保存/回放等）按需再单独添加。
        self.addons.add(
            Core(),
            Proxyserver(),
            TlsConfig(),
            NextLayer(),
            DnsResolver(),
        )


# ─────────────────────────────────────────────────────────────
# 自测：直接 `python services.py` 启动一个轻量代理并把流量全打印
# 用法：python services.py [port]   然后浏览器/系统代理指向 127.0.0.1:port
# 停止：Ctrl+C
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    from mitmproxy.http import HTTPFlow

    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

    class PrintAddon:
        """只打印响应阶段，用于验证 CaptureMaster。"""

        def response(self, flow: HTTPFlow):
            if flow.response is None:
                print(f"[response] (无响应) {flow.request.pretty_url}")
                return
            print(f"[response] {flow.response.status_code} {flow.request.pretty_url}")
            print("  content-type:", flow.response.headers.get("content-type", ""))
            if flow.response.content:
                preview = flow.response.content[:500]
                print("  body:", preview.decode("utf-8", errors="replace"))

    async def _run():
        opts = Options(listen_host="127.0.0.1", listen_port=PORT)
        master = CaptureMaster(opts)
        master.addons.add(PrintAddon())
        print(f"CaptureMaster 已启动，监听 127.0.0.1:{PORT}")
        print(f"把系统/浏览器代理设为 127.0.0.1:{PORT}，Ctrl+C 停止\n")
        try:
            await master.run()
        except asyncio.CancelledError:
            pass
        finally:
            print("CaptureMaster 已停止")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出")
