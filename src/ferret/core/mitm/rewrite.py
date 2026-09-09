"""Rewrite-rule model, validation and the compiled snapshot the addon executes.

重写引擎是自研的（plans/rewrite-ui.md）：八个类型全部由
:class:`ferret.core.mitm.addons.FerretRewriteAddon` 执行，原生 MapRemote /
MapLocal / ModifyHeaders / ModifyBody 四件已退役。自研的动机不是复刻原生，而是
原生补不上的两个缺口：ModifyHeaders / ModifyBody 改不了状态码和 method/path
（「替换响应」「替换请求」无原生件），四件散在四个 addon / 四种 spec 语法 /
两个钩子区，跨 addon 的次序语义割裂。自研之后一个规则模型、一套 URL 匹配、
一份列表、行序＝执行序。

本模块只碰模型与编译：校验消息会被 `apps/rewrite` 原样显示，所以过
`QCoreApplication.translate`（`from_dict` 那几条不译 —— 它们被
`rules_from_config` 吞掉，从不上界面）。执行分支在 addons.py。

再往下加一种类型：往 :class:`RewriteKind` 加成员、给 :meth:`RewriteRule.validate`
补一条分支、在 :class:`RewriteRuleSet` 的编译里挂上类型专属的预编译件，最后
`FerretRewriteAddon` 加一个执行分支。下发链路（runtime / facade / controller）
一概不用改。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication


class RewriteKind(StrEnum):
    """Which kind of rewrite a rule performs.

    成员值是 Ferret 自己的标识（会落盘），与任何 mitmproxy 选项无关。
    ``map_remote`` 的成员值刻意与早先落盘的 ``{"kind": "map_remote"}`` 同形，
    老配置原样读得回来。
    """

    MAP_REMOTE = "map_remote"
    MAP_LOCAL = "map_local"
    MODIFY_REQUEST_HEADER = "modify_request_header"
    MODIFY_RESPONSE_HEADER = "modify_response_header"
    MODIFY_REQUEST_BODY = "modify_request_body"
    MODIFY_RESPONSE_BODY = "modify_response_body"
    REPLACE_REQUEST = "replace_request"
    REPLACE_RESPONSE = "replace_response"


HEADER_KINDS = frozenset(
    {RewriteKind.MODIFY_REQUEST_HEADER, RewriteKind.MODIFY_RESPONSE_HEADER}
)
BODY_KINDS = frozenset(
    {RewriteKind.MODIFY_REQUEST_BODY, RewriteKind.MODIFY_RESPONSE_BODY}
)
MAP_KINDS = frozenset({RewriteKind.MAP_REMOTE, RewriteKind.MAP_LOCAL})
REPLACE_KINDS = frozenset({RewriteKind.REPLACE_REQUEST, RewriteKind.REPLACE_RESPONSE})

# 「整体替换」用的体正则。不能写 `.*`：`re.sub` 在非空匹配之后还会匹配一次末尾的
# 空串（实测 `re.sub(b".*", lambda _: b"R", b"body", flags=re.DOTALL)` → `b"RR"`），
# 替换内容会被插两遍。`\A.*\Z` 加 DOTALL 恰好整体命中一次。
WHOLE_BODY_PATTERN = r"\A.*\Z"

# 头值 / 体内容以 `@` 开头时按文件路径**每请求现读**。比原生 modify 的
# 「spec 解析时校验可读性、请求时重读」少了定格校验这一步 —— 文件可以先建规则
# 后落盘，更利于 mock 迭代，是刻意差异（plans/rewrite-ui.md §5）。也因此
# **无法**下发一个真的以 `@` 开头的字面量。
FILE_REPLACEMENT_PREFIX = "@"

# 替换响应缺省状态码：界面上留空就是它（§5 契约「状态码（默认 200）」）。
REPLACE_RESPONSE_DEFAULT_STATUS = 200

# 文件映射 / 替换响应在 `request` 钩子就地作答后打上的标记。mitmproxy 的 http 层
# 对这种「预作答」流量仍会派发 `responseheaders`（proxy/layers/http/__init__.py
# 里那句 "we now need to emulate the responseheaders hook"），SSE tee 的检测点
# 恰好在那里 —— 不跳过的话，一条被替换成 text/event-stream 的静态响应会被它
# 当成真事件流包装起来（plans/rewrite-ui.md §13 风险一）。
REWRITE_ANSWERED_KEY = "ferret_rewrite_answered"


class RewriteLogic(StrEnum):
    """How a rule's match value is turned into the url-regex subject."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


