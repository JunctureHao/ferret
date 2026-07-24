import contextlib
import re
from collections.abc import Iterable

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat

from .syntax import (
    MaterialStyle,
    Text,
    Token,
    TokenType,
    tokenize_html,
    tokenize_http,
    tokenize_json,
)


class UniversalHighlighter(QSyntaxHighlighter):
    def __init__(self, document, lang="http"):
        super().__init__(document)
        self.format_cache = {}
        self.pygments_style = MaterialStyle
        self.refresh_style()

    def refresh_style(self):
        self.format_cache.clear()
        self.rehighlight()

    def _get_format(self, ttype):
        if ttype in self.format_cache:
            return self.format_cache[ttype]
        style_dict = self.pygments_style.style_for_token(ttype)
        qtf = QTextCharFormat()
        if style_dict["color"]:
            color = QColor(f"#{style_dict['color']}")
            if str(ttype).startswith("Token.Literal.String"):
                color = QColor("#107C10")
            qtf.setForeground(color)
        if style_dict["bold"]:
            qtf.setFontWeight(QFont.Weight.Bold)
        if style_dict["italic"]:
            qtf.setFontItalic(True)
        self.format_cache[ttype] = qtf
        return qtf

    _HEADER_RE = re.compile(r"^([^:]+):\s?(.*)$")

    def _tokenize_line(self, text: str) -> list:
        """按行启发式分派：JSON / HTML / 请求头 / 纯文本。"""
        s = text.strip()
        if not s:
            return [(Text, text)]
        if s[0] in "{[":
            return tokenize_json(text)
        if s[0] == "<":
            return tokenize_html(text)
        m = self._HEADER_RE.match(text)
        if ":" in text and m:
            return [
                (Token.Name.Attribute, m.group(1)),
                (Token.Operator, ": "),
                (Token.Literal, m.group(2)),
            ]
        return [(Text, text)]

    def highlightBlock(self, text):
        tokens = self._tokenize_line(text)
        current_index = 0
        for ttype, value in tokens:
            token_len = len(value)
            fmt = self._get_format(ttype)
            if fmt:
                self.setFormat(current_index, token_len, fmt)
            current_index += token_len


