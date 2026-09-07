"""Rewrite-rule model and mitmproxy rewrite-option spec construction.

The rewriting itself is done by mitmproxy's native addons (`mapremote.py`,
`maplocal.py`, `modifyheaders.py`, `modifybody.py`); this module only builds and
validates the option strings those addons consume.

只用 QtCore 的 `QCoreApplication.translate`、不碰控件：校验失败的消息会被
`apps/rewrite` 原样显示出来，所以要过翻译目录（`from_dict` 那两条不译 —— 它们被
`rules_from_raw` 吞掉，从不上界面）。

六种重写类型落在四个原生选项上（见 :attr:`RewriteKind.option`）::

    重定向（远程）  map_remote   →  mapremote.MapRemote
    重定向（本地）  map_local    →  maplocal.MapLocal
    请求头/响应头   modify_headers  →  modifyheaders.ModifyHeaders
    请求体/响应体   modify_body     →  modifybody.ModifyBody

`ModifyHeaders` / `ModifyBody` 一个 addon 同时挂请求和响应两侧的钩子，**没有**
方向参数。方向靠 spec 自带的 flow-filter 区分：请求侧钩子触发时 ``flow.response``
还是 None，所以 ``~q``（无响应）只在请求期成立、``~s``（有响应）只在响应期成立。

再往下加一种类型：往 :class:`RewriteKind` 加成员、在 `_KIND_OPTIONS` 里给出它落到
哪个原生选项、必要时在 `_KIND_PHASES` 里给出阶段选择器，最后给 :meth:`RewriteRule.to_spec`
加一条分支。``rewrite_option_updates`` 与整条下发链路都不用改。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication

from ferret.core.mitm.bindings import (
    parse_filter,
    parse_map_local_spec,
    parse_map_remote_spec,
    parse_modify_spec,
)
from ferret.core.mitm.filters import quote_regex


class RewriteKind(StrEnum):
    """Which native rewrite addon a rule is pushed down to.

    成员值是 Ferret 自己的标识（会落盘），**不是**选项名 —— 六种类型只对应四个
    选项，用 :attr:`option` 取。``map_remote`` 的成员值刻意与选项名同形，
    这样早先落盘的 ``{"kind": "map_remote"}`` 还能原样读回来。
    """

    MAP_REMOTE = "map_remote"
    MAP_LOCAL = "map_local"
    MODIFY_REQUEST_HEADER = "modify_request_header"
    MODIFY_RESPONSE_HEADER = "modify_response_header"
    MODIFY_REQUEST_BODY = "modify_request_body"
    MODIFY_RESPONSE_BODY = "modify_response_body"

    @property
    def option(self) -> str:
        """The mitmproxy option name this kind is pushed into."""
        return _KIND_OPTIONS[self]


_KIND_OPTIONS: dict[RewriteKind, str] = {
    RewriteKind.MAP_REMOTE: "map_remote",
    RewriteKind.MAP_LOCAL: "map_local",
    RewriteKind.MODIFY_REQUEST_HEADER: "modify_headers",
    RewriteKind.MODIFY_RESPONSE_HEADER: "modify_headers",
    RewriteKind.MODIFY_REQUEST_BODY: "modify_body",
    RewriteKind.MODIFY_RESPONSE_BODY: "modify_body",
}

# 阶段选择器：`~q` = 还没有响应（请求期），`~s` = 已有响应（响应期）。
# map_remote / map_local 的 addon 只挂 request 钩子且自带 `if flow.response: return`，
# 不需要选择器 —— 它们也用不上 flow-filter 段（见 to_spec 的两段式 spec）。
_KIND_PHASES: dict[RewriteKind, str] = {
    RewriteKind.MODIFY_REQUEST_HEADER: "~q",
    RewriteKind.MODIFY_RESPONSE_HEADER: "~s",
    RewriteKind.MODIFY_REQUEST_BODY: "~q",
    RewriteKind.MODIFY_RESPONSE_BODY: "~s",
}

HEADER_KINDS = frozenset(
    {RewriteKind.MODIFY_REQUEST_HEADER, RewriteKind.MODIFY_RESPONSE_HEADER}
)
BODY_KINDS = frozenset(
    {RewriteKind.MODIFY_REQUEST_BODY, RewriteKind.MODIFY_RESPONSE_BODY}
)
MAP_KINDS = frozenset({RewriteKind.MAP_REMOTE, RewriteKind.MAP_LOCAL})

# 「整体替换」用的体正则。不能写 `.*`：`re.sub` 在非空匹配之后还会匹配一次末尾的
# 空串（实测 `re.sub(b".*", lambda _: b"R", b"body", flags=re.DOTALL)` → `b"RR"`），
# 替换内容会被插两遍。`\A.*\Z` 加 DOTALL 恰好整体命中一次。
WHOLE_BODY_PATTERN = r"\A.*\Z"

# 头值 / 体内容以 `@` 开头时，原生 `ModifySpec.read_replacement` 会把后面的部分
# 当文件路径读取（modifyheaders.py）。这是原生语义，界面上要如实告知；也因此
# **无法**下发一个真的以 `@` 开头的字面量。
FILE_REPLACEMENT_PREFIX = "@"


class RewriteLogic(StrEnum):
    """How a rule's match value is turned into the url-regex subject."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