def escape_template(text: str) -> str:
    r"""Escape text so ``re.sub`` treats it literally.

    替换串里只有反斜杠是元字符（``\1`` / ``\g<name>`` 都以它开头），把它翻倍即可；
    对替换串用 `re.escape` 是错的 —— 那会把 ``/``、``.`` 之类原样输出的字符也加上
    反斜杠，直接写进 URL。
    """
    return text.replace("\\", "\\\\")


def _validate_template(subject: str, template: str) -> None:
    r"""Reject replacement templates that would explode on live traffic.

    坏的反向引用（``\1`` 越界、``\g<nope>``）要等请求期那句 `re.sub` 才炸，而且
    异常直接窜出 addon 钩子。好在 `re.sub` 是**预先**解析替换串的：模式没命中也
    照样报错，拿任意字符串试跑一次就能提前拦住。两种异常都要接：坏转义是
    `re.error`，未知分组名是 `IndexError`。
    """
    try:
        re.sub(subject, template, "")
    except (re.error, IndexError) as exc:
        # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
        raise ValueError(
            QCoreApplication.translate("RewriteRule", "无效的重写目标：{}").format(exc)
        ) from exc


def read_replacement(replacement: str) -> bytes:
    """替换串 → 字节。**只在 mitm 线程上调用**（`@路径` 走文件系统）。

    `@路径` 每请求现读（见 :data:`FILE_REPLACEMENT_PREFIX`）；否则按字面量
    UTF-8 编码 —— 自研引擎不做原生那套 `\\n` 转义解码，多行编辑器给出来的就是
    真换行，字面量语义原样送达。

    Raises:
        OSError: `@路径` 指向的文件此刻读不了（调用方记日志跳过，不打断钩子）。
    """
    if replacement.startswith(FILE_REPLACEMENT_PREFIX):
        return (
            Path(replacement[len(FILE_REPLACEMENT_PREFIX) :]).expanduser().read_bytes()
        )
    return replacement.encode("utf-8")


def _checked_status(status_code: int) -> int:
    if not 100 <= status_code <= 599:
        raise ValueError(
            QCoreApplication.translate("RewriteRule", "无效的 HTTP 状态码：{}").format(
                status_code
            )
        )
    return status_code


