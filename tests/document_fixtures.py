"""Small documents built inside the tests (U4, unit-test-instructions.md).

No library is added for them (NFR4-Q3=A): PDFs are written by hand as the
smallest valid PDF bytes, a docx is a ZIP of the few XML parts Word needs, and
an OLE head is its 8-byte signature.

- :func:`pdf_bytes` writes one page per text (an empty text is a blank page);
  ``encrypted=True`` adds a Standard security handler to the trailer.
- :func:`docx_bytes` writes paragraphs; a heading is ``(level, text)``.
- :func:`ole_bytes` is what an encrypted docx or an old ``.doc`` starts with.
"""

from __future__ import annotations

import io
import zipfile
from typing import Sequence, Union
from xml.sax.saxutils import escape

OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _pdf_string(text: str) -> bytes:
    data = text.encode("latin-1")
    return (
        b"("
        + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
        + b")"
    )


def _content(text: str) -> bytes:
    if not text:
        return b""
    lines = text.split("\n")
    parts = [b"BT /F1 12 Tf 14 TL 72 720 Td"]
    for line in lines:
        parts.append(_pdf_string(line) + b" Tj T*")
    parts.append(b"ET")
    return b"\n".join(parts)


def pdf_bytes(pages: Sequence[str], *, encrypted: bool = False) -> bytes:
    """A PDF with one page per item of ``pages`` (ASCII / Latin-1 text).

    Args:
        pages: The text of each page; ``""`` makes a page without text.
        encrypted: Put a ``/Encrypt`` dictionary in the trailer (the
            password does not open it; the readers only need to see it).
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled below
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids: list[int] = []
    for text in pages:
        stream = _content(text)
        content = add(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_obj, font, content)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_obj
    objects[pages_obj - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    encrypt = 0
    if encrypted:
        encrypt = add(
            b"<< /Filter /Standard /V 1 /R 2 /P -4 /O <"
            + b"11" * 32
            + b"> /U <"
            + b"22" * 32
            + b"> >>"
        )
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if encrypt:
        trailer += b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (
            encrypt,
            b"ab" * 16,
            b"ab" * 16,
        )
    out.write(b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


Block = Union[str, tuple[int, str]]

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
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_bytes(blocks: Sequence[Block], *, japanese_style_ids: bool = False) -> bytes:
    """A docx of paragraphs; ``(level, text)`` is a heading of that level.

    Args:
        blocks: Paragraph texts and ``(level, text)`` headings (level 1〜3).
        japanese_style_ids: Use the style IDs a Japanese Word writes
            (``"1"``, ``"2"`` ...) with the names ``heading 1`` ... .
    """
    body: list[str] = []
    for block in blocks:
        if isinstance(block, tuple):
            level, text = block
            style = str(level) if japanese_style_ids else f"Heading{level}"
            body.append(
                f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
                f"<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"
            )
        else:
            body.append(f"<w:p><w:r><w:t>{escape(block)}</w:t></w:r></w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W}"><w:body>{"".join(body)}</w:body></w:document>'
    )
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="'
        f'{level if japanese_style_ids else f"Heading{level}"}">'
        f'<w:name w:val="heading {level}"/></w:style>'
        for level in (1, 2, 3)
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{_W}">{styles}</w:styles>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles_xml)
    return out.getvalue()


def ole_bytes(size: int = 4096) -> bytes:
    """The head of an OLE file (an encrypted docx or an old ``.doc``)."""
    return OLE_SIGNATURE + b"\x00" * (size - len(OLE_SIGNATURE))
