"""详情面板的字段声明表与卡片式渲染：一份数据，一层循环。

原来 `OverviewTree.set_data` 是近 300 行几乎逐字重复的 `QTreeWidgetItem` 构造 ——
每个分组重新造一遍粗体字号、`setTextAlignment` 成对抄、空值判断抄七遍。字段本身
（标签、取哪个键、怎么格式化、什么时候显示）其实是纯数据，抽出来之后渲染只剩一层
循环，加字段不用再碰渲染代码。

上一期抽出规格表时渲染还是那棵树；这一期树整个退役，换成 `OverviewPane` 的单列
平铺分组流（组头 + 键值网格，无卡片底框）。中间那一层 `section_rows()` 是**纯函数**
：分组 + 数据字典 → 已求值的 `Row` 列表。「整组没东西就整段不显示」「`when` 说不
显示就整组不露面」这两条规则因此不用起窗口就能测，`FieldCard` 那侧只剩「一串 Row
怎么摆进 QGridLayout」。

字段标签在这里**只做标记、不求值**：类体和模块级一样在导入期跑完，而
`core/application.py` 在顶层就 import 了 MainWindow —— 那时翻译器还没装，
求值出来的文案会永久冻结成中文。求值统一走 `field_label()` / `section_title()`。
值那一侧是 lambda，调用时才跑，所以可以直接写 `QCoreApplication.translate`。

没被 `QT_TRANSLATE_NOOP` 包住的标签（``Code`` / ``SNI`` / ``Common Name`` 之类）是
**故意**不译的专有名词，和搬过来之前保持一致：`translate()` 查不到就原样返回源文本，
所以它们照旧显示原文，也不会进 `en_GB.ts`。

翻译 context 统一是 ``"FlowFields"``，而且**必须逐字写成字面量** ——
`tests/core/test_i18n.py` 的 `test_translate_always_names_its_context_literally` 只给
`utils/i18n.py` 开了口子，context 写成变量整条都提取不到。
"""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon,
    SingleDirectionScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
)

from ferret.apps.common.font import FontManager
from ferret.core.mitm import human
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

# 视作“没有值”的取值结果，和搬过来之前的判断逐字一致。
EMPTY = (None, "", "N/A", "-")

# 标签列压暗到约 62% 不透明度。和 `edit/theme.py::EditorPalette._mono` 同一套办法
# —— 暗色叠半透明白、亮色叠半透明黑，底图（卡片色、主题色）换了也不会突然对不上。
# `setTextColor(light, dark)` 两套一起给，主题切换由 `FluentLabelBase` 自己重贴。
_LABEL_ALPHA = 160

# 标签列的最小宽度。窄到一定程度值那侧就没有换行的余地了，这一列先保住。
_LABEL_COLUMN_WIDTH = 108


@dataclass(frozen=True, slots=True)
class Field:
    """一行字段。

    Args:
        label: `QT_TRANSLATE_NOOP` 标记（或故意不译的专有名词），不在这里求值。
        source: 取值方式 —— 字符串表示 `data` 的键，可调用对象表示当场算。
        fmt: 拿到值之后的格式化，`None` 表示 `str()`。
        tip: 悬浮提示的标记，同样只做标记、由 `field_tip()` 求值。
        mono: 等宽显示（id、指纹、序列号、curl 这类）。
        always: 空值也占一行（显示 ``-``）。默认空值直接跳过。
    """

    label: str
    source: str | Callable[[dict], object]
    fmt: Callable[[object], str] | None = None
    tip: str | None = None
    mono: bool = False
    always: bool = False


@dataclass(frozen=True, slots=True)
class Section:
    """一个分组，也就是概览里的一段。

    Args:
        title: 分组标题的标记。分组必须有标题，空串只在嵌套小节里还有意义。
        fields: 组内条目，按声明顺序渲染；嵌一个 `Section` 就是一个次级小节，
            渲染成组内跨两列的小标题行，不会另开一段。
        when: 整组的显示条件，`None` 表示只要组内有可见字段就显示。
        collapsed: 默认折叠（冷门分组）。折叠与否只是**初始状态**，每组都能收。
    """

    title: str
    fields: tuple["Field | Section", ...]
    when: Callable[[dict], bool] | None = None
    collapsed: bool = False