@dataclass(frozen=True, slots=True)
class RewriteRule:
    """A single user-authored rewrite rule.

    字段含义在八个类型里保持一致，表格的列头不随类型变：

    - ``logic`` + ``value``：匹配哪些 URL（恒对 `flow.request.pretty_url` 生效）；
    - ``target``：改什么 —— 头名（头类型）／体正则（体类型，留空 = 整体替换）；
    - ``replacement``：改成什么 —— 新 URL／本地路径／头值／新内容／替换类的体
      （内联或 ``@路径`` 现读）；
    - 替换类专属（仅 :data:`REPLACE_KINDS` 使用，`to_dict` 按需写出）：
      ``method`` / ``path``（替换请求，均可选）、``status_code``（替换响应，
      ``None`` = 执行期取 200）、``headers``（两替换类的头表，逐项覆盖）。
    """

    kind: RewriteKind = RewriteKind.MAP_REMOTE
    logic: RewriteLogic = RewriteLogic.CONTAINS
    value: str = ""
    target: str = ""
    replacement: str = ""
    enabled: bool = True
    method: str = ""
    path: str = ""
    status_code: int | None = None
    headers: tuple[tuple[str, str], ...] = ()

    @property
    def subject(self) -> str:
        """The url-regex this rule matches against (`flow.request.pretty_url`)."""
        value = self.value.strip()
        if not value:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "匹配值不能为空")
            )
        if self.logic == RewriteLogic.REGEX:
            return value
        if self.logic == RewriteLogic.EQUALS:
            return f"^{re.escape(value)}$"
        return re.escape(value)

    @property
    def template(self) -> str:
        r"""The ``re.sub`` replacement template this rule rewrites the URL with."""
        replacement = self.replacement.strip()
        if not replacement:
            # 空替换串会让整条 URL 变空，`request.url` 的 setter 直接抛
            # ValueError("No hostname given") —— 而那是在 addon 钩子里、对着真实
            # 流量抛的。重定向本来就得有目标，索性在这里就拦掉。
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "重写目标不能为空")
            )
        if self.logic == RewriteLogic.REGEX:
            # 正则模式保留反向引用（\1 / \g<name>），原样交给 re.sub。
            return replacement
        return escape_template(replacement)

    @property
    def filled(self) -> bool:
        """本条规则是否填够了执行所需的字段（不判合法性，那是 `validate` 的事）。

        没填完的规则整条跳过而不是抛错：规则列表是整批下发的，一条半成品
        不该连坐整批。
        """
        if not self.value.strip():
            return False
        if self.kind in MAP_KINDS:
            return bool(self.replacement.strip())
        if self.kind in HEADER_KINDS:
            return bool(self.target.strip())
        if self.kind == RewriteKind.REPLACE_REQUEST:
            # method / path / 头表 / 体均可选，但至少得填一项才有可执行的语义。
            return bool(
                self.method.strip()
                or self.path.strip()
                or self.headers
                or self.replacement
            )
        if self.kind == RewriteKind.REPLACE_RESPONSE:
            return (
                self.status_code is not None
                or bool(self.headers)
                or bool(self.replacement)
            )
        # 体类型两栏都可以空：空正则 = 整体替换，空内容 = 清空 body。
        return True

    # —— 校验 ——

    def validate(self) -> None:
        """单条规则的合法性；界面上过不了它就不让保存。

        Raises:
            ValueError: 任何一栏不可用。报错文案按「哪一栏写坏了怪哪一栏」给。
        """
        subject = self._compile_subject()
        if self.kind == RewriteKind.MAP_REMOTE:
            self._validate_map_remote(subject)
        elif self.kind == RewriteKind.MAP_LOCAL:
            self._validate_map_local()
        elif self.kind in HEADER_KINDS:
            self._validate_header_name()
        elif self.kind in BODY_KINDS:
            self._validate_body_regex()
        elif self.kind == RewriteKind.REPLACE_REQUEST:
            self._validate_replace_request()
        elif self.kind == RewriteKind.REPLACE_RESPONSE:
            self._validate_replace_response()

    def _compile_subject(self) -> str:
        """Compile the url-regex on its own so a bad regex blames the right field.

        正则写一半（``bad(``）时用户改的是「匹配值」那一栏，错怪另一栏比不报错
        更难查。
        """
        subject = self.subject
        try:
            re.compile(subject)
        except re.error as exc:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "无效的匹配值：{}").format(
                    exc
                )
            ) from exc
        return subject

    def _validate_map_remote(self, subject: str) -> None:
        template = self.template
        if self.logic == RewriteLogic.EQUALS:
            # EQUALS 是整条 URL 替换，且替换串一定是字面量 —— 这是唯一能在下发前
            # 断定结果 URL 的模式，顺手把「没有 scheme/host」挡掉，
            # 否则是 request.url setter 在钩子里抛 ValueError。
            parsed = urlparse(self.replacement.strip())
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(
                    QCoreApplication.translate(
                        "RewriteRule",
                        "重写目标必须是带协议和主机名的完整 URL",
                    )
                )
        _validate_template(subject, template)

    def _validate_map_local(self) -> None:
        # 保存时要求路径当下存在（和原生 spec 解析同一道闸，挡住绝大多数手滑）；
        # 执行期仍然每请求现读，建了规则再挪走/换内容都即时生效（§5 契约）。
        path = self.replacement.strip()
        if not path:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "本地文件或目录不能为空")
            )
        try:
            Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "本地路径不存在或不可访问：{}（{}）",
                ).format(path, exc)
            ) from exc

    def _validate_header_name(self) -> None:
        name = self.target.strip()
        if not name:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求头/响应头名称不能为空")
            )
        if "\n" in name or "\r" in name:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求头/响应头名称不能含换行")
            )

    def _validate_body_regex(self) -> None:
        # 体正则不 strip：正则里的空白是有意义的。整栏留空才当「整体替换」。
        pattern = self.target if self.target.strip() else WHOLE_BODY_PATTERN
        try:
            re.compile(pattern, re.DOTALL)
        except re.error as exc:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "无效的体正则：{}").format(
                    exc
                )
            ) from exc

    def _validate_replace_request(self) -> None:
        if not self.filled:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule", "替换请求至少要填写方法、路径、请求头或请求体之一"
                )
            )
        # 方法得是个 token：带空格的「GET /x」会顺着 `request.method` 写进报文行。
        # （mitmproxy 的 method setter 会顺手大写化，小写 "post" 不用管。）
        if self.method.strip() and re.search(r"[\s]", self.method):
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求方法不能含空白字符")
            )

    def _validate_replace_response(self) -> None:
        if not self.filled:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule", "替换响应至少要填写状态码、响应头或响应体之一"
                )
            )
        if self.status_code is not None:
            _checked_status(self.status_code)

    # —— 持久化 ——

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": str(self.kind),
            "logic": str(self.logic),
            "value": self.value,
            "target": self.target,
            "replacement": self.replacement,
            "enabled": self.enabled,
        }
        # 替换类专属字段按需写出：六类老规则的落盘形状一个字节都不变。
        if self.method:
            data["method"] = self.method
        if self.path:
            data["path"] = self.path
        if self.status_code is not None:
            data["status_code"] = self.status_code
        if self.headers:
            data["headers"] = [[name, value] for name, value in self.headers]
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> "RewriteRule":
        """Rebuild a rule from persisted data; raises on anything unusable."""
        if not isinstance(raw, dict):
            raise TypeError("规则必须是对象")
        try:
            kind = RewriteKind(str(raw.get("kind", RewriteKind.MAP_REMOTE)))
            logic = RewriteLogic(str(raw.get("logic", RewriteLogic.CONTAINS)))
        except ValueError as exc:
            raise ValueError(f"未知的规则字段：{exc}") from exc
        return cls(
            kind=kind,
            logic=logic,
            value=str(raw.get("value", "")),
            target=str(raw.get("target", "")),
            replacement=str(raw.get("replacement", "")),
            enabled=bool(raw.get("enabled", True)),
            method=str(raw.get("method", "")),
            path=str(raw.get("path", "")),
            status_code=_status_from_raw(raw.get("status_code")),
            headers=_headers_from_raw(raw.get("headers")),
        )


