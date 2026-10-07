"""HTTP body 的 UI 派生数据。

解析、美化、视图选择全部交给 mitmproxy 官方能力，这里只负责把结果摊成
UI 消费的字典：

- body 美化 / 视图命中 / 高亮语言 → ``mitmproxy.contentviews.prettify_message``
  （JSON 视图的缩进由这里收紧为 2 空格，见 ``build_body``）
- Content-Encoding 解压           → ``Message.get_content(strict=False)``
- charset 解码                    → ``Message.get_text(strict=False)``

mitmproxy 侧没有的派生数据一并去掉，避免两侧行为分叉：JSON 括号折叠区域
（``compute_folds``）、自定义 mime 归类（``mime_of`` / ``_is_binary_mime``）、
二进制嗅探（``_is_binary_content``）、``format_bytes`` 自定义字节格式
（改用 ``mitmproxy.utils.human.pretty_size``）。
"""

import json

from ferret.core.mitm.bindings import contentviews

MAX_PRETTY_SIZE = 1024 * 1024  # 1MB 以上跳过美化


def build_body(flow, message, max_size: int = MAX_PRETTY_SIZE) -> dict:
    """产出 body 面板所需的派生数据。

    Args:
        flow: message 所属的 ``HTTPFlow``；contentviews 要靠它组装 Metadata。
        message: mitmproxy 的 ``http.Request`` / ``http.Response``。
        max_size: 超过该字节数则跳过美化。contentviews 会把整个 body 解析后
            重新序列化，几十 MB 的 body 足以卡死 Qt 主线程；mitmproxy 自己靠
            ``content_view_lines_cutoff`` 截行来防这一手，但那需要“显示全部”
            的开关配套，ferret 没有，所以退一步按字节数直接跳过。

    返回:
    {
        "raw": bytes,           # 解压后的原始字节（Content-Encoding 已还原）
        "text": str,            # 解码后文本，Raw 标签页兜底用
        "pretty": str | None,   # contentview 美化结果；空 body 或超限为 None
                                # （JSON 视图再收紧为 2 空格缩进）
        "view": str,            # 命中的 contentview 名（JSON / gRPC / Raw …）
        "syntax": str,          # contentview 声明的高亮语言，见 SyntaxHighlight
    }
    """
    raw = message.get_content(strict=False) or b""
    text = _safe_text(message)

    if not raw or len(raw) > max_size:
        return {"raw": raw, "text": text, "pretty": None, "view": "", "syntax": "none"}

    result = contentviews.prettify_message(message, flow)
    pretty = result.text
    if result.view_name == "JSON":
        # mitmproxy 的 JSON 视图固定 ``indent=4``，ferret 统一收紧为 2。视图
        # 命中即代表它已对同一份数据成功 ``json.loads`` 过，重新解析不会失败；
        # 兜一层只为极端畸形输出不炸穿 body 页。
        try:
            pretty = json.dumps(json.loads(pretty), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    return {
        "raw": raw,
        "text": text,
        "pretty": pretty,
        "view": result.view_name or "",
        "syntax": result.syntax_highlight,
    }


def _safe_text(message) -> str:
    """``Message.get_text(strict=False)``，并清掉孤立代理字符。

    ``get_text`` 解码失败时兜底用 ``surrogateescape``，产出的孤立代理字符
    无法编码为 UTF-8，交给 Qt 渲染有风险，这里统一折成替换符。
    """
    text = message.get_text(strict=False) or ""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "replace").decode("utf-8")
    return text