@dataclass(frozen=True, slots=True)
class Row:
    """一条已求值的行 —— `section_rows()` 的输出，`FieldCard` 的输入。

    Args:
        label: 已求值的标签文案。
        value: 已求值的值文案；`heading` 行固定是空串。
        mono: 等宽显示。
        tip: 已求值的悬浮提示，`None` 表示没有。
        heading: 次级小节的小标题行 —— 跨两列，没有值。
    """

    label: str
    value: str = ""
    mono: bool = False
    tip: str | None = None
    heading: bool = False


def section_title(section: Section) -> str:
    """分组标题求值；无标题的分组返回空串。"""
    if not section.title:
        return ""
    return QCoreApplication.translate("FlowFields", section.title)


def field_label(field: Field) -> str:
    """字段标签求值。查不到译文时 Qt 原样返回源文本，专有名词就靠这个保持英文。"""
    return QCoreApplication.translate("FlowFields", field.label)


def field_tip(field: Field) -> str | None:
    """字段悬浮提示求值；没写提示就返回 `None`（调用方据此决定不装 tooltip）。"""
    if not field.tip:
        return None
    return QCoreApplication.translate("FlowFields", field.tip)


def field_value(field: Field, data: dict) -> str | None:
    """算出这一行该显示的文本；返回 `None` 表示这一行整个跳过。

    `always` 的字段空值显示 ``-``，其余空值跳过。空列表、空元组、空字典都算空值
    —— 原来它会渲染成一行「0 项」或者一行空白，那既不是值也不是提示。
    """
    if isinstance(field.source, str):
        raw = data.get(field.source)
    else:
        raw = field.source(data)
    if raw in EMPTY or (isinstance(raw, (list, tuple, dict)) and not raw):
        return "-" if field.always else None
    return field.fmt(raw) if field.fmt is not None else str(raw)


def section_rows(section: Section, data: dict) -> list[Row]:
    """一个分组 + 一份数据 → 已求值的行；整组没东西可显示就返回空表。

    这是渲染与规格之间唯一的接缝，也是**纯函数** —— 卡片那侧只要照单摆格子，
    「空组不出现」「`when` outranks 组内字段」「小节没内容连小标题一起不出现」
    三条规则都在这里，不起窗口就能测。

    嵌套 `Section` 不会另开一张卡：它变成一行 `heading` 跨两列的小标题，后面跟
    自己的行。证书的「主体」「签发者」、连接的「前端」「后端」就是这么来的。
    """
    if section.when is not None and not section.when(data):
        return []
    rows: list[Row] = []
    for entry in section.fields:
        if isinstance(entry, Section):
            nested = section_rows(entry, data)
            if not nested:
                continue
            title = section_title(entry)
            if title:
                rows.append(Row(label=title, heading=True))
            rows.extend(nested)
            continue
        value = field_value(entry, data)
        if value is None:
            continue
        rows.append(
            Row(
                label=field_label(entry),
                value=value,
                mono=entry.mono,
                tip=field_tip(entry),
            )
        )
    return rows


def format_time(ts) -> str:
    """时间戳 → 本地时间字符串；空值显示 ``-``。

    ``human.format_timestamp`` 对 ``None`` 会返回“当前时间”（``time.localtime(None)``），
    所以空值必须自己挡掉。
    """
    return human.format_timestamp(ts) if ts else "-"


def _ms(value: object) -> str:
    """毫秒时长。整句留在外面，交给 `format()`，否则 lupdate 提取不到。"""
    return f"{float(value):.1f} ms"  # ty: ignore[invalid-argument-type]


