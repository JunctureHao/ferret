from PySide6.QtCore import QObject, QThread, Signal

from ferret.core.http import HttpClient, HttpMethod


class _SendWorker(QThread):
    """后台线程：运行 asyncio 事件循环发请求，避免阻塞 UI。

    属于 RequestController 的内部实现，不对外暴露。
    """

    finished = Signal(int, str, str)  # status_code, body, http_version
    failed = Signal(str)  # error message

    def __init__(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
    ):
        super().__init__()
        self._method = method
        self._url = url
        self._headers = headers
        self._params = params
        self._json = json
        self._data = data

    def run(self):
        import asyncio

        async def _do():
            async with HttpClient() as client:
                resp = await client.send(
                    self._method,
                    self._url,
                    headers=self._headers,
                    params=self._params,
                    json=self._json,
                    data=self._data,
                )
                return resp.status_code, resp.text, resp.http_version

        try:
            status, body, version = asyncio.run(_do())
            self.finished.emit(status, body, version)
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class RequestController(QObject):
    """请求控制器 — 持有业务数据、发送能力与线程生命周期，UI 只通过它发请求。"""

    # 发完请求后广播结果，各 UI 面板订阅
    responseReady = Signal(int, str, str)  # (status_code, body, http_version)
    errorOccurred = Signal(str)  # 发送失败时的错误信息

    def __init__(self, parent=None):
        super().__init__(parent)
        self.http_method: list[str] = [m.value for m in HttpMethod]
        self._worker: _SendWorker | None = None

    def get_http_methods(self) -> list[str]:
        return self.http_method

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json: dict | None = None,
        data: dict | None = None,
    ) -> None:
        """发送请求。线程细节完全封装在 controller 内，UI 无需感知。

        正在发送中会忽略重复调用（防重入）。
        """
        if self._worker is not None and self._worker.isRunning():
            return

        self._worker = _SendWorker(
            method,
            url,
            headers=headers,
            params=params,
            json=json,
            data=data,
        )
        self._worker.finished.connect(self._on_response_ready)
        self._worker.failed.connect(self._on_response_failed)
        self._worker.start()

    # ── worker 回调：把线程结果转成业务信号广播 ──
    def _on_response_ready(self, status: int, body: str, version: str):
        self.responseReady.emit(status, body, version)

    def _on_response_failed(self, error: str):
        self.errorOccurred.emit(error)
