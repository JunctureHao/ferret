"""详情页的时序瀑布：详情字典 → 段列表（纯函数）+ 瀑布条控件 + 「时序」组的 lead。

与 `fields.py` / `messages.py` 同形态：纯函数在前、控件在后，`phases()` 不起
窗口就能测。时序块（`TimingPane`）作为 lead 挂进概览的「时序」卡（组头之下、
时刻行之上）—— 图定比例、行给精确值，一个组头一个故事。

两条硬约束（出处见 `.plans/timing-waterfall.md`）：

- **总耗时永远是一次减法**（``res_end - req_start``），绝不是各段求和 ——
  ``connection_strategy="eager"`` 下 HTTPS 的连接/TLS 发生在请求**之前**，明文
  HTTP 与 compose 回放则嵌在等待**之中**，求和两种情形都双记（原生 HAR 的
  ``"time"`` 就是这么错的，mitmproxy `savehar.py:240`）。
- **DNS 没有独立计时**：`asyncio.open_connection` 把 getaddrinfo 与 TCP 握手包在
  同一个 await 里，中间没有埋点。所以「DNS + 连接」是一个合成段，唯一能补充的
  诚实信息是「目标是 IP 字面量 ⇒ 这段里没有 DNS」。

段的分子分母逐字照抄原生 HAR 口径（`savehar.py:130-177`），泳道归属是单条
flow 能判到的极限：连接段结束时刻 ≤ 请求开始 ⇒ 「请求前」（eager 下首请求也
长这样，**不能**写成「复用连接」），> 请求开始 ⇒ 「嵌在等待里」。
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, isDarkTheme

from ferret.apps.common.flow.fields import format_duration
from ferret.utils.i18n import QT_TRANSLATE_NOOP

# 段名表是模块级常量，只做标记不求值（翻译器没装时求值会永久冻结成中文）。
_PHASE_LABELS: dict[str, str] = {
    "client_tls": QT_TRANSLATE_NOOP("FlowTiming", "客户端 TLS 握手"),
    "connect": QT_TRANSLATE_NOOP("FlowTiming", "DNS + 连接"),
    "tls": QT_TRANSLATE_NOOP("FlowTiming", "服务端 TLS 握手"),
    "send": QT_TRANSLATE_NOOP("FlowTiming", "发送"),
    "wait": QT_TRANSLATE_NOOP("FlowTiming", "等待响应"),
    "receive": QT_TRANSLATE_NOOP("FlowTiming", "接收"),
}

# 连接段三个 key 的顺序就是渲染顺序。
_CONN_PHASE_KEYS = ("client_tls", "connect", "tls")
_MAIN_PHASE_KEYS = ("send", "wait", "receive")

# 瀑布条行高与标签列宽。详情面板是窄栏，一行 18px 够看清相对长短。
_ROW_HEIGHT = 18
_LABEL_WIDTH = 120


def phase_label(key: str) -> str:
    """段 key → 已求值文案。"""
    return QCoreApplication.translate("FlowTiming", _PHASE_LABELS[key])


@dataclass(frozen=True, slots=True)
class Phase:
    """一段时序。``start`` / ``end`` 是 epoch 秒（绝对轴）；缺失的段根本不进列表。"""

    key: str  # "client_tls" | "connect" | "tls" | "send" | "wait" | "receive"
    start: float
    end: float
    lane: str  # "pre" 请求前 / "main" 请求轴上 / "nested" 嵌在等待里

    @property
    def ms(self) -> float:
        # 墙钟不是单调钟，NTP 步进能产生负差，夹住（与 models._duration_ms 同姿态）。
        return max(0.0, (self.end - self.start) * 1000)


@dataclass(frozen=True, slots=True)
class TimingModel:
    """``phases()`` 的输出：段列表 + 三个汇总数（都是一次减法，永不求和）。

    ``total_ms`` / ``ttfb_ms`` 为 ``None`` 表示「进行中」（响应未收完 / 响应头
    还没到），显示层据此写「进行中」而不是编一个数字。
    """

    phases: tuple[Phase, ...]
    total_ms: float | None
    ttfb_ms: float | None
    # 最后一个「请求前」段的结束时刻到请求开始的提前量；没有 pre 段为 None。
    pre_delta_ms: float | None
    # 目标是 IP 字面量 ⇒ True，「DNS + 连接」段里没有 DNS 成本。取不到目标为 None。
    target_is_ip: bool | None

    def lane(self, lane: str) -> list[Phase]:
        return [phase for phase in self.phases if phase.lane == lane]


def _phase(key: str, start, end, lane: str) -> Phase | None:
    """任一端缺失 ⇒ 该段不存在（显示层看不到它，而不是看到 ``—``）。"""
    if start is None or end is None:
        return None
    return Phase(key=key, start=float(start), end=float(end), lane=lane)


def phases(data: dict) -> TimingModel:
    """详情字典 → 时序模型。空字典不抛异常（详情面板先于数据构造）。

    取值别名一次性吸收详情字典的命名不对称（``req_time`` 是 start、
    ``res_time`` 是 end，历史键名不改，测试与 compose 都钉着）：

    - ``c0/ct`` = 客户端连接开始 / 客户端 TLS 握手完成
    - ``s0/st/ss`` = 服务端连接开始 / TCP 握手完成 / 服务端 TLS 握手完成
    - ``q0/q1`` = 请求开始 / 请求收完；``p0/p1`` = 响应头到达 / 响应收完
    """
    c0 = data.get("Front Connection Start")
    ct = data.get("Front TLS Handshake")
    s0 = data.get("Back Connection Start")
    st = data.get("Back TCP Handshake")
    ss = data.get("Back TLS Handshake")
    q0 = data.get("req_time")
    q1 = data.get("req_timestamp_end")
    p0 = data.get("res_timestamp_start")
    p1 = data.get("res_time")

    result: list[Phase] = []

    # 连接三段：结束时刻 ≤ q0 ⇒ 请求前（lane="pre"）；> q0 ⇒ 这次请求自己付了
    # 连接成本，嵌在等待里（lane="nested"）。s0 缺失 ⇒ 整组缺席（从未拨号）。
    # `st` 缺失（UDP/QUIC，上游只对 TCP 写 timestamp_tcp_setup）⇒ 连接段与
    # 服务端 TLS 段（分母是 st）一起缺席，不拿别的时刻冒充。
    conn_ends: list[float] = []
    for key, start, end in (
        ("client_tls", c0, ct),
        ("connect", s0, st),
        ("tls", st, ss),
    ):
        phase = _phase(key, start, end, "pre")
        if phase is None:
            continue
        if q0 is not None and phase.end > q0:
            phase = Phase(
                key=phase.key, start=phase.start, end=phase.end, lane="nested"
            )
        else:
            conn_ends.append(phase.end)
        result.append(phase)

    for key, start, end in (("send", q0, q1), ("wait", q1, p0), ("receive", p0, p1)):
        phase = _phase(key, start, end, "main")
        if phase is not None:
            result.append(phase)

    total_ms = None
    if q0 is not None and p1 is not None:
        total_ms = max(0.0, (p1 - q0) * 1000)
    ttfb_ms = None
    if q0 is not None and p0 is not None:
        ttfb_ms = max(0.0, (p0 - q0) * 1000)
    pre_delta_ms = None
    if conn_ends and q0 is not None:
        pre_delta_ms = max(0.0, (q0 - max(conn_ends)) * 1000)

    target_is_ip: bool | None = None
    address = data.get("Back Address")
    if address:
        host = str(address).rsplit(":", 1)[0].strip("[]")
        try:
            ipaddress.ip_address(host)
            target_is_ip = True
        except ValueError:
            target_is_ip = False

    return TimingModel(
        phases=tuple(result),
        total_ms=total_ms,
        ttfb_ms=ttfb_ms,
        pre_delta_ms=pre_delta_ms,
        target_is_ip=target_is_ip,
    )


def _phase_color(key: str, lane: str) -> QColor:
    """段色板，与 `FlowTableModel._semantic_color` 同族。"""
    dark = isDarkTheme()
    if lane == "nested":
        return QColor("#9a9a9a" if dark else "#6b6b6b")
    colors = {
        "client_tls": "#6ea8fe" if dark else "#1769aa",
        "connect": "#6ea8fe" if dark else "#1769aa",
        "tls": "#6ea8fe" if dark else "#1769aa",
        "send": "#62c174" if dark else "#22863a",
        "wait": "#e5b64b" if dark else "#a15c00",
        "receive": "#62c174" if dark else "#22863a",
    }
    return QColor(colors[key])


class WaterfallBar(QWidget):
    """一条绝对时间轴上的瀑布条：main 段实心，nested 段虚线框（嵌在等待里）。

    只画「请求轴上」与「嵌套」的段；「请求前」段不进这条轴 —— keep-alive 下连接
    可能早几十秒，同轴会把整条请求压成一像素，那几段由时序页分块显示、各自定标。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = TimingModel((), None, None, None, None)
        self.setMinimumHeight(_ROW_HEIGHT * 3 + 8)

    def set_model(self, model: TimingModel) -> None:
        self._model = model
        rows = model.lane("main") + model.lane("nested")
        self.setFixedHeight(_ROW_HEIGHT * max(len(rows), 1) + 8)
        self.update()

    def paintEvent(self, event) -> None:
        rows = self._model.lane("main") + self._model.lane("nested")
        if not rows:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            q0 = min(phase.start for phase in rows)
            p1 = max(phase.end for phase in rows)
            span = max(p1 - q0, 1e-9)
            bar_x = _LABEL_WIDTH
            bar_w = max(self.width() - bar_x - 4, 1)
            for row, phase in enumerate(rows):
                y = 4 + row * _ROW_HEIGHT
                painter.setPen(QColor(153, 153, 153, 180))
                painter.drawText(
                    0,
                    y,
                    _LABEL_WIDTH - 8,
                    _ROW_HEIGHT,
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                    phase_label(phase.key),
                )
                x = bar_x + int((phase.start - q0) / span * bar_w)
                w = max(int((phase.end - phase.start) / span * bar_w), 2)
                color = _phase_color(phase.key, phase.lane)
                if phase.lane == "nested":
                    fill = QColor(color)
                    fill.setAlpha(80)
                    painter.setBrush(fill)
                    pen = QPen(color)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    painter.setPen(pen)
                else:
                    painter.setBrush(color)
                    painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRoundedRect(x, y + 3, w, _ROW_HEIGHT - 6, 2, 2)
        finally:
            painter.end()


