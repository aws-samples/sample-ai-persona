"""Markdown→HTML変換時のXSS対策サニタイズユーティリティ"""

import markdown  # type: ignore[import-untyped]
import nh3

# markdownフィルタで許可するHTMLタグ
ALLOWED_TAGS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "br", "hr",
    "strong", "em", "b", "i", "u", "s", "del",
    "code", "pre", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "img",
    "div", "span",
    "dl", "dt", "dd", "sup", "sub",
}

# 許可する属性
ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"align"},
    "th": {"align"},
    "code": {"class"},
    "pre": {"class"},
    "span": {"class"},
    "div": {"class"},
}


def render_markdown(text: str) -> str:
    """マークダウンをHTMLに変換し、サニタイズして返す"""
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br"])
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
    )
