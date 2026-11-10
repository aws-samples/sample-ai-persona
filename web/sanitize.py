"""Markdown→HTML変換時のXSS対策サニタイズユーティリティ

変換は markdown-it-py（CommonMark 準拠で、処理時間が入力長にほぼ比例する）で行い、
入力中の生の HTML も通したうえで、最後に nh3 で許可したタグ・属性だけを残す。
"""

import types
from typing import Sequence

import nh3
from markdown_it import MarkdownIt
from markdown_it.common.utils import isMdAsciiPunct, isPunctChar, isWhiteSpace
from markdown_it.renderer import RendererHTML
from markdown_it.rules_inline import emphasis
from markdown_it.rules_inline.state_inline import Scanned, StateInline
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict

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


# 日本語（かな・漢字・全角記号）の範囲
_CJK_RANGES = (
    (0x3000, 0x303F),  # CJK symbols and punctuation (「」、。)
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x31F0, 0x31FF),  # Katakana phonetic extensions
    (0x3400, 0x4DBF),  # CJK unified ideographs extension A
    (0x4E00, 0x9FFF),  # CJK unified ideographs
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0xFF00, 0xFFEF),  # Halfwidth and fullwidth forms
)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _scan_delims_cjk(self: StateInline, start: int, canSplitWord: bool) -> Scanned:
    """``StateInline.scanDelims`` that also lets ``*`` open/close next to Japanese.

    CommonMark does not treat ``**「重要」**です`` or ``**50%**でした`` as
    emphasis, because the markers sit between punctuation and a letter.
    Japanese has no spaces between words, so a marker next to a CJK character
    is also accepted as flanking (the rule Python-Markdown effectively used).
    """
    pos = start
    maximum = self.posMax
    marker = self.src[start]
    last_char = self.src[start - 1] if start > 0 else " "
    while pos < maximum and self.src[pos] == marker:
        pos += 1
    count = pos - start
    next_char = self.src[pos] if pos < maximum else " "

    last_punct = isMdAsciiPunct(ord(last_char)) or isPunctChar(last_char)
    next_punct = isMdAsciiPunct(ord(next_char)) or isPunctChar(next_char)
    last_space = isWhiteSpace(ord(last_char))
    next_space = isWhiteSpace(ord(next_char))

    left_flanking = not next_space and (
        not next_punct or last_space or last_punct or _is_cjk(last_char)
    )
    right_flanking = not last_space and (
        not last_punct or next_space or next_punct or _is_cjk(next_char)
    )
    can_open = left_flanking and (canSplitWord or not right_flanking or last_punct)
    can_close = right_flanking and (canSplitWord or not left_flanking or next_punct)
    return Scanned(can_open, can_close, count)


def _emphasis_cjk(state: StateInline, silent: bool) -> bool:
    """The emphasis rule, scanning delimiters with :func:`_scan_delims_cjk`."""
    state.scanDelims = types.MethodType(_scan_delims_cjk, state)  # type: ignore[method-assign]
    return emphasis.tokenize(state, silent)


def _build_renderer() -> MarkdownIt:
    """Build the shared Markdown renderer.

    ``breaks`` keeps the former ``nl2br`` behaviour, ``table`` the former
    ``tables`` extension (fenced code is part of CommonMark). Only explicit
    ``http(s)://`` URLs are auto-linked, as before.
    """
    md = MarkdownIt(
        "commonmark", {"breaks": True, "html": True, "linkify": True}
    ).enable(["table", "linkify"])
    assert md.linkify is not None  # linkify-it-py is a declared dependency
    md.linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})
    md.inline.ruler.at("emphasis", _emphasis_cjk)

    def link_open(
        self: RendererHTML,
        tokens: Sequence[Token],
        idx: int,
        options: OptionsDict,
        env: EnvType,
    ) -> str:
        token = tokens[idx]
        if token.markup == "linkify":
            # 自動リンクにした URL だけ新しいタブで開く（rel は nh3 が付ける）
            token.attrSet("target", "_blank")
        return self.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    return md


_md = _build_renderer()


def render_markdown(text: str) -> str:
    """Render Markdown to HTML and keep only the allowed tags and attributes."""
    html = _md.render(text)
    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
    )
