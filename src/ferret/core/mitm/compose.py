"""手工构造请求并交由 mitmproxy 内核发出（编辑页「发送」的内核一侧）。

和 `MitmFacade.replay_flows` 同源：`ClientPlayback.start_replay` 本来就接受任何
「不在线、未拦截、请求完整」的 flow，并不在乎它是从列表里复制的还是徒手造的。
区别只在三处：

1. flow 由 `build_compose_flow` 从零构造（`Request.make` + 客户端连接桩）；
2. `flow.metadata[COMPOSE_METADATA_KEY]` 打上标记，`ComposeAddon` 凭它在
   `response` / `error` 钩子里认出这条流量、把结果快照经信号桥送回编辑页；
3. 「进入流量列表」是一个选项：`View.requestheaders` 会无条件收录所有 flow，
   不入选的由 `ComposeAddon` 在响应/错误落地后从 View 里摘除（提前摘会被
   `View.remove` 的 kill 副作用杀掉这条 replay flow，见 `_finish` 注释）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ferret.core.mitm.bindings import HTTPFlow, Request, View, connection, http_url
from ferret.core.mitm.detail import build_flow_detail

# `flow.metadata` 的键：一次手工发送的唯一凭据。ComposeAddon 靠存在性识别，
# 值只是给人看的（详情面板的「流量元数据」行会显示它）。
COMPOSE_METADATA_KEY = "ferret.compose"


def build_compose_flow(
    method: str,
    url: str,
    headers: list[tuple[str, str]] | None,
    content: bytes | str | None,
) -> HTTPFlow:
    """从零构造一条可回放的 flow。只在 mitm 线程内调用。

    `Request.make` 会解析 URL 并按 body 算 `Content-Length`，但**不补 `Host`**：
    url setter 的 `_update_host_and_authority` 只覆写已存在的同名头
    （mitmproxy 12 实测如此），而回放路径的 `Http1Client` 也只在 h2/h3 转
    h1 时才代补 —— 缺了它，HTTP/1.1 请求会被服务器按 RFC 9112 §3.2 一律
    回 400（jsonplaceholder/Cloudflare 实测 155b 的裸 400 就是这个）。
    所以这里仿 curl：用户显式给 `Host` 就尊重（须先摘后还，否则会被 URL
    覆写），否则按 scheme/host/port 生成，默认端口省略。
    """
    # 用户显式给的 Host 单拎出来，绕开 Request.make 的 URL 覆写；同名多行时
    # 后写的赢，与 curl 的 -H 一致。
    user_host: str | None = None
    header_pairs: list[tuple[str, str]] = []
    for key, value in headers or []:
        if key.strip().lower() == "host":
            user_host = value
        else:
            header_pairs.append((key, value))
    request = Request.make(
        method.upper(),
        url,
        content or b"",
        # mitmproxy 12 的 Headers 只收 bytes；界面给的是 str，utf-8 转一层。
        [(k.encode("utf-8"), v.encode("utf-8")) for k, v in header_pairs],
    )
    if user_host is not None:
        request.headers["Host"] = user_host
    else:
        request.headers["Host"] = http_url.hostport(
            request.scheme, request.host, request.port
        )
    client_conn = connection.Client(
        # 桩连接：`client_playback.ReplayHandler` 只 copy 它并改 state，
        # 真正的连接按 `request.host/port` 现拨，桩的地址永远不上线。
        peername=("127.0.0.1", 0),
        sockname=("127.0.0.1", 0),
    )
    flow = HTTPFlow(client_conn, connection.Server(address=None))
    flow.request = request
    return flow


@dataclass
class ComposeResult:
    """一次手工发送的落地结果：纯数据快照，可安全跨线程交给 Qt 侧。

    `detail` 是 `build_flow_detail` 的完整字典（响应已存在才有响应字段），
    编辑页的响应侧直接消费它，与抓包详情面板同一份口径。
    """

    flow_id: str
    error: str  # 空串表示成功拿到响应
    detail: dict


def compose_result(flow: HTTPFlow) -> ComposeResult:
    """在钩子（mitm 线程）里当场把活 flow 压成值对象。"""
    return ComposeResult(
        flow_id=flow.id,
        error=flow.error.msg if flow.error else "",
        detail=build_flow_detail(flow),
    )


class ComposeAddon:
    """认出编辑页发出的流量：回报结果，并按发送时的选择决定留不留列表。

    所有成员只在 mitm 线程上读写：登记经 `MitmRuntime.call` marshal 进同一个
    loop，钩子本来就跑在这个线程，不需要锁。
    """

    def __init__(self, view: View) -> None:
        # flow_id → 落地后是否留在流量列表。结果桥由 runtime 在 master 起来后接上
        # （与 `GatewayState.on_suspend_changed` 同一个模式）。
        self._view = view
        self._keep: dict[str, bool] = {}
        self.on_result: Callable[[ComposeResult], None] | None = None

    def register(self, flow_id: str, *, keep: bool) -> None:
        self._keep[flow_id] = keep

    def response(self, flow: HTTPFlow) -> None:
        self._finish(flow)

    def error(self, flow: HTTPFlow) -> None:
        self._finish(flow)

    def _finish(self, flow: HTTPFlow) -> None:
        keep = self._keep.pop(flow.id, None)
        if keep is None:
            return
        callback = self.on_result
        if callback is not None:
            callback(compose_result(flow))
        if not keep:
            # 「进入流量列表 = False」：View.requestheaders 无条件收录一切 flow，
            # 只能靠摘除对抗。但摘除**不能提前**（`View.remove` 会 kill 活着的
            # replay flow，error 会抢在响应前落地）—— 等到 response/error 落地、
            # replay 结束（`killable=False`）后再摘，remove 就只是安静移出列表。
            # 这一轮 response 钩子会先触发一次 `sig_view_update`：那一行存在过
            # 一个事件循环切片，界面可能闪一下。要完全没有痕迹，得让 View 不认
            # 这条流量（原生没有这条路子），这是最小代价的做法。
            self._view.remove([flow])
