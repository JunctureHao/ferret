"""详情面板的字段声明表：一份数据，两种渲染。

原来 `OverviewTree.set_data` 是近 300 行几乎逐字重复的 `QTreeWidgetItem` 构造 ——
每个分组重新造一遍粗体字号、`setTextAlignment` 成对抄、空值判断抄七遍。字段本身
（标签、取哪个键、怎么格式化、什么时候显示）其实是纯数据，抽出来之后渲染只剩一层
循环，加字段不用再碰渲染代码。

字段标签在这里**只做标记、不求值**：类体和模块级一样在导入期跑完，而
`core/application.py` 在顶层就 import 了 MainWindow —— 那时翻译器还没装，
求值出来的文案会永久冻结成英文。求值统一走 `field_label()` / `section_title()`。
值那一侧是 lambda，调用时才跑，所以可以直接写 `QCoreApplication.translate`。

没被 `QT_TRANSLATE_NOOP` 包住的标签（``Code`` / ``SNI`` / ``Common Name`` 之类）是
**故意**不译的专有名词，和搬过来之前保持一致：`translate()` 查不到就原样返回源文本，
所以它们照旧显示英文，也不会进 `zh_CN.ts`。

翻译 context 统一是 ``"FlowFields"``，而且**必须逐字写成字面量** ——
`tests/core/test_i18n.py` 的 `test_translate_always_names_its_context_literally` 只给
`utils/i18n.py` 开了口子，context 写成变量整条都提取不到。
"""

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication
from qfluentwidgets import FluentIcon

from ferret.core.mitm import human
from ferret.utils.i18n import QT_TRANSLATE_NOOP, resolve_marker

#: 视作“没有值”的取值结果，和搬过来之前的判断逐字一致。
EMPTY = (None, "", "N/A", "-")