def _status_from_raw(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("无效的 HTTP 状态码") from exc


def _headers_from_raw(raw: Any) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    # from_dict 的校验消息从不上界面，这里随原生约定用 TypeError 表达类型错。
    if not isinstance(raw, (list, tuple)):
        raise TypeError("头表必须是键值对列表")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("头表必须是键值对列表")
        pairs.append((str(item[0]), str(item[1])))
    return tuple(pairs)


@dataclass(frozen=True, slots=True)
class CompiledRewrite:
    """One active rule plus its pre-compiled matchers.

    编译一次、每请求只读 —— 不在钩子里 `re.compile`（对齐 `GatewayRuleSet`）。
    """

    rule: RewriteRule
    #: URL 匹配（``re.search`` 于 `flow.request.pretty_url`）。
    url: re.Pattern[str]
    #: 体类型的体正则（DOTALL；「整体替换」在构造期就已换成 WHOLE_BODY_PATTERN）。
    body: re.Pattern[str] | None = None
    #: 头类型的头名（strip 后；执行期 pop/add 都用它，免得每请求 strip）。
    header_name: str = ""

    def matches(self, url: str) -> bool:
        return bool(self.url.search(url))


def _compile_body(rule: RewriteRule) -> re.Pattern[str] | None:
    if rule.kind not in BODY_KINDS:
        return None
    pattern = rule.target if rule.target.strip() else WHOLE_BODY_PATTERN
    return re.compile(pattern, re.DOTALL)


class RewriteRuleSet:
    """A pre-compiled, immutable snapshot of the rewrite rules.

    只在下发时构造一次，之后每条 flow 只读它；坏规则在构造期就抛，绝不留到
    运行期（对齐 `GatewayRuleSet` 的契约）。停用 / 没填完的规则整条跳过。

    Raises:
        ValueError: 任何一条启用且填完的规则不合法。
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: Iterable[RewriteRule] = ()) -> None:
        compiled: list[CompiledRewrite] = []
        for rule in rules:
            if not rule.enabled or not rule.filled:
                continue
            rule.validate()
            compiled.append(
                CompiledRewrite(
                    rule=rule,
                    url=re.compile(rule.subject),
                    body=_compile_body(rule),
                    header_name=rule.target.strip()
                    if rule.kind in HEADER_KINDS
                    else "",
                )
            )
        self._rules: tuple[CompiledRewrite, ...] = tuple(compiled)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def __len__(self) -> int:
        return len(self._rules)

    def entries(self) -> tuple[CompiledRewrite, ...]:
        """The compiled rules in execution order（行序＝执行序）."""
        return self._rules


def rewrite_rules_from_config(raw: Any) -> list[RewriteRule]:
    """Read rules back from persisted config, dropping entries we cannot parse."""
    if not isinstance(raw, list):
        return []
    rules: list[RewriteRule] = []
    for item in raw:
        try:
            rules.append(RewriteRule.from_dict(item))
        except (TypeError, ValueError):
            continue
    return rules


def rewrite_rules_to_config(rules: Iterable[RewriteRule]) -> list[dict[str, Any]]:
    """Serialize rules for persistence."""
    return [rule.to_dict() for rule in rules]
