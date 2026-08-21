"""单一语法高亮器。

重构前这里有四个类：``UniversalHighlighter``（61 行，全仓库零实例化）、
``HTTPHighlighter``、``HeadersHighlighter``、``JSONHighlighter``。后三者本该通过
覆写 ``_generate_tokens`` 换词法器来区分，但 ``_full_relex`` 从头到尾直接调
``tokenize_http``——**那个钩子零调用点**。后果是 ``HeadersHighlighter`` 整个类是
no-op（它唯一的 override 永不执行，headers 面板一直在走 HTTP 词法器），而
``JSONHighlighter`` 只能靠复制粘贴 26 行 ``_full_relex`` 才能工作。

所以这里不再留"子类覆写钩子"这种结构：语言直接映射到一个词法器 callable
（``syntax.TOKENIZERS``），换语言就是换函数，没有可以忘记调用的钩子。

两种工作模式：

* **全文模式**（默认）：一次分词全文，按行缓存 ``(长度, 格式)``，``highlightBlock``
  只做查表。HTTP 报文的 header→body 上下文（Content-Type 决定 body 怎么高亮）跨行
  传递，``QSyntaxHighlighter`` 自带的 per-block 状态机扛不住，必须全文。
* **逐行模式**（文档 > ``LEX_LIMIT``）：放弃跨行上下文，``highlightBlock`` 现场分词
  当前行。HTTP 的 header/body 分界改用 ``QSyntaxHighlighter`` 的 block state 传递。
  静默降级——大 body 宁可少一点上下文，也不能卡住 UI。

写入路径同样做了防抖：原实现 ``contentsChanged`` 直连全量重分词，每敲一个键就
``toPlainText()`` 物化全文 + 重分词 + ``rehighlight()`` 再扫一遍全文。只读时还能忍，
可编辑后就是硬阻塞。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QTextDocument

from .syntax import (
    RE_BODY_SEPARATOR,
    Binary,
    Language,
    Text,
    TokenType,
    tokenize_headers,
    tokenize_html,
    tokenize_http_line,
    tokenize_json,
    tokenize_text,
    tokenizer_for,
)
from .theme import EditorPalette

#: 超过这个字符数就降级为逐行分词。512 KB 是"全文分词还能在一帧内跑完"的经验上限；
#: 抓包场景里超过它的基本都是 minified JS / base64 / 大 JSON，逐行已经够用。
LEX_LIMIT = 512 * 1024

#: ``contentsChanged`` 合并窗口（毫秒）。连续输入只在停手后重分词一次。
RELEX_DEBOUNCE_MS = 50

# 逐行模式下 HTTP 的 block state：Qt 用它在 block 之间传递"我在报文的哪一段"。
# -1 是 Qt 的默认值（未设置），所以从 0 开始编号。
_STATE_HEADER = 0
_STATE_BODY = 1

# Content-Type 中出现即判定 body 不可读的片段。
_BINARY_HINTS = (
    "octet-stream",
    "image/",
    "audio/",
    "video/",
    "application/pdf",
    "application/zip",
    "application/x-binary",
    "gzip",
    "protobuf",
)


class _BinaryLang:
    """伪语言标记：Content-Type 判定为 body 不可读。

    不进 ``TOKENIZERS``——它不分词，整段 body 就是一个 ``Binary`` token。原实现用
    字符串 ``"__BINARY__"`` 当伪 token 混在 token 流里，逼得取格式时先判一次
    ``isinstance(str)``。这里用独立哨兵类型而非另一个 ``str``：与 ``Language``
    （``StrEnum``）比较时不会因为值恰好相等而误判，``is`` 判定也不依赖字符串驻留。
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<binary>"


#: 唯一哨兵实例，判定一律用 ``is BINARY``。
BINARY = _BinaryLang()


def _content_type_lang(header_text: str) -> Language | _BinaryLang | None:
    """从 header 文本里解析 Content-Type，归一化为 body 的高亮语言。

    返回 ``None`` 表示"没有可用信息"，由调用方退回首字符启发式；
    其余返回值（``Language.*`` / ``BINARY``）都是明确判定。
    """
    for line in header_text.lower().split("\n"):
        if not line.startswith("content-type:"):
            continue
        value = line.split(":", 1)[1]
        if "json" in value:
            return Language.JSON
        if "xml" in value or "html" in value:
            return Language.XML
        if any(hint in value for hint in _BINARY_HINTS):
            # 二进制不进词法器：整段一个 Binary token（灰斜体）。
            return BINARY
        # 其它已知文本类型（text/plain 等）不强判，交给首字符启发式
        return None
    return None