class HTTPHighlighter(QSyntaxHighlighter):
    """
    全量解析高亮器：
    复用 MaterialStyle 原始样式表，不进行人工颜色干预。
    通过一次性解析全文，保留 HTTP 到 Body 的上下文感应。
    """

    def __init__(self, document, lang="http"):
        super().__init__(document)
        self.pygments_style = MaterialStyle
        self.format_cache = {}
        self.binary_format = None  # 二进制内容的灰暗斜体格式
        self.line_data = []  # 存储缓存: [[(length, QTextCharFormat), ...], ...]
        self.fold_regions = []  # JSON body 折叠区域（全局行号）
        self._relexing = False  # 防止重入

        # 监听文档变化进行全量重解析
        self.document().contentsChanged.connect(self._on_contents_changed)
        self._full_relex()

    def _on_contents_changed(self):
        if not self._relexing:
            self._full_relex()

    @staticmethod
    def _parse_content_type(header_text: str):
        """
        从已收集的 header 文本中解析 Content-Type，归一化为 body 类型标识。
        返回 "json" / "xml" / "binary" / None。
        优先依据 Content-Type 响应头，缺失时才退回首字符启发式。
        """
        lower = header_text.lower()
        # 仅扫描 Content-Type 行（大小写不敏感）
        for line in lower.split("\n"):
            if not line.startswith("content-type:"):
                continue
            val = line.split(":", 1)[1]
            if "json" in val:
                return "json"
            if "xml" in val or "html" in val:
                return "xml"
            # 典型二进制 / 不可读类型
            if any(
                k in val
                for k in (
                    "octet-stream",
                    "image/",
                    "application/pdf",
                    "gzip",
                    "protobuf",
                    "application/zip",
                    "application/x-binary",
                )
            ):
                return "binary"
            # 其它已知文本类型（如 text/plain）不强制子语言，交给启发式
            return None
        return None

    def _auto_detect_body(self, text: str, content_type=None):
        """
        根据 content_type 检测 body 内容类型，返回高亮后的 token 列表。
        content_type 为 None 时退回首字符启发式（兼容无 Content-Type 场景）。
        返回特殊元组 ("__BINARY__", text) 表示二进制不可解析内容。
        """
        stripped = text.strip()
        if not stripped:
            return None

        # 1. 优先按 Content-Type 分流
        if content_type == "json":
            try:
                return tokenize_json(stripped)
            except (TypeError, ValueError):
                return None
        if content_type == "xml":
            try:
                return tokenize_html(stripped)
            except (TypeError, ValueError):
                return None
        if content_type == "binary":
            return [("__BINARY__", text)]

        # 2. 无 Content-Type 时退回首字符启发式
        # 尝试 JSON
        if stripped[0] in ("{", "["):
            with contextlib.suppress(TypeError, ValueError):
                return tokenize_json(stripped)

        # 尝试 XML / HTML
        if stripped[0] == "<":
            with contextlib.suppress(TypeError, ValueError):
                return tokenize_html(stripped)

        return None

    def _full_relex(self):
        """对全文进行词法分析，并按行拆分存储"""
        if self._relexing:
            return
        self._relexing = True
        text = self.document().toPlainText()
        if not text:
            self.line_data = []
            self._relexing = False
            return

        # 1. 获取全量 Token，并对 body 做自动内容检测
        raw_tokens = tokenize_http(text)
        enhanced_tokens = []
        in_body = False
        content_type = None
        header_text = []  # 累积 header 文本，用于解析 Content-Type
        body_start_line = None  # body 起始行（0-based），用于折叠区域偏移
        line_cursor = 0  # 当前全局行号
        for ttype, value in raw_tokens:
            # 空行标记 header/body 分界
            if (
                ttype is Text
                and value == "\n"
                and not in_body
                and enhanced_tokens
                and enhanced_tokens[-1][0] is Text
                and enhanced_tokens[-1][1] == "\n"
            ):
                in_body = True
                body_start_line = line_cursor + 1  # 空行之后即 body
                # 进入 body 前，依据已收集 header 判定 body 类型
                content_type = self._parse_content_type("".join(header_text))
            if not in_body:
                header_text.append(value)
            if in_body and ttype is Text and value.strip():
                detected = self._auto_detect_body(value, content_type)
                if detected:
                    enhanced_tokens.extend(detected)
                    if value.endswith("\n"):
                        enhanced_tokens.append((Text, "\n"))
                    continue
            enhanced_tokens.append((ttype, value))
            # 统计全局行号（token 内的换行）
            line_cursor += value.count("\n")

        new_line_data = [[]]
        line_idx = 0

        # 2. 将 Token 拆分到每一行
        for ttype, value in enhanced_tokens:
            parts = value.split("\n")
            for i, part in enumerate(parts):
                if part:
                    fmt = self._get_format(ttype)
                    new_line_data[line_idx].append((len(part), fmt))

                # 如果有换行符，开启下一行
                if i < len(parts) - 1:
                    new_line_data.append([])
                    line_idx += 1

        self.line_data = new_line_data

        # 3. 计算 JSON body 的折叠区域（带 body 起始行偏移）
        self.fold_regions = []
        if content_type == "json" and body_start_line is not None:
            raw_body = self.document().toPlainText().split("\n")[body_start_line:]
            raw_body_text = "\n".join(raw_body)
            try:
                from ferret.utils.http_parser import compute_folds

                self.fold_regions = [
                    {
                        "start": r["start"] + body_start_line,
                        "end": r["end"] + body_start_line,
                        "brace": r["brace"],
                    }
                    for r in compute_folds(raw_body_text)
                ]
            except (ImportError, TypeError, ValueError, KeyError):
                self.fold_regions = []

        self.rehighlight()
        self._relexing = False

    def _get_format(self, ttype):
        """严格提取 MaterialStyle 的样式属性"""
        if ttype in self.format_cache:
            return self.format_cache[ttype]

        # 二进制内容：灰暗斜体，不调子语言词法器
        if ttype == "__BINARY__":
            if self.binary_format is None:
                qtf = QTextCharFormat()
                qtf.setForeground(QColor("#808080"))
                qtf.setFontItalic(True)
                self.binary_format = qtf
            return self.binary_format

        # 直接从 MaterialStyle 字典中获取定义
        style_dict = self.pygments_style.style_for_token(ttype)
        qtf = QTextCharFormat()

        # 1. 设置前景色 (Foreground)
        # Token.Text 及其子类继承 widget 默认色（暗色白字/亮色黑字），不硬编码
        if style_dict["color"] and not str(ttype).startswith("Token.Text"):
            hex_color = style_dict["color"]
            # material 的部分颜色在浅色主题下偏浅，统一加深
            if hex_color.lower() == "c3e88d":
                hex_color = "388E3C"  # 绿色加深
            elif hex_color.lower() == "89ddff":
                hex_color = "00ACC1"  # 青色（: { }）加深
            qtf.setForeground(QColor(f"#{hex_color}"))

        # 2. 设置背景色 (Background)
        if style_dict["bgcolor"]:
            qtf.setBackground(QColor(f"#{style_dict['bgcolor']}"))

        # 3. 设置字体样式
        if style_dict["bold"]:
            qtf.setFontWeight(QFont.Weight.Bold)
        if style_dict["italic"]:
            qtf.setFontItalic(True)
        if style_dict["underline"]:
            qtf.setFontUnderline(True)

        self.format_cache[ttype] = qtf
        return qtf

    def highlightBlock(self, text):
        """渲染逻辑：直接按行索引从缓存中提取格式"""
        idx = self.currentBlock().blockNumber()
        if idx >= len(self.line_data):
            return

        pos = 0
        for length, fmt in self.line_data[idx]:
            self.setFormat(pos, length, fmt)
            pos += length

    def _generate_tokens(self, text: str) -> Iterable[tuple[TokenType, str]]:
        """生成初始 token 流，子类可覆写以改变词法分析方式。"""
        return tokenize_http(text)

    def refresh_style(self):
        """主题切换时清空缓存并重新解析"""
        self.format_cache.clear()
        self.binary_format = None
        self.pygments_style = MaterialStyle
        self._full_relex()
        self.rehighlight()