def _join(value: object) -> str:
    """列表值拍平成一行。

    原来这里是「N 项」加一层子节点，只有 ALPN offers 和 cipher list 两个字段用得上；
    卡片式渲染的值是会换行的 `BodyLabel`，一行逗号分隔更直观，也少两条拼接文案。
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(entry) for entry in value)
    return str(value)


def _pairs(value: object) -> str:
    """字典值拍平成 ``k=v`` 逗号串（`Flow Metadata` 这类自由字典）。"""
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _marker(value: object) -> str:
    """`flow.marked` → 面向人的一句话。

    原生存的是 emoji 短码（`MARKER_DEFAULT` 是 ``:default:``），直接铺在卡片上是一串
    没人认得的记号。界面上标记只有「有 / 没有」两种状态，所以有值就是「已标记」，
    短码本身没有信息量。空值这一行本来就不会出现（`field_value` 跳空值）。
    """
    if not value:
        return "-"
    return QCoreApplication.translate("FlowFields", "标记")


def _size_of(*keys: str) -> Callable[[dict], object]:
    """几个字节数键相加，按人类可读格式化；缺的键算 0。

    缺键算 0（而不是跳过这一行）是刻意的：只抓到请求的流量照旧会显示 ``0b`` 的响应
    大小，和搬过来之前一致。
    """

    def read(data: dict) -> object:
        return human.pretty_size(sum(int(data.get(key) or 0) for key in keys))

    return read


def _any_value(*keys: str) -> Callable[[dict], bool]:
    """任一键有非空值 —— 连接、TLS、证书三组历来按这个判断整组是否露面。"""

    def test(data: dict) -> bool:
        return any(data.get(key) for key in keys)

    return test


def _any_present(*keys: str) -> Callable[[dict], bool]:
    """任一键存在，``0`` 也算 —— 时序与大小两组历来按这个判断（`is not None`）。"""

    def test(data: dict) -> bool:
        return any(data.get(key) is not None for key in keys)

    return test


# `state` 键 → 状态文案标记。求值在 `_state()` 里。
# 只有这三条：`infer_state()` 只会返回 ``request`` / ``complete`` / ``error``。
# 早先还挂着 ``request_headers`` / ``response_headers`` 两条，配的是产出侧同样
# 永远进不去的两条分支 —— 两边一起清掉了。
_STATE_LABELS: dict[str, str] = {
    "request": QT_TRANSLATE_NOOP("FlowFields", "请求已发送"),
    "complete": QT_TRANSLATE_NOOP("FlowFields", "已完成"),
    "error": QT_TRANSLATE_NOOP("FlowFields", "错误"),
}


def _state(data: dict) -> str:
    """`state` 键 → 面向人的状态文案；不认识的状态显示「未知」。"""
    return resolve_marker(
        _STATE_LABELS,
        data.get("state", ""),
        "FlowFields",
        QCoreApplication.translate("FlowFields", "未知"),
    )


def _code_with_reason(data: dict) -> object:
    """概要那一行的状态码：有 reason 就跟在后面（``200 OK``）。

    响应组里 `Status Code` 和 `Reason` 仍然各占一行 —— 那一组是给人逐项核对的；
    概要这张卡是给人一眼扫的，两个半句合成一句才读得下去。
    """
    code = data.get("Status Code")
    if code in EMPTY:
        return None
    reason = data.get("Reason")
    return f"{code} {reason}" if reason else code


# 连接的「前端」「后端」两个小节共用的字段，只差 ``Front`` / ``Back`` 键前缀。
# `Via` / `Address` 只有后端产出（前端那侧 `Client.address` 是 `peername` 的废弃
# 别名，产出侧刻意没写），前端渲染时这两行自然缺席 —— 一张表管两侧。
_CONN_PEER_FIELDS: tuple[tuple[str, str], ...] = (
    (QT_TRANSLATE_NOOP("FlowFields", "客户端 地址"), "Client Address"),
    (QT_TRANSLATE_NOOP("FlowFields", "客户端 端口"), "Client Port"),
    (QT_TRANSLATE_NOOP("FlowFields", "服务器地址"), "Server Address"),
    (QT_TRANSLATE_NOOP("FlowFields", "服务端 端口"), "Server Port"),
    (QT_TRANSLATE_NOOP("FlowFields", "请求地址"), "Address"),
    (QT_TRANSLATE_NOOP("FlowFields", "上游代理"), "Via"),
    (QT_TRANSLATE_NOOP("FlowFields", "连接状态"), "Connection State"),
    (QT_TRANSLATE_NOOP("FlowFields", "传输协议"), "Transport Protocol"),
    (QT_TRANSLATE_NOOP("FlowFields", "连接错误"), "Connection Error"),
)

# 证书的「主体」「签发者」两个小节共用的六项，只差 ``Subject`` / ``Issuer`` 键前缀。
_CERT_NAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("Common Name", "Common Name"),
    (QT_TRANSLATE_NOOP("FlowFields", "国家"), "Country"),
    (QT_TRANSLATE_NOOP("FlowFields", "省（州）"), "State"),
    (QT_TRANSLATE_NOOP("FlowFields", "地区"), "Locality"),
    (QT_TRANSLATE_NOOP("FlowFields", "组织"), "Organization"),
    (QT_TRANSLATE_NOOP("FlowFields", "单位"), "Organizational Unit"),
)

# TLS 两张卡共用的六项，只差 ``TLS`` / ``Client TLS`` 键前缀。
_TLS_FIELDS: tuple[tuple[str, str], ...] = (
    (QT_TRANSLATE_NOOP("FlowFields", "版本"), "Version"),
    ("SNI", "SNI"),
    ("ALPN", "ALPN Offers"),
    (QT_TRANSLATE_NOOP("FlowFields", "选择ALPN"), "ALPN Selected"),
    (QT_TRANSLATE_NOOP("FlowFields", "加密算法列表"), "Cipher List"),
    (QT_TRANSLATE_NOOP("FlowFields", "选择算法"), "Cipher"),
)

# 值是列表、需要 `_join` 拍平的那两项。
_TLS_JOINED = frozenset({"ALPN Offers", "Cipher List"})

# 整组是否露面的判断依据，逐字沿用搬过来之前的键集合。
_TLS_KEYS: tuple[str, ...] = tuple(f"TLS {key}" for _label, key in _TLS_FIELDS)

_CLIENT_TLS_KEYS: tuple[str, ...] = tuple(
    f"Client TLS {key}" for _label, key in _TLS_FIELDS
)

_CERT_KEYS: tuple[str, ...] = (
    *(f"Subject {key}" for _label, key in _CERT_NAME_FIELDS),
    *(f"Issuer {key}" for _label, key in _CERT_NAME_FIELDS),
    "Not Before",
    "Not After",
    "Fingerprint SHA256",
    "Serial Number Hex",
    "Certificate Key",
    "Certificate Alt Names",
    "Certificate Chain Depth",
)

_TIME_KEYS: tuple[str, ...] = (
    "Flow Created",
    "Front TLS Handshake",
    "req_time",
    "req_timestamp_end",
    "req_duration",
    "Back TCP Handshake",
    "Back TLS Handshake",
    "res_timestamp_start",
    "res_time",
    "res_duration",
    "Duration",
    "Front Connection End",
    "Back Connection End",
)

# 大小组的四种口径，每侧三个键加一个合计：
#
# * ``*_headers_size`` —— 头部字节，走 `assemble_*_head()` 拿真实线格式；
# * ``*_wire_size`` —— 报文体的**线上**字节（压缩后），和表格 Size 列同源；
# * ``*_decoded_size`` —— 报文体**解压后**的字节，只在和线上不一样时才显示；
# * ``*_total_size`` / ``total_size`` —— 头部 + 线上，也就是这条报文实际占的字节。
#
# 改造前只有 ``req_size`` / ``res_size`` 一个含混的「大小」（量的是解压后），
# 却和头部字节加在一起当合计，于是同一条 gzip 响应在表格和详情里能差好几倍。
_SIZE_KEYS: tuple[str, ...] = (
    "req_headers_size",
    "req_wire_size",
    "req_decoded_size",
    "req_total_size",
    "res_headers_size",
    "res_wire_size",
    "res_decoded_size",
    "res_total_size",
    "total_size",
)


def _decoded_size(wire_key: str, decoded_key: str) -> Callable[[dict], str | None]:
    """「解压后」这一行 —— 和线上字节一样大就不显示。

    绝大多数报文没压缩，两个数字一模一样，并排摆两行相同的值只是噪音。这一行出现
    就说明这条报文确实压缩过、两个口径确实不是一回事，读的人也就知道该看哪个。
    """

    def read(data: dict) -> str | None:
        decoded = data.get(decoded_key)
        if decoded is None or int(decoded) == int(data.get(wire_key) or 0):
            return None
        return human.pretty_size(int(decoded))

    return read


def _peer_section(title: str, prefix: str, id_key: str) -> Section:
    """连接的「前端」「后端」小节 —— 同名字段，只差键前缀。"""
    return Section(
        title=title,
        fields=(
            Field("ID", id_key, mono=True),
            *(Field(label, f"{prefix} {key}") for label, key in _CONN_PEER_FIELDS),
        ),
    )


def _cert_name_section(title: str, prefix: str) -> Section:
    """证书的「主体」「签发者」小节 —— 六项同名字段，只差键前缀。

    这两组不再用 `always=True`：证书里没有的项就不占行。上一期这里空值也硬占一行，
    加上 models 压根没产出这六项，整组渲染出来是 12 行字面 ``-`` —— 看着像「读到了
    证书但每项都是空的」，其实是根本没读。现在 `certificate_fields()` 产出实际有的
    项，缺哪项少哪行，整张证书都没有就整组不显示。
    """
    return Section(
        title=title,
        fields=tuple(
            Field(label, f"{prefix} {key}") for label, key in _CERT_NAME_FIELDS
        ),
    )


def _tls_section(title: str, prefix: str, keys: tuple[str, ...]) -> Section:
    """TLS 的「服务端」「客户端」两张卡 —— 六项同名字段，只差键前缀。

    客户端一侧历来完全看不到：握手用了哪个版本、哪个套件、有没有走 ALPN 全都没有
    出口。产出侧上一期补齐了 `Client TLS *`，这里把它接上。
    """
    return Section(
        title=title,
        collapsed=True,
        when=_any_value(*keys),
        fields=tuple(
            Field(label, f"{prefix} {key}", fmt=_join if key in _TLS_JOINED else None)
            for label, key in _TLS_FIELDS
        ),
    )


# 概览的完整规格：声明顺序就是渲染顺序，一个顶层分组一张卡。
SECTIONS: tuple[Section, ...] = (
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "概要"),
        fields=(
            Field(QT_TRANSLATE_NOOP("FlowFields", "状态"), _state),
            Field(QT_TRANSLATE_NOOP("FlowFields", "方法"), "Method"),
            Field("URL", "URL"),
            Field("Code", _code_with_reason),
            Field(QT_TRANSLATE_NOOP("FlowFields", "协议"), "Protocol"),
            Field("Content Type", "Response Content-Type"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "内容编码"),
                "Response Content-Encoding",
            ),
            Field("Keep Alive", "Keep Alive"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "代理协议"), "Proxy Protocol"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "服务器地址"), "Server Address"),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "耗时"),
        when=_any_present(*_TIME_KEYS),
        fields=(
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "流量创建"),
                "Flow Created",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "客户端 TLS 握手"),
                "Front TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "请求开始"),
                "req_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "请求结束"),
                "req_timestamp_end",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "请求时长"),
                "req_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "TCP 握手"),
                "Back TCP Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "服务端 TLS 握手"),
                "Back TLS Handshake",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "响应开始"),
                "res_timestamp_start",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "响应结束"),
                "res_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "响应时长"),
                "res_duration",
                fmt=_ms,
            ),
            Field(QT_TRANSLATE_NOOP("FlowFields", "总时长"), "Duration"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "客户端连接结束"),
                "Front Connection End",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "服务端连接结束"),
                "Back Connection End",
                fmt=format_time,
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "大小"),
        when=_any_present(*_SIZE_KEYS),
        fields=(
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "请求"),
                _size_of("req_total_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 请求头"),
                _size_of("req_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 请求体（线上）"),
                _size_of("req_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 请求体（解压后）"),
                _decoded_size("req_wire_size", "req_decoded_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "响应"),
                _size_of("res_total_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 响应头"),
                _size_of("res_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 响应体（线上）"),
                _size_of("res_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- 响应体（解压后）"),
                _decoded_size("res_wire_size", "res_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("FlowFields", "总计"), _size_of("total_size")),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "连接"),
        collapsed=True,
        when=_any_value("Connection ID", "Connection Time"),
        fields=(
            Field(QT_TRANSLATE_NOOP("FlowFields", "时间"), "Connection Time"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "代理模式"), "Client Proxy Mode"),
            _peer_section(
                QT_TRANSLATE_NOOP("FlowFields", "前端"), "Front", "Connection ID"
            ),
            _peer_section(
                QT_TRANSLATE_NOOP("FlowFields", "后端"), "Back", "Back Connection ID"
            ),
        ),
    ),
    _tls_section(QT_TRANSLATE_NOOP("FlowFields", "TLS · 服务端"), "TLS", _TLS_KEYS),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "TLS · 客户端"),
        collapsed=True,
        when=_any_value(*_CLIENT_TLS_KEYS, "Client Mitm Certificate"),
        fields=(
            *_tls_section("", "Client TLS", _CLIENT_TLS_KEYS).fields,
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "中间人证书"),
                "Client Mitm Certificate",
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "服务端证书"),
        collapsed=True,
        when=_any_value(*_CERT_KEYS),
        fields=(
            _cert_name_section("Subject", "Subject"),
            _cert_name_section(QT_TRANSLATE_NOOP("FlowFields", "签发者"), "Issuer"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "开始时间"), "Not Before"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "截止时间"), "Not After"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "已过期"), "Certificate Expired"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "是否 CA"), "Certificate Is CA"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "公钥"), "Certificate Key"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "备用名称"),
                "Certificate Alt Names",
                fmt=_join,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "证书链层数"),
                "Certificate Chain Depth",
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "证书链"),
                "Certificate Chain",
                fmt=_join,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "指纹"),
                "Fingerprint SHA256",
                mono=True,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "序列号"),
                "Serial Number Hex",
                mono=True,
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "错误"),
        when=_any_value("Error Message"),
        fields=(
            Field(QT_TRANSLATE_NOOP("FlowFields", "错误信息"), "Error Message"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "时间"), "Error Time", fmt=format_time
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "流量元数据"),
        collapsed=True,
        when=_any_value("Flow ID"),
        fields=(
            Field("ID", "Flow ID", mono=True),
            Field(QT_TRANSLATE_NOOP("FlowFields", "类型"), "Flow Type"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "状态版本"), "Flow Version"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "存活"), "live"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "已拦截"), "Intercepted"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "已修改"), "Modified"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "重放"), "is_replay"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "标记"), "marked", fmt=_marker),
            Field(QT_TRANSLATE_NOOP("FlowFields", "备注"), "comment"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "元数据"), "Flow Metadata", fmt=_pairs
            ),
        ),
    ),
)


class FieldCard(QWidget):
    """一个分组一段：组头（标题 + 整组复制 + 折叠，整行可点）+ 两列网格。

    **刻意不用卡片底框**（`HeaderCardWidget` / `SimpleCardWidget` 那套）：概览一屏
    十来个分组，每段再套一圈圆角底色就成了卡片墙，扫读时视线要在框与框之间跳。
    现在只剩「加粗标题 + 键值行」，组与组之间靠间距分界。

    也**刻意没用 `SimpleExpandGroupSettingCard` 做折叠组**。那个卡自己按
    ``viewLayout.sizeHint().height()`` 调 `setFixedHeight`，而 `QGridLayout.sizeHint()`
    不问 height-for-width —— 值那一列是必须换行的 `BodyLabel`，窄面板下真实高度
    远超 sizeHint，内容会被裁掉。这里折叠直接 `view.setVisible()`，高度交给布局
    自己算。

    折叠能力给**每一段**，不只给 `collapsed=True` 的那几段：只有部分能点等于让
    用户去记哪几段能点。`collapsed` 只决定初始状态。整行（不只箭头）都可点 ——
    组头是这一段唯一的操作区，命中区域太小等于没有。

    `set_data` 每次重建网格而不是复用控件池：详情面板只在换选中行时更新，一次几十
    行的重建量级可以忽略，而混着跨列 span 的控件池极易对错格子。
    """

    def __init__(self, section: Section, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.section = section
        self._rows: list[Row] = []
        self._expanded = True

        self.header = QWidget(self)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.installEventFilter(self)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)
        self.title_label = StrongBodyLabel(section_title(section), self.header)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)

        self.copy_button = TransparentToolButton(FluentIcon.COPY, self.header)
        self.copy_button.setToolTip(
            QCoreApplication.translate("FlowFields", "复制本组")
        )
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        header_layout.addWidget(self.copy_button)

        self.toggle_button = TransparentToolButton(
            FluentIcon.CHEVRON_DOWN_MED, self.header
        )
        self.toggle_button.clicked.connect(self.toggle)
        header_layout.addWidget(self.toggle_button)

        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(16)
        self.grid.setVerticalSpacing(6)
        self.grid.setColumnMinimumWidth(0, _LABEL_COLUMN_WIDTH)
        self.grid.setColumnStretch(1, 1)

        self.view = QWidget(self)
        view_layout = QVBoxLayout(self.view)
        view_layout.setContentsMargins(0, 6, 0, 0)
        view_layout.addLayout(self.grid)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.view)

        self.set_expanded(not section.collapsed)

    # —— 折叠 ——

    def eventFilter(self, watched, event) -> bool:
        """组头整行可点：左键按下即折叠，箭头按钮只补一个视觉锚点。"""
        if (
            watched is self.header
            and event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.toggle()
            return True
        return super().eventFilter(watched, event)

    def is_expanded(self) -> bool:
        """折叠状态。**不读 `view.isVisible()`** —— 那个在整棵树 `show()` 之前
        对每一段都是 `False`（Qt 的 `isVisible` 连祖先一起算），会把「段是收起的」
        和「面板还没显示」混成同一个答案。状态自己存一份，和显示时机无关。
        """
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        """展开/收起正文。"""
        self._expanded = expanded
        self.view.setVisible(expanded)
        self.toggle_button.setIcon(
            FluentIcon.CHEVRON_DOWN_MED if expanded else FluentIcon.CHEVRON_RIGHT_MED
        )

    def toggle(self) -> None:
        self.set_expanded(not self.is_expanded())

    # —— 数据 ——

    def rows(self) -> list[Row]:
        """当前渲染出来的行。测试按这个断言，不用去数控件。"""
        return list(self._rows)

    def set_data(self, data: dict) -> None:
        """按数据重建网格；整组没东西可显示就整段隐藏。"""
        self._rows = section_rows(self.section, data)
        self.__clear()
        for index, row in enumerate(self._rows):
            if row.heading:
                self.grid.addWidget(self.__heading(row), index, 0, 1, 2)
                continue
            self.grid.addWidget(self.__label(row), index, 0, Qt.AlignmentFlag.AlignTop)
            self.grid.addWidget(self.__value(row), index, 1)
        self.setVisible(bool(self._rows))

    def copy_to_clipboard(self) -> None:
        """整组按 ``标签: 值`` 逐行复制。小标题行只出标题。"""
        lines = [
            row.label if row.heading else f"{row.label}: {row.value}"
            for row in self._rows
        ]
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(lines))

    # —— 行控件 ——

    def __clear(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def __heading(self, row: Row) -> CaptionLabel:
        label = CaptionLabel(row.label, self.view)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def __label(self, row: Row) -> CaptionLabel:
        label = CaptionLabel(row.label, self.view)
        label.setTextColor(
            QColor(0, 0, 0, _LABEL_ALPHA), QColor(255, 255, 255, _LABEL_ALPHA)
        )
        if row.tip:
            label.setToolTip(row.tip)
        return label

    def __value(self, row: Row) -> BodyLabel:
        label = BodyLabel(row.value, self.view)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if row.mono:
            label.setFont(FontManager.code_font(9))
        if row.tip:
            label.setToolTip(row.tip)
        return label


class OverviewPane(SingleDirectionScrollArea):
    """概览页：`SECTIONS` 一个顶层分组一段，单列纵向滚动。

    单列（而不是自适应多列）是刻意的：详情面板本来就窄，两列摆下来每列都不够值
    换行，反而更难读。窄的时候读的是「这条流量怎么了」，一列从上往下扫最快。

    无卡片底框：段与段只靠 8px 间距分界（平铺风格见 `FieldCard`）。
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        sections: tuple[Section, ...] = SECTIONS,
    ) -> None:
        super().__init__(parent, orient=Qt.Orientation.Vertical)
        self.setWidgetResizable(True)
        # 关掉横向滚动条，值那侧的 word wrap 才有意义 —— 否则长 URL 会把整页撑宽。
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.container = QWidget(self)
        layout = QVBoxLayout(self.container)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)
        self.cards = [FieldCard(section, self.container) for section in sections]
        for card in self.cards:
            layout.addWidget(card)
        layout.addStretch(1)

        self.setWidget(self.container)
        self.enableTransparentBackground()

    def set_data(self, data: dict) -> None:
        for card in self.cards:
            card.set_data(data)

    def visible_cards(self) -> list[FieldCard]:
        """当前有内容的段。测试按这个断言「空组整段不显示」。"""
        return [card for card in self.cards if card.rows()]
