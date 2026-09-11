"""Capture channel specs: the four ways traffic is allowed to reach the kernel.

四条通道对应原生 ``mode`` 选项（``mode_specs.ProxyMode``）里的条目，可任意组合：

- **系统代理（regular）** —— 内核的常驻底盘，负责监听 TCP 端口；本机客户端与
  局域网设备都连它。「开始/停止抓包」控制的是系统代理注册表与写入闸门，
  regular 监听本身永远在。
- **本地重定向（local）** —— mitmproxy_rs 在 OS 层按进程重定向本机流量，客户端
  零配置。Windows 上由提权子进程 windows-redirector.exe 实现（首次开启弹 UAC）。
  它不监听任何端口，spec 只认进程名 / PID（逗号分隔，``!`` 取反）。
- **WireGuard（wireguard）** —— 内核作为 WireGuard 服务端收别的设备接入的流量；
  UDP 51820，密钥文件由上游写在 confdir（``wireguard.conf``）。
- **反向代理（reverse）** —— 把 ferret 架在目标服务前面：客户端把监听口当
  服务器直连，一个通道只指向一个固定目标（spec 写死，spec 语法与查重动机见
  ``reverse_mode_spec``）。适配「客户端改不了代理配置」的场景。

上游代理（upstream）**不是第五条通道**，而是第一个槽位的替换：开启后 ``regular``
整条换成 ``upstream:<目标>``（见 ``upstream_mode_spec``），监听地址与端口一字不动，
只把系统代理这条通道的出口从直连换成「先交给上游代理」。local / wireguard / reverse
的出口仍是直连 —— 它们的实例顶层是 ``TransparentProxy``，而原生只在 upstream 的
HTTP 层上设 ``server_conn.via``（``http/__init__.py:963-967``）。

环回豁免是这套组合的安全边界：系统代理把本机流量送到 ``127.0.0.1:port``，而
local 的 WinDivert 过滤器 ``!loopback && ...``（上游 main2.rs，本机
windows-redirector.exe 内嵌字符串逐字一致）在驱动层放行全部环回流量，两条通道
并存不会二次代理/自环。**别把系统代理地址写成局域网 IP**：不带 loopback 标志的
流量会被 local 截到 —— ferret 恒写 ``127.0.0.1``（见 ``MitmFacade.local_client_host``）。
"""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

import segno
from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import (
    ProxyMode,
    UpstreamMode,
    rs_process_info,
    rs_wireguard,
)
from ferret.core.network import ANY_HOST

WIREGUARD_HOST = ANY_HOST
"""WireGuard 服务端绑定地址：VPN 端点必须让局域网设备可达，环回绑定毫无意义。"""

WIREGUARD_PORT = 51820
"""WireGuard 默认端口（上游 ``WireGuardMode.default_port``）。"""

LOCAL_DUP_DODGE = "@127.0.0.1:0"
"""给 local spec 挂的显式 ``@`` 地址，绕开上游重复地址误报。

上游 #7063（12.2.3 未修）：``proxyserver.configure`` 的查重对每个模式取
``listen_port(ctx.options.listen_port)``，local 的 ``default_port=None`` 会被
全局默认端口（非 None）顶掉，于是 local 被「算成」和 regular 一样监听
``*:8080``，三通道并存必报 ``Cannot spawn multiple servers on the same address``。
给 local 显式 ``@127.0.0.1:0`` 后，查重读到唯一的 ``(127.0.0.1, 0)``；而
``LocalRedirectorInstance.listen_addrs = ()``（空类属性）意味着这个地址**永远不会
被真正绑定**，纯属查重占位。上游修复后本常量与拼接处可整体移除。
"""


def local_mode_spec(local_spec: str) -> str:
    """拼 ``local`` 模式串。过滤串留空 = 截全部本机进程（自身 PID 由上游自动排除）。"""
    cleaned = local_spec.strip()
    head = "local" if not cleaned else f"local:{cleaned}"
    return head + LOCAL_DUP_DODGE


