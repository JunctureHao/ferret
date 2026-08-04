import logging
import logging.handlers
import sys
from collections import deque
from threading import Lock

from PySide6.QtCore import QObject, Signal

from ferret.core.settings import get_config_dir

LOG_MAXLEN = 2000
FILE_MAX_BYTES = 5 * 1024 * 1024
FILE_BACKUP_COUNT = 3

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class FerretFormatter(logging.Formatter):
    """全局日志格式化器。

    支持 ``raw`` 标记：当 LogRecord 带有 ``raw=True`` 属性
    （通过 ``log.info(msg, extra={"raw": True})`` 传入）时，
    把正常前缀（时间/级别/logger 名）替换为等长空格，使消息
    从冒号后同一列开始，模拟 mitmproxy 终端日志的对齐效果。
    多行消息（如请求行 + 响应行）的每一行都会补齐前缀。
    """

    # 日期时间格式：YYYY-MM-DD HH:MM:SS，时间戳外层包裹 [time]
    _default_datefmt = "%Y-%m-%d %H:%M:%S"

    def __init__(self, fmt=None, datefmt=None, **kwargs):
        if datefmt is None:
            datefmt = self._default_datefmt
        super().__init__(fmt, datefmt, **kwargs)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # 把时间戳包裹成 [time]，例如 [2026-08-04 17:03:04]
        base = super().formatTime(record, datefmt)
        return f"[{base}]"

    def format(self, record: logging.LogRecord) -> str:
        # 显示时剥掉 ferret. 前缀，只留子名（ferret 自身显示为空）。
        saved = record.name
        if saved == "ferret":
            record.name = ""
        elif saved.startswith("ferret."):
            record.name = saved[7:]

        try:
            if getattr(record, "raw", False):
                if record.exc_info:
                    return super().format(record)
                msg = record.getMessage()
                full = super().format(record)
                prefix = " " * (len(full) - len(msg))
                return prefix + msg.replace("\n", "\n" + prefix)
            return super().format(record)
        finally:
            record.name = saved


class LogEmitter(QObject):
    """把日志记录转成 Qt 信号，跨线程安全投递到 GUI 线程。"""

    # levelname, logger_name, formatted_message
    log_received = Signal(str, str, str)


class RingBufferHandler(logging.Handler):
    """日志处理器：入队环形缓冲，并经由 ``LogEmitter`` 广播给 UI。

    在任意线程（GUI 线程或 mitmproxy worker 线程）的 ``emit`` 都会被调用，
    因此用 ``Lock`` 保护 deque 的读写。``emit`` 末端的 ``LogEmitter.log_received``
    信号跨线程发射由 Qt 自动队列化到 GUI 线程，UI 端无需额外同步。
    """

    def __init__(self, emitter: "LogEmitter", maxlen: int = LOG_MAXLEN) -> None:
        super().__init__()
        self._emitter = emitter
        self._buf: deque[logging.LogRecord] = deque(maxlen=maxlen)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        with self._lock:
            self._buf.append(record)
        self._emitter.log_received.emit(
            record.levelname, record.name, self.format(record)
        )

    def recent(self, n: int | None = None) -> list[logging.LogRecord]:
        """返回最近 n 条记录（用于 UI 初始化回填）。"""
        with self._lock:
            items = list(self._buf)
        return items if n is None else items[-n:]


# ── 模块级单例 ──
_emitter: LogEmitter | None = None
_handler: RingBufferHandler | None = None
# handler 挂在 ferret 这棵独立的树上：业务子 logger 归属 ferret.* 可命中，
# 而第三方库（hpack/mitmproxy.proxy.* 等）不在树下，自然被隔离，不会冒出噪音。
_logger = logging.getLogger("ferret")


def init_logging() -> None:
    """初始化全局日志设施（幂等，可重复调用）。

    必须在 QApplication 存在后调用（``LogEmitter`` 是 QObject）。
    """

    global _emitter, _handler
    if _emitter is not None:
        return

    _emitter = LogEmitter()
    _handler = RingBufferHandler(_emitter)
    _handler.setFormatter(FerretFormatter(_FMT))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.DEBUG)
    _logger.propagate = False

    # 落盘：配置目录下的滚动日志
    log_file = get_config_dir() / "ferret.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=FILE_MAX_BYTES,
        backupCount=FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(FerretFormatter(_FMT))
    _logger.addHandler(file_handler)

    # 终端输出：让 `uv run ferret` 的控制台实时滚动日志（UI 面板/文件不受影响）。
    # Windows 控制台下 sys.stdout 默认 errors="backslashreplace"，特殊字符不会抛错；
    # Nuitka windowed 模式下 sys.stdout 为 None，自动跳过。
    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(FerretFormatter(_FMT))
        _logger.addHandler(console_handler)


def get_logger(name: str | None = None) -> logging.Logger:
    """获取业务 logger

    各模块自行定义子名（如 "cert"、"mitmproxy"），内部归属 ``ferret.<name>`` 层级，
    从而能传播到挂在 ``ferret`` 上的 handler；显示时由 ``FerretFormatter`` 剥掉
    ``ferret.`` 前缀，只输出短名（如 ``cert:``）。

    Args:
        name(str): 子名；为 None 返回根 ``ferret`` logger
    """
    return logging.getLogger("ferret" if name is None else f"ferret.{name}")


def get_emitter() -> LogEmitter | None:
    """返回 UI 用于订阅日志信号的 ``LogEmitter``（未初始化时为 None）。"""
    return _emitter


# 业务代码便捷别名：from ferret.core.log import log（应用级默认 logger，显示 ferret:）
log = _logger