def _dedup(names: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for name in names:
        seen[name] = None
    return tuple(seen)


# 下发时恒写全量列表：规则被删光也要把选项写回空列表，否则内核继续用上一批 spec。
# 六种 kind 只有四个选项，必须去重 —— 重复的键在 dict 里会互相覆盖，
# 后一个空列表会把前一个刚攒好的 spec 抹掉。
REWRITE_OPTIONS: tuple[str, ...] = _dedup(_KIND_OPTIONS.values())

# 原生 utils/spec.py::parse_spec 拿 option[0] 当分隔符，再 `rem.split(sep, 2)`，
# 2 段当「无过滤器」、3 段当「带过滤器」—— 段数不固定，坑比 block_list 更深：
# 分隔符若出现在 replacement 里，一条 2 段 spec 会被**静默**读成 3 段
# （实测 "/foo/http://new.com/x" → subject="http:"、replacement="/new.com/x"，
# 不抛任何异常）。所以按内容动态挑一个各段都没出现过的字符，拼完还要回读复核。
# 注意 maxsplit=2 意味着**三段式 spec 的最后一段可以随便含分隔符**，
# 所以三段式只需要前两段避开它。
_SEPARATOR_POOL = "|#@^!~,;=+&*%$:/?"


def _pick_separator(*parts: str) -> str:
    for candidate in _SEPARATOR_POOL:
        if all(candidate not in part for part in parts):
            return candidate
    raise ValueError(
        QCoreApplication.translate(
            "RewriteRule",
            "无法为该规则挑选分隔符，请简化匹配值或重写目标",
        )
    )


def escape_template(text: str) -> str:
    r"""Escape text so ``re.sub`` treats it literally.

    替换串里只有反斜杠是元字符（``\1`` / ``\g<name>`` 都以它开头），把它翻倍即可；
    对替换串用 `re.escape` 是错的 —— 那会把 ``/``、``.`` 之类原样输出的字符也加上
    反斜杠，直接写进 URL。
    """
    return text.replace("\\", "\\\\")


def escape_escaped_str(text: str) -> str:
    r"""Escape text so ``strutils.escaped_str_to_bytes`` yields it verbatim.

    `parse_modify_spec` 对 subject 和 replacement **都**跑一遍
    ``codecs.escape_decode``：用户写的 ``\n`` 会变成真换行、``\b`` 会变成 0x08，
    单独结尾的 ``\`` 直接抛 ValueError("Trailing \\ in string")。字面量语义要
    原样送达，就得先把反斜杠翻倍（和 `escape_template` 同一个写法、完全两回事的
    理由：那边防的是 ``re.sub`` 的反向引用）。
    """
    return text.replace("\\", "\\\\")


def _validate_template(subject: str, template: str) -> None:
    r"""Reject replacement templates that would explode on live traffic.

    原生 `parse_map_remote_spec` 只 `re.compile` 了 subject，**从不校验 replacement**；
    坏的反向引用要等 `MapRemote.request` 里那句 `re.sub` 才炸，而且异常直接窜出
    addon 钩子（实测 ``|foo|bar\1`` 会在请求期抛 re.error）。
    好在 `re.sub` 是**预先**解析替换串的：模式没命中也照样报错，所以拿任意字符串
    试跑一次就能提前拦住。两种异常都要接：坏转义是 `re.error`，
    未知分组名是 `IndexError`（实测 ``\g<n>`` → IndexError）。
    调用方保证 subject 已经单独编译过，所以这里冒出来的错只可能是 template 的。
    """
    try:
        re.sub(subject, template, "")
    except (re.error, IndexError) as exc:
        # 文案单独取：lupdate 的 Python 解析器不往 f-string 里看。
        raise ValueError(
            QCoreApplication.translate("RewriteRule", "无效的重写目标：{}").format(exc)
        ) from exc


@dataclass(frozen=True, slots=True)
class RewriteRule:
    """A single user-authored rewrite rule.

    四个字段的含义**在所有类型里保持一致**，这样表格的列头不用随类型变：

    - ``logic`` + ``value``：匹配哪些 URL（恒对 `flow.request.pretty_url` 生效）
    - ``target``：改什么 —— 头名（头类型）／体正则（体类型，留空 = 整体替换）
    - ``replacement``：改成什么 —— 新 URL／本地路径／头值／新内容
    """

    kind: RewriteKind = RewriteKind.MAP_REMOTE
    logic: RewriteLogic = RewriteLogic.CONTAINS
    value: str = ""
    target: str = ""
    replacement: str = ""
    enabled: bool = True

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
        """本条规则是否填够了下发所需的字段（不判合法性，那是 `to_spec` 的事）。

        没填完的规则整条跳过而不是抛错：`options.update` 是原子的，一条半成品会
        把整批规则连坐回滚。
        """
        if not self.value.strip():
            return False
        if self.kind in MAP_KINDS:
            return bool(self.replacement.strip())
        if self.kind in HEADER_KINDS:
            return bool(self.target.strip())
        # 体类型两栏都可以空：空正则 = 整体替换，空内容 = 清空 body。
        return True

    # —— spec 构造 ——

    def to_spec(self) -> str:
        """Build the option string for this rule's native addon.

        Raises:
            ValueError: 任何一栏不可用，或原生解析器不认这条 spec。
        """
        self._compile_subject()
        if self.kind == RewriteKind.MAP_REMOTE:
            return self._map_remote_spec()
        if self.kind == RewriteKind.MAP_LOCAL:
            return self._map_local_spec()
        if self.kind in HEADER_KINDS:
            return self._header_spec()
        if self.kind in BODY_KINDS:
            return self._body_spec()
        raise ValueError(
            QCoreApplication.translate("RewriteRule", "暂不支持的重写类型：{}").format(
                self.kind
            )
        )

    def _compile_subject(self) -> str:
        """Compile the url-regex on its own so a bad regex blames the right field.

        `_validate_template` / `parse_filter` 里也会编译它，但那两处报错会被冠上
        「无效的重写目标」/「无效的过滤器」—— 正则写一半（``bad(``）时用户改的是
        匹配值那一栏，错怪另一栏比不报错更难查。
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

    def _flow_filter(self) -> str:
        """The flow-filter segment: phase selector + url match.

        头/体两类 addon 的 subject 已经被头名／体正则占掉了，URL 匹配只能塞进
        flow-filter 段。正则一律加引号（见 `filters.quote_regex`）。
        """
        phase = _KIND_PHASES[self.kind]
        expr = f"{phase} ~u {quote_regex(self._compile_subject())}"
        try:
            parse_filter(expr)
        except ValueError as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule", "无法为该规则生成合法的过滤器：{}"
                ).format(exc)
            ) from exc
        return expr

    def _modify_replacement(self) -> str:
        """The replacement segment for modify_headers / modify_body.

        以 `@` 开头交给原生按文件路径读取（原样下发，由 `parse_modify_spec` 校验
        可读性）；否则按字面量处理，反斜杠先翻倍。
        """
        if self.replacement.startswith(FILE_REPLACEMENT_PREFIX):
            return self.replacement
        return escape_escaped_str(self.replacement)

    def _map_remote_spec(self) -> str:
        subject, template = self.subject, self.template
        if self.logic == RewriteLogic.EQUALS:
            # EQUALS 是整条 URL 替换，且替换串一定是字面量 —— 这是唯一能在下发前
            # 断定结果 URL 的模式，顺手把「没有 scheme/host」挡掉，
            # 否则同样是 request.url setter 在钩子里抛 ValueError。
            parsed = urlparse(self.replacement.strip())
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(
                    QCoreApplication.translate(
                        "RewriteRule",
                        "重写目标必须是带协议和主机名的完整 URL",
                    )
                )
        _validate_template(subject, template)

        separator = _pick_separator(subject, template)
        spec = f"{separator}{subject}{separator}{template}"
        # 最终裁判是原生 parse_map_remote_spec：分隔符、段数、subject 正则全过它。
        parsed_spec = parse_map_remote_spec(spec)
        # 但「过了」不等于「读对了」：段数不固定，多切一刀也不报错。回读复核，
        # 确认它读到的就是我们想给的两段。
        if (parsed_spec.subject, parsed_spec.replacement) != (subject, template):
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "无法为该规则生成合法的重写表达式，请简化匹配值或重写目标",
                )
            )
        return spec

    def _map_local_spec(self) -> str:
        subject = self.subject
        path = self.replacement.strip()
        if not path:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "本地文件或目录不能为空")
            )
        # 原生 parse_map_local_spec 用 `resolve(strict=True)`：路径必须**当下存在**，
        # 不存在整批规则会一起回滚。先自己解析一次，好把错怪到这一栏上。
        try:
            resolved = Path(path).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "本地路径不存在或不可访问：{}（{}）",
                ).format(path, exc)
            ) from exc
        # 和 map_remote 同为两段式，所以本地路径也得避开分隔符。
        separator = _pick_separator(subject, path)
        spec = f"{separator}{subject}{separator}{path}"
        parsed_spec = parse_map_local_spec(spec)
        if (parsed_spec.regex, parsed_spec.local_path) != (subject, resolved):
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "无法为该规则生成合法的重写表达式，请简化匹配值或本地路径",
                )
            )
        return spec

    def _header_spec(self) -> str:
        name = self.target.strip()
        if not name:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求头/响应头名称不能为空")
            )
        if "\n" in name or "\r" in name:
            raise ValueError(
                QCoreApplication.translate("RewriteRule", "请求头/响应头名称不能含换行")
            )
        flow_filter = self._flow_filter()
        subject = escape_escaped_str(name)
        replacement = self._modify_replacement()
        # 三段式：maxsplit=2 让最后一段可以随便含分隔符，只需前两段避开。
        separator = _pick_separator(flow_filter, subject)
        spec = f"{separator}{flow_filter}{separator}{subject}{separator}{replacement}"
        parsed_spec = self._parse_modify(spec, subject_is_regex=False)
        if (parsed_spec.subject, parsed_spec.replacement_str) != (
            name.encode(),
            replacement,
        ):
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "无法为该规则生成合法的重写表达式，请简化匹配值或头名称",
                )
            )
        return spec

    def _body_spec(self) -> str:
        # 体正则不 strip：正则里的空白是有意义的。整栏留空才当「整体替换」。
        subject = self.target if self.target.strip() else WHOLE_BODY_PATTERN
        flow_filter = self._flow_filter()
        replacement = self._modify_replacement()
        separator = _pick_separator(flow_filter, subject)
        spec = f"{separator}{flow_filter}{separator}{subject}{separator}{replacement}"
        parsed_spec = self._parse_modify(spec, subject_is_regex=True)
        if parsed_spec.replacement_str != replacement:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule",
                    "无法为该规则生成合法的重写表达式，请简化匹配值或体正则",
                )
            )
        return spec

    @staticmethod
    def _parse_modify(spec: str, *, subject_is_regex: bool) -> Any:
        """Run the native parser and translate its message into our own vocabulary.

        `parse_modify_spec` 会从三处抛 ValueError：段数不对、正则编译失败、
        `escaped_str_to_bytes` 遇到坏转义（例如结尾一个孤零零的 ``\\``）。
        """
        try:
            return parse_modify_spec(spec, subject_is_regex)
        except ValueError as exc:
            raise ValueError(
                QCoreApplication.translate(
                    "RewriteRule", "重写表达式不合法：{}"
                ).format(exc)
            ) from exc

    # —— 持久化 ——

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "logic": str(self.logic),
            "value": self.value,
            "target": self.target,
            "replacement": self.replacement,
            "enabled": self.enabled,
        }

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
        )


def _is_active(rule: RewriteRule) -> bool:
    return rule.enabled and rule.filled


def rewrite_option_updates(
    rules: Iterable[RewriteRule],
) -> dict[str, list[str]]:
    """Translate rules into the ``options.update`` kwargs for every rewrite option.

    每个受支持的选项都会出现在结果里（没有规则就是空列表），这样调用方一次
    `options.update(**updates)` 既能下发新规则、也能清掉被删掉的老规则。
    停用/没填完的规则整条跳过，不参与下发。
    """
    updates: dict[str, list[str]] = {name: [] for name in REWRITE_OPTIONS}
    for rule in rules:
        if not _is_active(rule):
            continue
        # 先算 spec：to_spec 会挡掉还没落地的 kind，所以下一行的取键必定命中，
        # 不会漏出一个上层 `except ValueError` 接不住的 KeyError。
        spec = rule.to_spec()
        updates[rule.kind.option].append(spec)
    return updates


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