def wireguard_mode_spec() -> str:
    return f"wireguard@{WIREGUARD_HOST}:{WIREGUARD_PORT}"


REVERSE_DEFAULT_PORT = 8081
"""reverse 通道默认监听端口（regular 默认 8080 + 1，与内核查重错开）。"""


def reverse_mode_spec(target: str, listen_host: str, listen_port: int) -> str:
    """拼 ``reverse:<target>@<listen_host>:<listen_port>``。

    ``@`` 地址必须显式：不带时上游回退全局 ``listen_host``/``listen_port``，
    与 regular 撞同一地址被 ``proxyserver.configure`` 查重拒（查重键是
    ``(host, port, proto)``，reverse https 是 tcp+udp 双侧）。这与 local 挂
    ``@127.0.0.1:0`` 绕上游 #7063 的动机不同——这里是**主动**要独立端口，
    上游修复后也**不能**随 local 一起移除。目标串只进 ``<target>`` 段，
    ``http(s)://`` 前缀与坏 host 交给 ``validate_mode_specs`` 过原生解析器。
    """
    return f"reverse:{target.strip()}@{listen_host}:{listen_port}"


def upstream_mode_spec(target: str) -> str:
    """拼 ``upstream:<target>``，占掉 regular 的那个槽位。

    **不带 ``@`` 监听段**：它接管的就是 regular 那条监听，必须继承全局
    ``listen_host``/``listen_port``（``ProxyMode.listen_port`` 在没有自定义端口时
    回退 ``ctx.options.listen_port``），否则系统代理写进注册表的地址与内核实际
    监听的对不上。与 reverse **必须**带 ``@`` 的动机正好相反：那边是主动要一个
    独立端口，这边是主动要同一个端口。

    ``target`` 只收 ``http(s)://host[:port]``，**凭证不进 spec**：原生
    ``server_spec.parse`` 的 host 段是 ``[^:/]+``，且 ``ProxyMode.parse`` 会先按
    最后一个 ``@`` 切监听段 —— ``upstream:http://user:pass@proxy:8080`` 实测报
    ``Invalid server specification: http://user:pass``。凭证走另一条正交的线，即
    ``upstream_auth`` 选项（见 ``MitmRuntime._upstream_auth``）。坏 scheme 之类交给
    ``validate_mode_specs`` 过原生解析器（``UpstreamMode`` 只认 http/https）。
    """
    return f"upstream:{target.strip()}"


def upstream_address(target: str) -> tuple[str, int]:
    """上游代理解析后的 ``(host, port)``。坏值抛已译 ``ValueError``。

    不能拿字符串比对代替它：scheme 与端口都可以省（``proxy`` → ``("proxy", 80)``、
    ``https://proxy`` → ``("proxy", 443)``，见 ``UpstreamMode.__post_init__``），
    同一个地址有一大把写法。
    """
    spec = upstream_mode_spec(target)
    validate_mode_specs([spec])
    mode = ProxyMode.parse(spec)
    # 上一句已经过了校验，这里必然是 UpstreamMode；断言只为把类型收窄。
    assert isinstance(mode, UpstreamMode)
    return mode.address


def upstream_targets_self(target: str, *, listen_host: str, listen_port: int) -> bool:
    """上游地址是否指回 ferret 自己的监听口（自环）。

    判据照抄原生自连守卫 ``proxyserver.server_connect``
    （``proxyserver.py:376-395``）：端口相同，且 host 落在
    ``localhost``/``127.0.0.1``/``::1``/监听地址 之内。原生那道守卫是**每条流量**
    的兜底，写的是 "Request destination unknown. Unable to figure out where this
    request should be forwarded to." —— 看不懂，而且要等到有流量才出现。所以提交
    上游配置时先在这里拦一道，给一句能看懂的中文。
    """
    host, port = upstream_address(target)
    return port == listen_port and host in (
        "localhost",
        "127.0.0.1",
        "::1",
        listen_host,
    )


