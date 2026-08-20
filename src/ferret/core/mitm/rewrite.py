"""Rewrite-rule model and mitmproxy rewrite-option spec construction.

The rewriting itself is mitmproxy's native addon (`mitmproxy/addons/mapremote.py`);
this module only builds and validates the option strings that addon consumes.
No Qt and no UI labels here.

目前只落地 ``map_remote``（远程重定向）。其余重写能力（``map_local`` /
``modify_headers`` / ``modify_body``）的接入点已经留好：往 :class:`RewriteKind`
加一个成员（成员值就是 mitmproxy 的选项名），再给 :meth:`RewriteRule.to_spec`
加一条分支即可，``rewrite_option_updates`` 与整条下发链路都不用改。
注意 ``map_local`` 在 `core/mitm/bindings.py` 里是**打包瘦身的桩**，接它之前
得先把桩和 `__main__.py` 的 nofollow 一起摘掉。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from ferret.core.mitm.bindings import parse_map_remote_spec


# 成员值即 mitmproxy 的选项名，rewrite_option_updates 直接拿它当 key。
class RewriteKind(StrEnum):
    """Which native rewrite addon a rule is pushed down to."""

    MAP_REMOTE = "map_remote"


class RewriteLogic(StrEnum):
    """How a rule's match value is turned into the url-regex subject."""

    CONTAINS = "contains"
    EQUALS = "equals"
    REGEX = "regex"


# 下发时恒写全量列表：规则被删光也要把选项写回空列表，否则内核继续用上一批 spec。
REWRITE_OPTIONS: tuple[str, ...] = tuple(str(kind) for kind in RewriteKind)

# 原生 utils/spec.py::parse_spec 拿 option[0] 当分隔符，再 `rem.split(sep, 2)`，
# 2 段当「无过滤器」、3 段当「带过滤器」—— 段数不固定，坑比 block_list 更深：
# 分隔符若出现在 replacement 里，一条 2 段 spec 会被**静默**读成 3 段
# （实测 "/foo/http://new.com/x" → subject="http:"、replacement="/new.com/x"，
# 不抛任何异常）。所以按内容动态挑一个三段都没出现过的字符，拼完还要回读复核。
_SEPARATOR_POOL = "|#@^!~,;=+&*%$:/?"


def _pick_separator(*parts: str) -> str:
    for candidate in _SEPARATOR_POOL:
        if all(candidate not in part for part in parts):
            return candidate
    raise ValueError("无法为该规则挑选分隔符，请简化匹配值或重写目标")


def escape_template(text: str) -> str:
    r"""Escape text so ``re.sub`` treats it literally.

    替换串里只有反斜杠是元字符（``\1`` / ``\g<name>`` 都以它开头），把它翻倍即可；
    对替换串用 `re.escape` 是错的 —— 那会把 ``/``、``.`` 之类原样输出的字符也加上
    反斜杠，直接写进 URL。
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
        raise ValueError(f"无效的重写目标：{exc}") from exc


@dataclass(frozen=True, slots=True)
class RewriteRule:
    """A single user-authored rewrite rule."""

    kind: RewriteKind = RewriteKind.MAP_REMOTE
    logic: RewriteLogic = RewriteLogic.CONTAINS
    value: str = ""
    replacement: str = ""
    enabled: bool = True

    @property
    def subject(self) -> str:
        """The url-regex this rule matches against (`flow.request.pretty_url`)."""
        value = self.value.strip()
        if not value:
            raise ValueError("匹配值不能为空")
        if self.logic == RewriteLogic.REGEX:
            return value
        if self.logic == RewriteLogic.EQUALS:
            return f"^{re.escape(value)}$"
        return re.escape(value)

    @property
    def template(self) -> str:
        r"""The ``re.sub`` replacement template this rule rewrites with."""
        replacement = self.replacement.strip()
        if not replacement:
            # 空替换串会让整条 URL 变空，`request.url` 的 setter 直接抛
            # ValueError("No hostname given") —— 而那是在 addon 钩子里、对着真实
            # 流量抛的。重定向本来就得有目标，索性在这里就拦掉。
            raise ValueError("重写目标不能为空")
        if self.logic == RewriteLogic.REGEX:
            # 正则模式保留反向引用（\1 / \g<name>），原样交给 re.sub。
            return replacement
        return escape_template(replacement)

    def to_spec(self) -> str:
        """Build the option string for this rule's native addon.

        Raises:
            ValueError: 匹配值/重写目标不可用，或原生解析器不认这条 spec。
        """
        if self.kind != RewriteKind.MAP_REMOTE:
            raise ValueError(f"暂不支持的重写类型：{self.kind}")

        subject, template = self.subject, self.template
        # 先单独编译 subject。`_validate_template` 里那句 re.sub 也会编译它，但报错
        # 会被冠上「无效的重写目标」——正则写一半（`bad(`）时用户改的是匹配值那一栏，
        # 错怪另一栏比不报错更难查。分开报，两栏各背自己的锅。
        try:
            re.compile(subject)
        except re.error as exc:
            raise ValueError(f"无效的匹配值：{exc}") from exc
        if self.logic == RewriteLogic.EQUALS:
            # EQUALS 是整条 URL 替换，且替换串一定是字面量 —— 这是唯一能在下发前
            # 断定结果 URL 的模式，顺手把「没有 scheme/host」挡掉，
            # 否则同样是 request.url setter 在钩子里抛 ValueError。
            parsed = urlparse(self.replacement.strip())
            if not parsed.scheme or not parsed.netloc:
                raise ValueError("重写目标必须是带协议和主机名的完整 URL")
        _validate_template(subject, template)

        separator = _pick_separator(subject, template)
        spec = f"{separator}{subject}{separator}{template}"
        # 最终裁判是原生 parse_map_remote_spec：分隔符、段数、subject 正则全过它。
        parsed_spec = parse_map_remote_spec(spec)
        # 但「过了」不等于「读对了」：段数不固定，多切一刀也不报错。回读复核，
        # 确认它读到的就是我们想给的两段。
        if (parsed_spec.subject, parsed_spec.replacement) != (subject, template):
            raise ValueError("无法为该规则生成合法的重写表达式，请简化匹配值或重写目标")
        return spec

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": str(self.kind),
            "logic": str(self.logic),
            "value": self.value,
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
            replacement=str(raw.get("replacement", "")),
            enabled=bool(raw.get("enabled", True)),
        )


def _is_active(rule: RewriteRule) -> bool:
    return rule.enabled and bool(rule.value.strip())


def rewrite_option_updates(
    rules: Iterable[RewriteRule],
) -> dict[str, list[str]]:
    """Translate rules into the ``options.update`` kwargs for every rewrite option.

    每个受支持的选项都会出现在结果里（没有规则就是空列表），这样调用方一次
    `options.update(**updates)` 既能下发新规则、也能清掉被删掉的老规则。
    停用/空值的规则整条跳过，不参与下发。
    """
    updates: dict[str, list[str]] = {name: [] for name in REWRITE_OPTIONS}
    for rule in rules:
        if not _is_active(rule):
            continue
        # 先算 spec：to_spec 会挡掉还没落地的 kind，所以下一行的取键必定命中，
        # 不会漏出一个上层 `except ValueError` 接不住的 KeyError。
        spec = rule.to_spec()
        updates[str(rule.kind)].append(spec)
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