def _body_tokens(
    text: str, lang: Language | _BinaryLang | None
) -> list[tuple[TokenType, str]] | None:
    """给 body 文本分词。``None`` 表示"识别不出来，保持 Text 别动"。"""
    if lang is BINARY:
        # 二进制先判：内容可能全是不可见字节，``strip()`` 后为空也要保持灰斜体，
        # 不能被下面的空白短路成"原样 Text"。
        return [(Binary, text)]
    if not text.strip():
        return None
    if lang is Language.JSON:
        return tokenize_json(text)
    if lang is Language.XML:
        return tokenize_html(text)

    # 无 Content-Type：退回首字符启发式（历史行为，抓包里 body 常常没有类型头）
    head = text.lstrip()[:1]
    if head in ("{", "["):
        return tokenize_json(text)
    if head == "<":
        return tokenize_html(text)
    return None


class TokenHighlighter(QSyntaxHighlighter):
    """按 ``Language`` 分词并上色。语言可原地切换，无需重建实例。"""

    def __init__(
        self,
        document: QTextDocument,
        language: Language = Language.HTTP,
    ) -> None:
        super().__init__(document)
        self._language = language
        self._line_formats: list[list[tuple[int, QTextCharFormat]]] = []
        self._lazy = False
        self._relexing = False

        # 单次定时器合并连续输入。父对象是 self，随 highlighter 一起销毁。
        self._relex_timer = QTimer(self)
        self._relex_timer.setSingleShot(True)
        self._relex_timer.setInterval(RELEX_DEBOUNCE_MS)
        self._relex_timer.timeout.connect(self._relex)

        document.contentsChanged.connect(self._on_contents_changed)
        self._relex()

    # —— 对外 API ——

    @property
    def language(self) -> Language:
        return self._language

    def set_language(self, language: Language) -> None:
        """原地切换词法器。

        原实现是 ``deleteLater()`` 旧 highlighter 再 new 一个，还得手动
        ``disconnect`` 对方的私有 slot（``editor.py:239``，且它 suppress 的
        ``RuntimeError`` 根本不是缺属性时抛的 ``AttributeError``）。
        """
        if language == self._language:
            return
        self._language = language
        self._relex()

    def refresh_style(self) -> None:
        """主题切换后重新取色。

        ``EditorPalette`` 的格式缓存以主题为键，切换后拿到的必然是新 ``QTextCharFormat``；
        这里只需让缓存的行格式表跟着换一遍。原实现在 ``_full_relex`` 之后又调了一次
        ``rehighlight()``（``highlighter.py:305-306``），全文白扫两遍。
        """
        if self._lazy:
            self.rehighlight()
        else:
            self._relex()

    def relex_now(self) -> None:
        """立刻重分词，跳过防抖窗口。

        程序化整体换文本（``ToolPlainTextEdit.set_text``：点一条 flow 就换一次全文）
        走这条路——等 50ms 会让报文先以无色状态闪一帧。手动逐字输入仍走防抖。
        """
        self._relex_timer.stop()
        self._relex()

    # —— 分词 ——

    def _on_contents_changed(self) -> None:
        # ``_relex`` 内部的 ``rehighlight()`` 会再次触发 contentsChanged，
        # 不挡住就是一个每 50ms 重跑一次的永动机。
        if not self._relexing:
            self._relex_timer.start()

    def _relex(self) -> None:
        """重建行格式缓存并重绘。"""
        if self._relexing:
            return

        # 已经要重分词了，挂着的防抖定时器没有意义（否则 relex_now 之后
        # 50ms 又会白跑一次全文）。
        self._relex_timer.stop()

        document = self.document()
        if document is None:
            return

        # characterCount() 是 O(1)，先用它决定模式，避免为了量长度而物化全文。
        lazy = document.characterCount() > LEX_LIMIT
        mode_changed = lazy != self._lazy
        self._lazy = lazy

        self._relexing = True
        try:
            if lazy:
                # 逐行模式不需要缓存，highlightBlock 现场分词。
                self._line_formats = []
                # 只在模式切换那一次全量重绘：之后每次编辑 Qt 自己会
                # 对改动的 block 调 highlightBlock，全量 rehighlight 是 O(全文)。
                if mode_changed:
                    self.rehighlight()
                return
            self._line_formats = self._build_line_formats(document.toPlainText())
            self.rehighlight()
        finally:
            self._relexing = False

    def _build_line_formats(
        self, text: str
    ) -> list[list[tuple[int, QTextCharFormat]]]:
        """全文分词 → 按行的 ``(长度, 格式)`` 列表（与 block 序号一一对应）。"""
        tokens = self._tokenize(text)
        lines: list[list[tuple[int, QTextCharFormat]]] = [[]]
        for ttype, value in tokens:
            fmt = EditorPalette.token_format(ttype)
            parts = value.split("\n")
            for i, part in enumerate(parts):
                if part:
                    lines[-1].append((len(part), fmt))
                if i < len(parts) - 1:
                    lines.append([])
        return lines

    def _tokenize(self, text: str) -> list[tuple[TokenType, str]]:
        if self._language is Language.HTTP:
            return self._tokenize_message(text)
        return tokenizer_for(self._language)(text)

    def _tokenize_message(self, text: str) -> list[tuple[TokenType, str]]:
        """HTTP 报文：头部按行分词，body 依 Content-Type 交给子语言词法器。

        ``tokenize_http`` 把整个 body 标成 ``Token.Text``，body 的实际语言只有在读完
        header 之后才知道——这段上下文传递就是"必须全文分词"的原因。
        """
        # 分界用 RE_BODY_SEPARATOR 而不是 partition("\n\n")：Raw 面板的文本是
        # mitmproxy 的 \r\n 线格式，硬找 "\n\n" 永远切不开，整个 JSON body 会
        # 继续按 header 上色，Content-Type 分派也永远不触发。
        match = RE_BODY_SEPARATOR.search(text)
        head = text if match is None else text[: match.start()]

        out: list[tuple[TokenType, str]] = []
        for i, line in enumerate(head.split("\n")):
            if i:
                out.append((Text, "\n"))
            out.extend(tokenize_http_line(line))
        if match is None:
            return out

        out.append((Text, match.group()))
        body = text[match.end() :]
        lang = _content_type_lang(head)
        tokens = _body_tokens(body, lang)
        out.extend(tokens if tokens is not None else [(Text, body)])
        return out

    # —— 渲染 ——

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt 虚函数)
        if self._lazy:
            self._highlight_line(text)
            return

        index = self.currentBlock().blockNumber()
        if index >= len(self._line_formats):
            return
        position = 0
        for length, fmt in self._line_formats[index]:
            self.setFormat(position, length, fmt)
            position += length

    def _highlight_line(self, text: str) -> None:
        """逐行模式：现场分词当前行。"""
        tokens = self._line_tokens(text)
        position = 0
        for ttype, value in tokens:
            length = len(value)
            self.setFormat(position, length, EditorPalette.token_format(ttype))
            position += length

    def _line_tokens(self, text: str) -> list[tuple[TokenType, str]]:
        lang = self._language
        if lang is Language.JSON:
            return tokenize_json(text)
        if lang is Language.XML:
            return tokenize_html(text)
        if lang is Language.HEADERS:
            return tokenize_headers(text)
        if lang is not Language.HTTP:
            return tokenize_text(text)

        # HTTP：用 block state 在行之间传"我还在头部 / 已进 body"。
        # 这是逐行模式下唯一保得住的跨行上下文（body 的 Content-Type 传不过来，
        # 所以 body 统一按纯文本渲染）。
        in_body = self.previousBlockState() == _STATE_BODY
        if not in_body and text in ("", "\r"):
            in_body = True  # 空行 = 头部结束（CRLF 报文的分界行只剩一个 "\r"）
        self.setCurrentBlockState(_STATE_BODY if in_body else _STATE_HEADER)
        return tokenize_text(text) if in_body else tokenize_http_line(text)


__all__ = ["LEX_LIMIT", "RELEX_DEBOUNCE_MS", "TokenHighlighter"]