def capture_mode_specs(
    *,
    use_local: bool,
    local_spec: str,
    use_wireguard: bool,
    use_reverse: bool = False,
    reverse_target: str = "",
    reverse_port: int = REVERSE_DEFAULT_PORT,
    use_upstream: bool = False,
    upstream_target: str = "",
    listen_host: str = "",
) -> list[str]:
    """完整 ``mode`` 选项列表。

    第一个槽位恒在，它是系统代理与 compose 的底盘（负责监听 TCP 端口）：默认
    ``regular``，开了上游代理就**整条换成** ``upstream:<目标>``，而不是追加一条。
    二者绝不并存 —— 都回退全局 ``listen_port``，同时在场必被
    ``proxyserver.configure`` 的 ``(host, port, proto)`` 查重拒（实测
    ``OptionsError: Cannot spawn multiple servers on the same address``）。换而不是
    追加还有第二个理由：``ClientPlayback`` 只看 ``options.mode[0]`` 是不是 upstream
    前缀（``clientplayback.py:97-105``），放在首位重放/compose 才跟着走上游。
    其余通道按启用勾选拼在后面。
    reverse 排在 local/wireguard 之前（紧跟首槽）：它与首槽共用
    ``listen_host``（一处管「谁能连进来」），spec 里的端口则由调用方保证错开。

    ``listen_host`` 缺省 ``""`` 时是上游 ``ProxyMode.parse`` 的「绑所有地址」语义，
    与 options.listen_host 的回退一致。真实调用（``MitmRuntime._mode_specs``）恒传
    self.listen_host；测试里使用空串的等价性由 ``test_local_spec_carries_the_
    duplicate_address_dodge`` 一类用例的 (host, port, proto) 三元组断言覆盖。
    ``reverse_target`` 为空时不开 reverse——避免「勾上但目标没填」误把
    ``reverse:@127.0.0.1:8081`` 这种空 spec 推进内核（ProxyMode.parse 会拒）。
    ``upstream_target`` 为空时同理保持 ``regular``：空目标换槽位只会让系统代理这条
    主通道整条死掉，比不生效糟得多。
    """
    head = "regular"
    if use_upstream and upstream_target.strip():
        head = upstream_mode_spec(upstream_target)
    specs = [head]
    if use_reverse and reverse_target.strip():
        specs.append(reverse_mode_spec(reverse_target, listen_host, reverse_port))
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
                QCoreApplication.translate("CaptureModes", "抓包通道无效：{}").format(
                    exc
                )
            ) from exc


def validate_local_spec(local_spec: str) -> None:
    """只校验本地重定向的进程过滤串（设置对话框实时反馈用）。"""
    validate_mode_specs([local_mode_spec(local_spec)])


def ensure_wireguard_conf(conf_path: Path) -> None:
    """确保 WireGuard 密钥文件存在，格式与 mitmproxy 内核写的完全一致。

    内核 WireGuardServerInstance._start() 只在文件不存在时写，所以这里抢先
    生成不会与它打架：先到者定密钥，后到者复用。用 "x" 独占创建关掉
    「两边同时发现文件不存在」的竞态，撞上了就认对方那份。
    """
    if conf_path.exists():
        return
    conf_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"server_key": rs_wireguard.genkey(), "client_key": rs_wireguard.genkey()},
        indent=4,
    )
    try:
        with conf_path.open("x", encoding="utf-8") as fp:
            fp.write(payload)
    except FileExistsError:
        return  # 内核（或另一个 ferret 实例）刚写完，用它那份


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


