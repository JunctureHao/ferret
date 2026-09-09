"""Qt 测试等待原语，全仓库只此一份，不要在新测试里复制嵌套 QEventLoop 版等待。

历史教训：旧等待是「connect + 嵌套 QEventLoop.exec + 单发 singleShot 超时」，
满载跑全量套件时约一半运行会随机挂掉一个启动型测试、单独复跑全过，且每次
轮到不同的测试。机理有两层：其一，信号在 connect 之后、exec 之前到达时，
``loop.quit()`` 落在 exec 启动之前，后来的 exec 没人叫醒，只能等满超时；其二，
单发 ready 信号本就可能被错过，而内核线程在满载下的调度延迟可达几十秒。
这里的轮询版每隔几毫秒泵一次事件队列，信号与状态转移总能被下一轮看到；
就绪检查直接读状态机（level-based），从机理上消除这类一次性竞态。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm import MitmRuntime, MitmRuntimeState

_POLL_INTERVAL_S = 0.01


def wait_for_signal(signal, *, timeout_ms: int = 30000) -> list:
    """等一个信号发射，返回收到的 args 元组列表；空列表即超时（超时语义与旧版一致）。

    轮询 ``processEvents`` 而不是嵌套 ``exec``：事件队列每轮都被泵到，
    没有可供错过的窗口。
    """
    values: list = []

    def receive(*args: object) -> None:
        values.append(args)

    signal.connect(receive)
    try:
        deadline = time.monotonic() + timeout_ms / 1000
        while not values and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            if not values:
                time.sleep(_POLL_INTERVAL_S)
    finally:
        signal.disconnect(receive)
    return values


def wait_ready(runtime: MitmRuntime, *, timeout_ms: int = 30000) -> None:
    """阻塞到内核进入 RUNNING；超时或启动失败时带 state / last_error 抛 AssertionError。

    读 `state` 而不是赌一发 `ready` 信号：轮询开始前信号可能已经发过，而
    状态是持续的；FAILED 则立刻抛真实错误，不再等满超时。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while runtime.state is MitmRuntimeState.STARTING and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if runtime.state is MitmRuntimeState.STARTING:
            time.sleep(_POLL_INTERVAL_S)
    if runtime.state is not MitmRuntimeState.RUNNING:
        raise AssertionError(
            f"runtime 未就绪: state={runtime.state}, last_error={runtime.last_error!r}"
        )


def start_runtime(runtime: MitmRuntime, *, timeout_ms: int = 30000) -> None:
    """start() 并等内核就绪；替换「start() + assertTrue(wait_for_signal(ready))」成对写法。"""
    runtime.start()
    wait_ready(runtime, timeout_ms=timeout_ms)


def wait_until(predicate: Callable[[], object], *, timeout_ms: int = 30000) -> bool:
    """轮询谓词直到为真；顺路泵事件队列，兼容依赖跨线程信号投递的条件。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return True
        QCoreApplication.processEvents()
        time.sleep(_POLL_INTERVAL_S)
    return bool(predicate())
