"""大正文边收边截（.plans/1-cut-flow-size.md）。

超阈值的响应正文只在内核里保留前 N 字节，**转发给客户端的数据永远完整**：
tee 是旁路观察（原生 `Response.stream` callable 语义，与 `sse.py` 同一条
路子），callable 收到的 chunk 原样还给转发路径，只是自己少攒。

决策要点（详见计划 §1）：

* **边收边截**，不是收完再砍 —— 收完再砍峰值内存照样炸，等于没做。
* 挂点是 `responseheaders`：原生源码注释明确 stream 必须赶在 `response` 钩子
  之前换，`response` 里设已晚（与 SSE 同一约束）。
* 截断只动存储副本，**不改 Content-Length**（AGENTS.md §3 红线）——头部与原始
  大小保持一致，界面靠 `flow.metadata` 两个键判断截断，不靠头部。
* `Content-Length` 缺失的流不截：流式未知长度的 body 少见于大文件场景，留给后续。
* 三类流量让路：SSE（stream 槽位已被 `sse.py` 的 tee 占走，本 addon 挂在它
  之后，见到非 False 的 stream 直接退出）、重写就地作答（静态替换体不是流式
  语义）、断点命中（stream 一旦装上，响应期断点的 body 编辑既到不了客户端
  也会被流末写回冲掉 —— 断点本来就要求完整 body）。
* 阈值是内存副本（网关规则模式），不落原生 options —— 12.x 已没有
  `stream_large_bodies` / `store_streamed_bodies` 可挂。
"""

from collections.abc import Callable

from ferret.core.log import get_logger
from ferret.core.mitm.bindings import HTTPFlow
from ferret.core.mitm.rewrite import REWRITE_ANSWERED_KEY

log = get_logger("mitmproxy")

# 截断标记：写进 flow.metadata，详情面板（metadata 子树）、facade 快照（copy 带
# metadata）、保存的 .flows 文件都自然带上它；导出确认靠它计数（menus.py）。
BODY_TRUNCATED_KEY = "ferret.body_truncated"
BODY_FULL_SIZE_KEY = "ferret.body_full_size"

# 阈值上下限：下限 16 KB 是「再小就截到正常网页」的地板；上限 64 MB 之上的流
# 反正会过 `utils/http_parser.py::MAX_PRETTY_SIZE`（1 MB）的显示闸，再放宽只是
# 白占内存。默认值与 SpinBox 的默认一致（core/settings.py、apps/settings/views.py
# 各引用这里的常量，别各写一份）。
MIN_BODY_CUT_SIZE = 16 * 1024
MAX_BODY_CUT_SIZE = 64 * 1024 * 1024
DEFAULT_BODY_CUT_SIZE = 10 * 1024 * 1024


def clamp_body_cut_size(size: object) -> int:
    """把阈值收敛进 [MIN, MAX]：配置是用户可手改的纯文本，进来先过闸。

    入参刻意收成 `object`：真实来源是 `CONFIG.get`（可能被手改成任意 JSON 值），
    非数字 / None 一律退回默认而不是炸启动。
    """
    if not isinstance(size, (int, str, float)):
        return DEFAULT_BODY_CUT_SIZE
    try:
        value = int(size)
    except (TypeError, ValueError):
        return DEFAULT_BODY_CUT_SIZE
    return min(max(value, MIN_BODY_CUT_SIZE), MAX_BODY_CUT_SIZE)


def truncated_body_count(flows: list[HTTPFlow]) -> int:
    """选中集里正文被截断的条数：导出确认（menus.py）的计数来源。

    读的是 `flow.metadata` —— 调用方手里是 facade 快照（跨线程只读副本）或
    会话文件读回的 flow，都是安全读本，不碰活流量（AGENTS.md §3）。
    """
    return sum(1 for flow in flows if flow.metadata.get(BODY_TRUNCATED_KEY))


