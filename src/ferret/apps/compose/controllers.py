"""编辑页控制器：收 UI 的待发数据 → facade，收内核结果 → 视图。"""

from PySide6.QtCore import QObject, Signal

from ferret.core.log import get_logger
from ferret.core.mitm import ComposeResult, MitmFacade

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

    @property
    def is_sending(self) -> bool:
        return bool(self._inflight_id)

    def send(
        self,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: str,
        *,
        record: bool,
    ) -> None:
        """发起一次发送；在途未完成时直接忽略（按钮已被禁用，这是双保险）。"""
        if self._inflight_id:
            return
        try:
            flow_id = self._mitm.send_custom_request(
                method,
                url,
                headers,
                body.encode("utf-8") if body else b"",
                record=record,
            )
        except (ValueError, RuntimeError, TimeoutError) as exc:
            self.send_failed.emit(self.tr("Send failed"), str(exc))
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