@dataclass(frozen=True, slots=True)
class Field:
    """一行字段。

    Args:
        label: `QT_TRANSLATE_NOOP` 标记（或故意不译的专有名词），不在这里求值。
        source: 取值方式 —— 字符串表示 `data` 的键，可调用对象表示当场算。
        fmt: 拿到值之后的格式化，`None` 表示 `str()`。
        tip: 悬浮提示的标记，留给卡片式渲染用。
        mono: 等宽显示（id、指纹、序列号这类），留给卡片式渲染用。
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
    """一个分组。

    Args:
        title: 分组标题的标记。**空串表示不要分组节点**，字段直接挂在最外层。
        fields: 组内条目，按声明顺序渲染；嵌一个 `Section` 就是一个次级小节。
        icon: 卡片式渲染用的图标；树形渲染忽略它。
        when: 整组的显示条件，`None` 表示只要组内有可见字段就显示。
        collapsed: 默认折叠（冷门分组），留给卡片式渲染用。
        subgroup: 次级小节 —— 树形渲染标题用下划线、字段前缀 ``- ``（证书那两组的老
            样子）；卡片式渲染会把它并进上级卡片。
    """

    title: str
    fields: tuple["Field | Section", ...]
    icon: FluentIcon | None = None
    when: Callable[[dict], bool] | None = None
    collapsed: bool = False
    subgroup: bool = False


def section_title(section: Section) -> str:
    """分组标题求值；无标题的分组返回空串。"""
    if not section.title:
        return ""
    return QCoreApplication.translate("FlowFields", section.title)


def field_label(field: Field) -> str:
    """字段标签求值。查不到译文时 Qt 原样返回源文本，专有名词就靠这个保持英文。"""
    return QCoreApplication.translate("FlowFields", field.label)


def field_value(field: Field, data: dict) -> str | None:
    """算出这一行该显示的文本；返回 `None` 表示这一行整个跳过。

    `always` 的字段空值显示 ``-``（证书那两组历来如此），其余空值跳过。空列表也算空值
    —— 原来它会渲染成一行「0 项」，那既不是值也不是提示。
    """
    if isinstance(field.source, str):
        raw = data.get(field.source)
    else:
        raw = field.source(data)
    if raw in EMPTY or (isinstance(raw, (list, tuple)) and not raw):
        return "-" if field.always else None
    return field.fmt(raw) if field.fmt is not None else str(raw)


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


#: `state` 键 → 状态文案标记。求值在 `_state()` 里。
#: 只有这三条：`infer_state()` 只会返回 ``request`` / ``complete`` / ``error``。
#: 早先还挂着 ``request_headers`` / ``response_headers`` 两条，配的是产出侧同样
#: 永远进不去的两条分支 —— 两边一起清掉了。
_STATE_LABELS: dict[str, str] = {
    "request": QT_TRANSLATE_NOOP("FlowFields", "Request sent"),
    "complete": QT_TRANSLATE_NOOP("FlowFields", "Completed"),
    "error": QT_TRANSLATE_NOOP("FlowFields", "Error"),
}


def _state(data: dict) -> str:
    """`state` 键 → 面向人的状态文案；不认识的状态显示「未知」。"""
    return resolve_marker(
        _STATE_LABELS,
        data.get("state", ""),
        "FlowFields",
        QCoreApplication.translate("FlowFields", "Unknown"),
    )


#: 连接的「前端」「后端」两个小节共用的四项，只差 ``Front`` / ``Back`` 键前缀。
_CONN_PEER_FIELDS: tuple[tuple[str, str], ...] = (
    (QT_TRANSLATE_NOOP("FlowFields", "Client address"), "Client Address"),
    (QT_TRANSLATE_NOOP("FlowFields", "Client port"), "Client Port"),
    (QT_TRANSLATE_NOOP("FlowFields", "Server address"), "Server Address"),
    (QT_TRANSLATE_NOOP("FlowFields", "Server port"), "Server Port"),
)

#: 证书的「主体」「签发者」两个小节共用的六项，只差 ``Subject`` / ``Issuer`` 键前缀。
_CERT_NAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("Common Name", "Common Name"),
    (QT_TRANSLATE_NOOP("FlowFields", "Country"), "Country"),
    (QT_TRANSLATE_NOOP("FlowFields", "State or province"), "State"),
    (QT_TRANSLATE_NOOP("FlowFields", "Locality"), "Locality"),
    (QT_TRANSLATE_NOOP("FlowFields", "Organization"), "Organization"),
    (QT_TRANSLATE_NOOP("FlowFields", "Organizational unit"), "Organizational Unit"),
)

#: 整组是否露面的判断依据，逐字沿用搬过来之前的键集合。
_TLS_KEYS: tuple[str, ...] = (
    "TLS Version",
    "TLS SNI",
    "TLS ALPN Offers",
    "TLS ALPN Selected",
    "TLS Cipher List",
    "TLS Cipher",
)

_CERT_KEYS: tuple[str, ...] = (
    *(f"Subject {key}" for _label, key in _CERT_NAME_FIELDS),
    *(f"Issuer {key}" for _label, key in _CERT_NAME_FIELDS),
    "Not Before",
    "Not After",
    "Fingerprint SHA256",
    "Serial Number Hex",
)

_TIME_KEYS: tuple[str, ...] = (
    "req_time",
    "req_timestamp_end",
    "req_duration",
    "res_timestamp_start",
    "res_time",
    "res_duration",
    "Duration",
)

#: 大小组的四种口径，每侧三个键加一个合计：
#:
#: * ``*_headers_size`` —— 头部字节，走 `assemble_*_head()` 拿真实线格式；
#: * ``*_wire_size`` —— 报文体的**线上**字节（压缩后），和表格 Size 列同源；
#: * ``*_decoded_size`` —— 报文体**解压后**的字节，只在和线上不一样时才显示；
#: * ``*_total_size`` / ``total_size`` —— 头部 + 线上，也就是这条报文实际占的字节。
#:
#: 改造前只有 ``req_size`` / ``res_size`` 一个含混的「大小」（量的是解压后），
#: 却和头部字节加在一起当合计，于是同一条 gzip 响应在表格和详情里能差好几倍。
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


def _peer_section(title: str, prefix: str) -> Section:
    """连接的「前端」「后端」小节 —— 四项同名字段，只差键前缀。"""
    return Section(
        title=title,
        fields=tuple(
            Field(label, f"{prefix} {key}") for label, key in _CONN_PEER_FIELDS
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
        subgroup=True,
        fields=tuple(
            Field(label, f"{prefix} {key}") for label, key in _CERT_NAME_FIELDS
        ),
    )


#: 概览的完整规格：声明顺序就是渲染顺序。
SECTIONS: tuple[Section, ...] = (
    Section(
        title="",
        fields=(
            Field(QT_TRANSLATE_NOOP("FlowFields", "State"), _state),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Method"), "Method"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Protocol"), "Protocol"),
            Field("Code", "Status Code"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Server address"), "Server Address"),
            Field("Keep Alive", "Keep Alive"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Flow"), "id", mono=True),
            Field("Content Type", "Response Content-Type"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Proxy protocol"), "Proxy Protocol"),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "Connection"),
        when=_any_value("Connection ID", "Connection Time"),
        fields=(
            Field("ID", "Connection ID", mono=True),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Time"), "Connection Time"),
            _peer_section(QT_TRANSLATE_NOOP("FlowFields", "Frontend"), "Front"),
            _peer_section(QT_TRANSLATE_NOOP("FlowFields", "Backend"), "Back"),
        ),
    ),
    Section(
        title="TLS",
        when=_any_value(*_TLS_KEYS),
        fields=(
            Field(QT_TRANSLATE_NOOP("FlowFields", "Version"), "TLS Version"),
            Field("SNI", "TLS SNI"),
            Field("ALPN", "TLS ALPN Offers", fmt=_join),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "ALPN selected"), "TLS ALPN Selected"
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Cipher list"),
                "TLS Cipher List",
                fmt=_join,
            ),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Cipher selected"), "TLS Cipher"),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "Server certificate"),
        when=_any_value(*_CERT_KEYS),
        fields=(
            _cert_name_section("Subject", "Subject"),
            _cert_name_section(QT_TRANSLATE_NOOP("FlowFields", "Issuer"), "Issuer"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Not before"), "Not Before"),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Not after"), "Not After"),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Fingerprint"),
                "Fingerprint SHA256",
                mono=True,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Serial number"),
                "Serial Number Hex",
                mono=True,
            ),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "Timing"),
        when=_any_present(*_TIME_KEYS),
        fields=(
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Request start"),
                "req_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Request end"),
                "req_timestamp_end",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Request duration"),
                "req_duration",
                fmt=_ms,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Response start"),
                "res_timestamp_start",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Response end"),
                "res_time",
                fmt=format_time,
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Response duration"),
                "res_duration",
                fmt=_ms,
            ),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Total duration"), "Duration"),
        ),
    ),
    Section(
        title=QT_TRANSLATE_NOOP("FlowFields", "Size"),
        when=_any_present(*_SIZE_KEYS),
        fields=(
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Request"),
                _size_of("req_total_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Request headers"),
                _size_of("req_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Request body on the wire"),
                _size_of("req_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Request body decoded"),
                _decoded_size("req_wire_size", "req_decoded_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "Response"),
                _size_of("res_total_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Response headers"),
                _size_of("res_headers_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Response body on the wire"),
                _size_of("res_wire_size"),
            ),
            Field(
                QT_TRANSLATE_NOOP("FlowFields", "- Response body decoded"),
                _decoded_size("res_wire_size", "res_decoded_size"),
            ),
            Field(QT_TRANSLATE_NOOP("FlowFields", "Total"), _size_of("total_size")),
        ),
    ),
)