def wireguard_qr_matrix(conf_path: Path, lan_address: str | None) -> list[list[bool]]:
    """客户端配置的 QR 矩阵（不含静区），供界面用 QPainter 自绘。

    WireGuard 官方 App 的「扫描二维码」扫的就是 wg-quick 配置原文——解码出的
    文本按 `[Interface]/[Peer]` 解析导入，没有任何专有编码。与
    `wireguard_client_config` 同源同刻，码和文本框不会各说一套。一份配置实测
    ~224 字节 → 纠错 M 下 version 11（61×61 模块），手机正常扫。
    """
    return qr_matrix(wireguard_client_config(conf_path, lan_address))


def qr_matrix(text: str, *, error: str = "m") -> list[list[bool]]:
    """把文本编码成 QR 布尔矩阵（不含静区；渲染方按规范补 4 模块白边）。"""
    qr = segno.make(text, error=error)
    # segno 的 matrix 元素是 0/1 int，这里归一成 bool，渲染层不必再判。
    return [[bool(dark) for dark in row] for row in qr.matrix]


# —— 本地重定向的进程点选（UI 数据层；匹配语义见 LocalRedirector 的子串包含）——


@dataclass(frozen=True)
class LocalTarget:
    """一个可点选的本机进程目标。`icon_png` 为 PNG 字节，取不到时是 None。"""

    display_name: str
    executable: str
    icon_png: bytes | None


def split_spec(spec: str) -> list[str]:
    """把过滤串拆成 token（逗号分隔、去空白、丢空段）。"""
    return [token.strip() for token in spec.split(",") if token.strip()]


def list_local_targets(*, include_system: bool = False) -> list[LocalTarget]:
    """枚举可点选的本机进程，供 UI 下拉勾选。

    只滤系统进程（``is_system``：svchost/Defender/服务宿主等）与空名进程——空
    ``display_name`` 的条目在点选列表里是一行无文字空白，且会在 ``checked_tokens``
    的 contains 语义下被任意 token 误点亮（空串是一切串的子串），进而在拼接
    spec 时产出尾逗号被上游拒收（``invalid intercept spec``）。**不要**按
    ``is_visible`` 过滤：无可见窗口 ≠ 不是用户应用，MuMu 模拟器组件、node、
    msedgewebview2、Reqable 的后台进程这些真实调试目标都会被误伤。ferret 自身
    的可执行文件也跳过：上游运行期会自动排除自身 PID，点选它没有意义。图标
    惰性取：调用方拿到 `icon_png=None` 时自行兜底通用图标；失败静默降级
    （上游 web UI 同款处理，给透明占位）。
    """
    own = Path(sys.executable).resolve()
    targets: list[LocalTarget] = []
    for process in rs_process_info.active_executables():
        if not include_system and process.is_system:
            continue
        if not process.display_name.strip():
            continue
        executable = str(process.executable)
        try:
            if Path(executable).resolve() == own:
                continue
        except OSError:
            pass
        try:
            icon_png: bytes | None = rs_process_info.executable_icon(executable)
        except Exception:  # noqa: BLE001
            icon_png = None
        targets.append(
            LocalTarget(
                display_name=process.display_name,
                executable=executable,
                icon_png=icon_png,
            )
        )
    return targets


def checked_tokens(spec: str, targets: list[LocalTarget]) -> set[str]:
    """打开下拉时应当勾选的候选名集合：spec 里能对上目标名/路径的那些。

    判定沿用内核的 contains 语义（大小写不敏感）：候选名出现在 spec 任一 token
    里即算勾选——用户写 `ding` 也能点亮「钉钉」。返回值是候选名本身（显示名 /
    exe 名 / 完整路径中第一个命中的），UI 直接与条目文本比对。
    """
    tokens = [token.lower() for token in split_spec(spec)]
    checked: set[str] = set()
    for target in targets:
        for candidate in (
            target.display_name,
            Path(target.executable).name,
            target.executable,
        ):
            lowered = candidate.lower()
            # 空候选名在 contains 语义下匹配一切 token，直接跳过（见
            # list_local_targets 对空 display_name 的过滤说明）。
            if not lowered:
                continue
            if any(lowered in token or token in lowered for token in tokens):
                checked.add(candidate)
                break
    return checked
