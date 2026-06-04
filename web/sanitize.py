"""Markdown→HTML変換時のXSS対策サニタイズユーティリティ"""

import re

import markdown  # type: ignore[import-untyped]
import nh3

_URL_RE = re.compile(
    r'(?<!["\'>=/])(https?://[^\s<>\'")\]]+[^\s<>\'")\].,;:!?])', re.ASCII
)

# markdownフィルタで許可するHTMLタグ
ALLOWED_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "br",
    "hr",
    "strong",
    "em",
    "b",
    "i",
    "u",
    "s",
    "del",
    "code",
    "pre",
    "blockquote",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "a",
    "img",
    "div",
    "span",
    "dl",
    "dt",
    "dd",
    "sup",
    "sub",
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


def _linkify_urls(html: str) -> str:
    """HTML内のプレーンテキストURLをクリッカブルリンクに変換する。

    既に<a>タグ内にあるURLは変換しない。
    """

    def _replace(match: re.Match[str]) -> str:
        url = match.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'

    parts = re.split(
        r"(<a[^>]*>.*?</a>|<code[^>]*>.*?</code>|<pre[^>]*>.*?</pre>)",
        html,
        flags=re.DOTALL,
    )
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(_URL_RE.sub(_replace, part))
        else:
            result.append(part)
    return "".join(result)


def render_markdown(text: str) -> str:
    """マークダウンをHTMLに変換し、サニタイズして返す"""
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br"])
    html = _linkify_urls(html)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
    )
