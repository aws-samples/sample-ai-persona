"""Make a large made-up interview record for the documents manual E2E (NFR1.15).

The text is built from a fixed vocabulary (made-up speakers and answers; no
real data). Each part starts with a heading, and every line names a speaker
with attributes (age band, gender, job) so the evidence can be matched with
table segments (BR28.1). Lines carry the labels 価値 / 不満 / 目標 / 行動.

Formats: ``md`` (``#`` headings), ``txt`` (the same text without the marks
read as headings by Markdown) and ``docx`` (Word headings ``Heading1``). The
docx is written as a ZIP of the few XML parts Word needs, with the standard
library only (python-docx is not a dependency of the app). Everything is
written a little at a time, so memory stays small however large the file is.

Example (about 800,000 characters, 55 chunks of 15,000)::

    uv run python scripts/make_large_sample_document.py --chars 800000 \\
        --format docx --headings 40 --out /tmp/large_interview.docx
"""

from __future__ import annotations

import argparse
import random
import sys
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterator, Optional, Sequence
from xml.sax.saxutils import escape

_FORMATS = ("md", "txt", "docx")
_AGES = ("20代", "30代", "40代", "50代", "60代")
_GENDERS = ("男性", "女性")
_JOBS = ("会社員", "公務員", "自営業", "学生", "主婦", "パート")
_THEMES = ("買い物", "通院", "子育て", "仕事", "趣味", "住まい", "移動", "食事")
_SENTENCES = {
    "価値": (
        "家族と過ごす時間を何よりも大切にしていると話した",
        "多少高くても長く使えるものを選ぶようにしている",
        "地元の店を応援したいという気持ちが強い",
    ),
    "不満": (
        "平日の夜は受付が混んでいて待ち時間が長すぎると感じる",
        "手続きの説明が分かりにくく何度も問い合わせることになる",
        "近くに使える店が少なく遠くまで出かける必要がある",
    ),
    "目標": (
        "来年までに週末の家事を半分に減らしたいと考えている",
        "仕事と家庭の時間の配分を見直したいと思っている",
        "健康のために毎日少しずつ歩く習慣をつけたい",
    ),
    "行動": (
        "仕事帰りにスマートフォンで翌日の予定を確認している",
        "週末にまとめて買い物をして平日は寄り道をしない",
        "気になることは口コミを読んでから決めている",
    ),
}
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.'
    'org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{_WORD_NS}">'
    '<w:style w:type="paragraph" w:styleId="Heading1">'
    '<w:name w:val="heading 1"/></w:style></w:styles>'
)


@dataclass(frozen=True)
class Written:
    """What was written: characters of the text and bytes of the file."""

    chars: int
    bytes: int


def _speaker(rng: random.Random, index: int) -> str:
    letter = chr(ord("A") + index % 26)
    return (
        f"回答者{letter}（{rng.choice(_AGES)}・{rng.choice(_GENDERS)}・"
        f"{rng.choice(_JOBS)}）"
    )


#: Characters each format adds around a heading and a line (as counted).
_OVERHEAD = {"md": (5, 1), "txt": (5, 1), "docx": (4, 2)}


def _blocks(
    rng: random.Random, chars: int, headings: int, fmt: str
) -> Iterator[tuple[bool, str]]:
    """``(is_heading, text)`` until about ``chars`` characters are given."""
    heading_extra, line_extra = _OVERHEAD[fmt]
    written = 0
    line = 0
    for part in range(headings):
        title = f"第{part + 1}章 {_THEMES[part % len(_THEMES)]}について"
        yield True, title
        written += len(title) + heading_extra
        target = chars * (part + 1) // headings
        while written < target:
            label = rng.choice(tuple(_SENTENCES))
            text = (
                f"{_speaker(rng, line)}: {label}: "
                f"{rng.choice(_SENTENCES[label])}（{line + 1}）。"
            )
            line += 1
            yield False, text
            written += len(text) + line_extra


def _write_text(path: str, blocks: Iterator[tuple[bool, str]], markdown: bool) -> int:
    chars = 0
    with open(path, "w", encoding="utf-8", newline="\n") as out:
        for is_heading, text in blocks:
            piece = (
                ("\n# " if markdown else "\n■ ") + text + "\n\n"
                if is_heading
                else text + "\n"
            )
            out.write(piece)
            chars += len(piece)
    return chars


def _write_docx(path: str, blocks: Iterator[tuple[bool, str]]) -> int:
    chars = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _RELS)
        archive.writestr("word/styles.xml", _STYLES)
        with archive.open("word/document.xml", "w") as raw:

            def put(text: str) -> None:
                raw.write(text.encode("utf-8"))

            put(
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{_WORD_NS}"><w:body>'
            )
            for is_heading, text in blocks:
                style = (
                    '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if is_heading else ""
                )
                put(f"<w:p>{style}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>")
                # as the preparation joins it: "# " before a heading, a blank
                # line between paragraphs
                chars += len(text) + _OVERHEAD["docx"][0 if is_heading else 1]
            put("</w:body></w:document>")
    return chars


def write_document(
    path: str, *, chars: int, fmt: str, headings: int, seed: int = 42
) -> Written:
    """Write a made-up interview record of about ``chars`` characters.

    Args:
        path: Where to write.
        chars: Characters to reach (the last line may go a little over).
        fmt: ``md``, ``txt`` or ``docx``.
        headings: Parts, each with one heading.
        seed: The same seed writes the same text.

    Raises:
        ValueError: A count is not positive or the format is unknown.
    """
    if chars < 1 or headings < 1:
        raise ValueError("chars and headings must be positive")
    if fmt not in _FORMATS:
        raise ValueError(f"format must be one of {_FORMATS}")
    blocks = _blocks(random.Random(seed), chars, headings, fmt)
    writers: dict[str, Callable[[], int]] = {
        "md": lambda: _write_text(path, blocks, markdown=True),
        "txt": lambda: _write_text(path, blocks, markdown=False),
        "docx": lambda: _write_docx(path, blocks),
    }
    written = writers[fmt]()
    with open(path, "rb") as f:
        size = f.seek(0, 2)
    return Written(chars=written, bytes=size)


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chars", type=_positive, required=True)
    parser.add_argument("--format", choices=_FORMATS, default="md")
    parser.add_argument("--headings", type=_positive, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    written = write_document(
        args.out,
        chars=args.chars,
        fmt=args.format,
        headings=args.headings,
        seed=args.seed,
    )
    print(f"wrote {written.chars:,} characters, {written.bytes:,} bytes to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