class TimingPane(QWidget):
    """「时序」组的 lead：汇总行 + 请求前块 + 瀑布条，挂在组头之下、时刻行之上。

    与「时序」组（`fields.SECTIONS`）合成一张卡：图定比例、下面的键值行给
    精确值，一个组头一个故事，整组一起折叠。没有可画内容（连一个时间戳都没
    有）时整块让位 —— 组本身此刻也被 `when` 藏掉，这里再兜一层底。

    汇总的口径：总耗时 / 首字节都是**一次减法**（与表格 Time 列同源）；响应
    未收完（SSE/流式）时写「进行中」，不编数字。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary_label = BodyLabel(self)
        self.summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.pre_label = CaptionLabel(self)
        self.pre_label.setWordWrap(True)

        self.pre_rows = QWidget(self)
        self.pre_layout = QVBoxLayout(self.pre_rows)
        self.pre_layout.setContentsMargins(0, 0, 0, 0)
        self.pre_layout.setSpacing(2)

        self.waterfall = WaterfallBar(self)

        # lead 块零外边距（卡片的 view 已有 6px 顶距）、无尾弹簧 —— 弹簧在概览
        # 整列的末尾，块自己加会把后面的卡片全推开。
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.pre_label)
        layout.addWidget(self.pre_rows)
        layout.addWidget(self.waterfall)

    def set_data(self, data: dict) -> None:
        model = phases(data)
        # 空字典 / 一个时间戳都没有：整块让位（此刻「时序」组也被 when 藏掉，
        # 这里再兜一层底）。
        if not model.phases and model.total_ms is None and model.ttfb_ms is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        translate = QCoreApplication.translate

        total = (
            format_duration(model.total_ms)
            if model.total_ms is not None
            else translate("FlowTiming", "进行中")
        )
        ttfb = (
            format_duration(model.ttfb_ms)
            if model.ttfb_ms is not None
            else translate("FlowTiming", "进行中")
        )
        self.summary_label.setText(
            translate("FlowTiming", "总耗时 {} · 首字节 {}").format(total, ttfb)
        )

        # 请求前块：连接三段早于本请求时分块显示、各自定标，明说「不计入总耗时」。
        pre = [phase for phase in model.lane("pre")]
        while self.pre_layout.count():
            item = self.pre_layout.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        if pre:
            self.pre_label.setText(
                translate(
                    "FlowTiming", "请求前（连接早于本请求 {} ms，不计入总耗时）"
                ).format(
                    f"{model.pre_delta_ms:.1f}"
                    if model.pre_delta_ms is not None
                    else "—"
                )
            )
            scale = max((phase.ms for phase in pre), default=0.0) or 1.0
            for phase in pre:
                row = QWidget(self.pre_rows)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(8)
                name = CaptionLabel(phase_label(phase.key), row)
                name.setFixedWidth(_LABEL_WIDTH)
                row_layout.addWidget(name)
                bar = _PreBar(phase.ms / scale, _phase_color(phase.key, "pre"), row)
                row_layout.addWidget(bar, 1)
                row_layout.addWidget(CaptionLabel(f"{phase.ms:.1f} ms", row))
                self.pre_layout.addWidget(row)
                if phase.key == "connect" and model.target_is_ip is not None:
                    # 「DNS + 连接」是合成量（上游不给 DNS 单独计时），这句必须写清，
                    # 否则会被当成「DNS 也能单独看」的 bug 报回来。
                    hint = (
                        translate("FlowTiming", "目标是 IP，这段不含 DNS")
                        if model.target_is_ip
                        else translate("FlowTiming", "目标是域名，这段含 DNS 解析")
                    )
                    caption = CaptionLabel(hint, self.pre_rows)
                    caption.setIndent(_LABEL_WIDTH + 8)
                    self.pre_layout.addWidget(caption)
            self.pre_label.show()
            self.pre_rows.show()
        elif data.get("Back Connection Start") is None and data.get("res_time"):
            self.pre_label.setText(translate("FlowTiming", "未建立服务端连接"))
            self.pre_label.show()
            self.pre_rows.hide()
        else:
            self.pre_label.hide()
            self.pre_rows.hide()

        self.waterfall.set_model(model)


class _PreBar(QWidget):
    """「请求前」块里的一段小横条：块内自定标，与请求轴无关。"""

    def __init__(self, ratio: float, color: QColor, parent: QWidget) -> None:
        super().__init__(parent)
        self._ratio = ratio
        self._color = color
        self.setFixedHeight(10)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        try:
            w = max(int(self.width() * self._ratio), 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._color)
            painter.drawRoundedRect(0, 2, w, self.height() - 4, 2, 2)
        finally:
            painter.end()