class HeadersHighlighter(HTTPHighlighter):
    """
    请求头 / 响应头专用高亮器。

    问题背景：HTTP lexer 期望完整 HTTP 报文
    （起始行 + 头部 + 空行 + body）。若只喂 ``Key: Value`` 行，
    缺少起始请求行，lexer 会整体 fallback 成 Token.Error（material 红色），
    导致整面板“全红”。

    本类改用正则把每行拆为 key / 分隔符 / value 三段，分别映射到
    既有 token，复用 material 配色，行号与复制内容保持原样。
    """

    _HEADER_RE = re.compile(r"^([^:]+):\s?(.*)$")

    def _generate_tokens(self, text: str) -> Iterable[tuple[TokenType, str]]:
        result: list[tuple[TokenType, str]] = []
        for line in text.split("\n"):
            m = self._HEADER_RE.match(line)
            if m:
                key, value = m.group(1), m.group(2)
                result.append((Token.Name.Attribute, key))
                result.append((Token.Operator, ": "))
                result.append((Token.Literal, value))
            else:
                result.append((Token.Text, line))
            result.append((Token.Text, "\n"))
        return result


class JSONHighlighter(HTTPHighlighter):
    """
    纯 JSON 高亮器，继承 HTTPHighlighter 的全量解析机制，
    仅切换词法器为 tokenize_json，并支持外部注入 fold_regions。
    用于 body 面板的独立 JSON 展示。
    """

    def __init__(self, document, lang="json"):
        super().__init__(document, lang)
        self.fold_regions = []  # 外部注入的折叠区域（供后续折叠 UI 使用）

    def _generate_tokens(self, text: str) -> Iterable[tuple[TokenType, str]]:
        return tokenize_json(text)

    def set_fold_regions(self, regions: list):
        """外部设置折叠区域并触发重解析"""
        self.fold_regions = regions or []
        self.refresh_style()


__all__ = [
    "HTTPHighlighter",
    "HeadersHighlighter",
    "JSONHighlighter",
    "UniversalHighlighter",
]