class _CutTap:
    """一条流的 tee 状态：攒前 N 字节，超出直通丢弃，流末把前缀写回 content。

    callable 本体是 :meth:`tee`（bound method），装进 `flow.response.stream` 后
    每个 chunk 走一趟：攒前缀 → 原样还给转发路径。所有字段只在 mitm 线程上碰，
    不加锁（与 `_SseTap` 同一个约定）。
    """

    def __init__(self, limit: int, on_end: Callable[[], None]) -> None:
        self.limit = limit
        self.buf = bytearray()
        # 实际收到的总字节数：Content-Length 只是触发判定的参考，服务器可能少发 /
        # 多发，metadata 里的 full_size 必须是实测值，否则导出确认的文案会说谎。
        self.received = 0
        self._on_end = on_end

    def tee(self, chunk: bytes) -> bytes:
        if not chunk:
            # 流末约定：mitmproxy 在转发路径收尾时以空 chunk 通知 callable。
            self._on_end()
            return chunk
        self.received += len(chunk)
        room = self.limit - len(self.buf)
        if room > 0:
            self.buf += chunk[:room]
        return chunk


class FerretCutAddon:
    """超阈值响应正文的存储截断：边收边截前 N 字节，转发侧一字不动。

    阈值快照经 `set_options` 整体换（运行期由 `MitmRuntime.apply_body_cut` 经
    `runtime.call` 投到 mitm 线程），每条流在 `responseheaders` 取当时快照 —
    热更不回溯已挂载的 tap（旧流按旧值收完，新流按新值，语义对齐 rewrite）。
    """

    def __init__(
        self,
        should_intercept: Callable[[HTTPFlow], bool] | None = None,
    ) -> None:
        # 断点判定走原生 `Intercept.should_intercept`（master 装配时注入 bound
        # method）：命中断点的流跳过截断，stream 槽位让给用户编辑语义。
        # 注意它在 responseheaders 阶段的超杀 —— 请求期规则（`~q`）此刻同样成立，
        # 这类流也会被跳过。保守方向是刻意的：多存一条完整 body 永远比毁掉一次
        # 断点编辑便宜。
        self._should_intercept = should_intercept
        self._enabled = False
        self._max_size = DEFAULT_BODY_CUT_SIZE
        self._taps: dict[str, _CutTap] = {}
        self._flows: dict[str, HTTPFlow] = {}

    def set_options(self, *, enabled: bool, max_size: int) -> None:
        """换阈值快照（只在 mitm 线程调用）。"""
        self._enabled = enabled
        self._max_size = max_size

    # —— addon 钩子 ——

    def responseheaders(self, flow: HTTPFlow) -> None:
        response = flow.response
        if response is None or not self._enabled:
            return
        # 重写引擎就地作答的流（与 SSE 同款让路，理由见 sse.py 注释）。
        if flow.metadata.get(REWRITE_ANSWERED_KEY):
            return
        # stream 槽位已被占（本 addon 挂在 SSE 之后，占它的就是 SSE 的 tee）——
        # 一个槽位养不起两个 tee，事件流优先级更高。
        if response.stream:
            return
        if self._should_intercept is not None and self._should_intercept(flow):
            return
        # 没有 Content-Length 就不截（见模块 docstring）。
        raw = response.headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(raw)
        except ValueError:
            return
        if declared <= self._max_size:
            return
        tap = _CutTap(self._max_size, on_end=lambda fid=flow.id: self._finish(fid))
        self._taps[flow.id] = tap
        self._flows[flow.id] = flow
        response.stream = tap.tee
        log.info(
            "响应正文超过截断阈值 %d 字节，只保留前缀: %s",
            self._max_size,
            flow.request.pretty_url,
        )

    # —— 对内 ——

    def _finish(self, flow_id: str) -> None:
        tap = self._taps.pop(flow_id, None)
        flow = self._flows.pop(flow_id, None)
        if tap is None or flow is None:
            return
        # 与 SSE 同一招：把 tee 攒下的字节补回 body，响应体页 / 保存 / HAR 导出
        # 都读它。别手动改 Content-Length —— 头部原样保留，截断靠 metadata 标记。
        if flow.response is not None:
            flow.response.data.content = bytes(tap.buf)
        flow.metadata[BODY_TRUNCATED_KEY] = True
        flow.metadata[BODY_FULL_SIZE_KEY] = tap.received

    def forget(self, flow_id: str) -> None:
        """flow 从 View 移除时丢状态。由 facade 的 remove/clear 路径调用。"""
        self._taps.pop(flow_id, None)
        self._flows.pop(flow_id, None)

    def clear(self) -> None:
        """整表清空（`clear_flows` 那条路）。"""
        self._taps.clear()
        self._flows.clear()
