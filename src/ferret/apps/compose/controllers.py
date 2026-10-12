"""编辑页控制器：收 UI 的待发数据 → facade，收内核结果 → 视图。"""

from PySide6.QtCore import QObject, Signal

from ferret.core.log import get_logger
from ferret.core.mitm import ComposeResult, MitmFacade, MitmRuntimeState

log = get_logger("compose")


class ComposeController(QObject):
    """一次只允许在途一发的状态机：发送 → 等待落地 → 出结果 / 报错。"""

    sending_changed = Signal(bool)
    result_ready = Signal(object)  # ComposeResult
    send_failed = Signal(str, str)  # title, content

    def __init__(self, parent: QObject | None = None, *, mitm: MitmFacade):
        super().__init__(parent)
        self._mitm = mitm
        self._inflight_id = ""
        # facade 背后是 `runtime.call`（同步阻塞等 mitm 线程），但请求本身在
        # mitm 线程异步飞 —— call 只等「入队」这一步，毫秒级，不必上 FunctionTask。
        self._mitm.runtime.compose_result.connect(self._on_compose_result)
        # 兜底：内核停机/重启时清在途状态。`compose_result` 是单向握手，只有收到
        # 匹配结果才会解锁按钮；内核侧 `ComposeAddon.done()` 虽然会在正常停机时补发
        # 终态结果，但它明说不是所有拆除路径都能到达（崩溃、stop 超时、启动期停止，
        # 见 core/mitm/runtime.py 的 `_run_master` 注释）。缺了终态结果，`_inflight_id`
        # 会永久挂住、发送按钮永久禁用。所以再押内核离开 RUNNING 这一条可靠信号。
        self._mitm.runtime.state_changed.connect(self._on_runtime_state)

    @property
    def is_sending(self) -> bool:
        return bool(self._inflight_id)

    def send(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes | str,
        *,
        record: bool,
    ) -> None:
        """发起一次发送；在途未完成时直接忽略（按钮已被禁用，这是双保险）。

        body 收 bytes 是二进制直通（prefill 锁只读的那类内容），原样下发；
        str 走既有的 UTF-8 编码。facade 本就两个都收。
        """
        if self._inflight_id:
            return
        content = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            flow_id = self._mitm.send_custom_request(
                method,
                url,
                headers,
                content,
                record=record,
            )
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.send_failed.emit(self.tr("发送失败"), str(exc))
            return
        self._inflight_id = flow_id
        self.sending_changed.emit(True)

    def _on_compose_result(self, result: ComposeResult) -> None:
        """内核落地结果。只认当前在途那条；迟到的（内核重启前的旧请求）丢弃。"""
        if not self._inflight_id or result.flow_id != self._inflight_id:
            return
        self._inflight_id = ""
        self.sending_changed.emit(False)
        self.result_ready.emit(result)

    def _on_runtime_state(self, state: MitmRuntimeState) -> None:
        """内核离开 RUNNING 即解除在途：在途请求活不过内核停机/重启。

        与 `_on_compose_result` 幂等——谁先到谁清。正常重启时 `state_changed`
        （STOPPING）在主线程上同步先到，随后跨线程排队的 `compose_result` 因
        `_inflight_id` 已空被丢弃，不会重复弹窗。注意「停止」只回落抓包会话、
        内核仍 RUNNING 供 Compose 使用，不会误触发（见 AGENTS.md 启停语义）。
        """
        if state == MitmRuntimeState.RUNNING or not self._inflight_id:
            return
        self._inflight_id = ""
        self.sending_changed.emit(False)
        self.send_failed.emit(
            self.tr("发送已取消"), self.tr("内核已停止，本次发送已取消")
        )
