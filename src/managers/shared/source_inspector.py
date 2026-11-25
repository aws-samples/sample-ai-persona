"""Inspection of the head of an uploaded table (advanced generation U1, BR3.1〜BR3.3).

``SourceUploadManager`` reads only the first bytes of the file (4MB by
default, BR3.1) and hands them here. The result is the
:class:`~src.models.source_file.SourceMetadata` the screen shows and the
preparation in the sandbox starts from. This module is a pure function over
those bytes: no I/O, no Service.

Encoding (BR3.2): a UTF-8 BOM means ``utf-8-sig``; a head of ASCII only is
``undetermined`` (the preparation settles it, BR4.3); otherwise the first of
UTF-8, CP932 and EUC-JP that decodes the head strictly. Strict Shift_JIS is
not tried (CP932 covers it).

A head that is not a table raises :class:`SourceInspectionError` with one
code per way of fixing it (U2 BR12.1): an empty column name
(``SOURCE_EMPTY_COLUMN_NAME``), a duplicated one
(``SOURCE_DUPLICATE_COLUMN_NAME``), nothing decodes
(``SOURCE_ENCODING_UNKNOWN``), binary / empty / a line longer than the head
(``SOURCE_NOT_A_TABLE``). ``context["reason"]`` names the reason with a fixed
word (never file content).
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime
from typing import Iterable, Sequence

from ...models.errors import CodedError, ErrorCode
from ...models.source_file import (
    SAMPLE_ROWS_MAX,
    DocumentMetadata,
    SourceColumn,
    SourceEncoding,
    SourceFormat,
    SourceKind,
    SourceMetadata,
)

#: Rows used to infer column types (enough for a stable guess, cheap to scan).
TYPE_SAMPLE_ROWS = 1000
#: Most columns a table may have; the type scan costs columns x rows.
MAX_COLUMNS = 1000
#: Longest column name; with MAX_COLUMNS this bounds the stored metadata.
MAX_COLUMN_NAME_CHARS = 100

_BOM = b"\xef\xbb\xbf"
_DECODE_ORDER = (SourceEncoding.UTF8, SourceEncoding.CP932, SourceEncoding.EUC_JP)
_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")
_BOOLEAN = frozenset({"true", "false"})


class SourceInspectionError(CodedError):
    """The head is not a table this feature can read (one code per fix, BR12.1)."""

    code = ErrorCode.SOURCE_NOT_A_TABLE


#: Reason word -> the code that tells the user how to fix it (U2 BR12.1).
_REASON_CODES = {
    "empty_column_name": ErrorCode.SOURCE_EMPTY_COLUMN_NAME,
    "duplicate_column_name": ErrorCode.SOURCE_DUPLICATE_COLUMN_NAME,
    "encoding": ErrorCode.SOURCE_ENCODING_UNKNOWN,
    "binary": ErrorCode.SOURCE_NOT_A_TABLE,
    "empty": ErrorCode.SOURCE_NOT_A_TABLE,
    "line_too_long": ErrorCode.SOURCE_NOT_A_TABLE,
    "too_many_columns": ErrorCode.SOURCE_NOT_A_TABLE,
    "column_name_too_long": ErrorCode.SOURCE_NOT_A_TABLE,
}


def _invalid(reason: str) -> SourceInspectionError:
    return SourceInspectionError(
        f"not a table: {reason}",
        code=_REASON_CODES.get(reason, ErrorCode.SOURCE_NOT_A_TABLE),
        context={"reason": reason},
    )


def _whole_lines(head: bytes, total_size: int) -> bytes:
    """Drop the line the read cut in the middle (BR3.1)."""
    if len(head) >= total_size:
        return head
    end = head.rfind(b"\n")
    if end < 0:
        raise _invalid("line_too_long")
    return head[: end + 1]


def _decode(data: bytes) -> tuple[SourceEncoding, str]:
    """The encoding and text of ``data`` (BR3.2)."""
    if data.startswith(_BOM):
        try:
            return SourceEncoding.UTF8_SIG, data[len(_BOM) :].decode("utf-8")
        except UnicodeDecodeError:
            raise _invalid("encoding") from None
    if data.isascii():
        return SourceEncoding.UNDETERMINED, data.decode("ascii")
    for encoding in _DECODE_ORDER:
        try:
            return encoding, data.decode(encoding.value)
        except UnicodeDecodeError:
            continue
    raise _invalid("encoding")


def _records(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [row for row in reader if any(cell.strip() for cell in row)]


def _check_header(header: Sequence[str]) -> list[str]:
    if len(header) > MAX_COLUMNS:
        raise _invalid("too_many_columns")
    names = [name.strip() for name in header]
    if any(len(name) > MAX_COLUMN_NAME_CHARS for name in names):
        raise _invalid("column_name_too_long")
    if not names or any(not name for name in names):
        raise _invalid("empty_column_name")
    if len(set(names)) != len(names):
        raise _invalid("duplicate_column_name")
    return names


def _type_of(values: Iterable[str]) -> str:
    """Inferred type of one column; the same values always give the same type."""
    present = [v.strip() for v in values if v.strip()]
    if not present:
        return "string"
    for name, matches in (
        ("integer", lambda v: bool(_INTEGER.match(v))),
        ("number", lambda v: bool(_NUMBER.match(v))),
        ("date", lambda v: bool(_DATE.match(v))),
        ("boolean", lambda v: v.lower() in _BOOLEAN),
    ):
        if all(matches(v) for v in present):
            return name
    return "string"


def _estimate_rows(rows: int, used_bytes: int, total_size: int) -> int:
    if used_bytes >= total_size or used_bytes <= 0:
        return rows
    return int(round(rows * total_size / used_bytes))


def inspect_table_head(
    head: bytes,
    *,
    source_format: SourceFormat,
    total_size: int,
    now: datetime,
) -> SourceMetadata:
    """Inspect the first bytes of a CSV / TSV file (BR3.1〜BR3.3).

    Args:
        head: The first bytes of the object (the whole object if smaller).
        source_format: CSV or TSV, decided from the file name at start.
        total_size: Size of the whole object, to estimate the row count.
        now: The inspection time.

    Returns:
        The metadata; ``sample_rows`` is cut to the display limits.

    Raises:
        SourceInspectionError: A ``SOURCE_*`` code (BR12.1) when the head is not
            a table.
    """
    if b"\x00" in head:
        raise _invalid("binary")
    used = _whole_lines(head, total_size)
    encoding, text = _decode(used)
    delimiter = source_format.delimiter
    records = _records(text, delimiter)
    if not records:
        raise _invalid("empty")
    names = _check_header(records[0])
    rows = records[1:]
    columns = [
        SourceColumn(
            name=name,
            inferred_type=_type_of(
                row[i] if i < len(row) else "" for row in rows[:TYPE_SAMPLE_ROWS]
            ),
        )
        for i, name in enumerate(names)
    ]
    return SourceMetadata.create(
        format=source_format,
        encoding=encoding,
        delimiter=delimiter,
        columns=columns,
        sample_rows=rows[:SAMPLE_ROWS_MAX],
        estimated_rows=_estimate_rows(len(rows), len(used), total_size),
        inspected_bytes=len(head),
        inspected_at=now,
    )


# --- U4: documents (BR22.1〜BR22.3, NFR3.25) ---------------------------------
#
# Only the head (4MB) and the tail (1MB) are read; the bytes are matched, no
# library parses them. What cannot be judged from them is left ready: the
# preparation in the sandbox finds it (BR22.2, BR24.6).

#: ``%PDF-`` must start within this many bytes (BR22.1).
_PDF_SIGNATURE_WINDOW = 1024
_PDF_SIGNATURE = b"%PDF-"
_ZIP_SIGNATURE = b"PK\x03\x04"
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_END = b"PK\x05\x06"
_ZIP_END_SIZE = 22
_ZIP64_MARK = 0xFFFFFFFF
_DOCX_PART = b"word/document.xml"
_TRAILER = re.compile(rb"trailer\s*<<")
_XREF_STREAM = re.compile(rb"/Type\s*/XRef\b")
_ENCRYPT_KEY = re.compile(rb"/Encrypt\b")
#: The trailer dictionary is short; look this far after ``trailer``.
_TRAILER_WINDOW = 4096
#: Bytes a cut multibyte character can leave at the end of the head (BR22.1).
_CUT_CHARACTER_BYTES = 3


def _not_document(reason: str) -> SourceInspectionError:
    return SourceInspectionError(
        f"not a document: {reason}",
        code=ErrorCode.SOURCE_NOT_A_DOCUMENT,
        context={"reason": reason},
    )


def _encrypted(reason: str) -> SourceInspectionError:
    return SourceInspectionError(
        f"encrypted document: {reason}",
        code=ErrorCode.DOCUMENT_ENCRYPTED,
        context={"reason": reason},
    )


def _trailer_dicts(data: bytes) -> Iterable[bytes]:
    """The trailer dictionaries in ``data``: classic trailers and XRef streams."""
    for match in _TRAILER.finditer(data):
        end = data.find(b"startxref", match.end())
        yield data[match.end() : end if end >= 0 else match.end() + _TRAILER_WINDOW]
    for match in _XREF_STREAM.finditer(data):
        start = data.rfind(b"obj", 0, match.start())
        end = data.find(b"stream", match.end())
        yield data[start + 3 if start >= 0 else 0 : end if end >= 0 else len(data)]


def _check_pdf(head: bytes, tail: bytes) -> None:
    start = head.find(_PDF_SIGNATURE, 0, _PDF_SIGNATURE_WINDOW + len(_PDF_SIGNATURE))
    if start < 0 or start > _PDF_SIGNATURE_WINDOW:
        raise _not_document("pdf_signature")
    # The last trailer is in the tail; a linearised PDF also has one at the
    # head. Only the trailer dictionary counts, never text in the body.
    for data in (tail, head):
        if any(_ENCRYPT_KEY.search(d) for d in _trailer_dicts(data)):
            raise _encrypted("pdf_encrypt")


def _check_docx(head: bytes, tail: bytes, total_size: int) -> None:
    if head.startswith(_OLE_SIGNATURE):
        # An encrypted docx and an old .doc cannot be told apart (BR22.1).
        raise _encrypted("ole")
    if not head.startswith(_ZIP_SIGNATURE):
        raise _not_document("zip_signature")
    end = tail.rfind(_ZIP_END)
    if end < 0 or end + _ZIP_END_SIZE > len(tail):
        raise _not_document("zip_end")
    directory_size = int.from_bytes(tail[end + 12 : end + 16], "little")
    directory_offset = int.from_bytes(tail[end + 16 : end + 20], "little")
    tail_start = total_size - len(tail)
    start = directory_offset - tail_start
    if directory_offset == _ZIP64_MARK or start < 0:
        return  # not judged here: the preparation reads it (NFR3.25)
    if _DOCX_PART not in tail[start : start + directory_size]:
        raise _not_document("docx_part")


def _text_head(head: bytes, total_size: int) -> bytes:
    """Whole lines of the head; without a line break, a cut character dropped."""
    if len(head) >= total_size:
        return head
    end = head.rfind(b"\n")
    if end >= 0:
        return head[: end + 1]
    for cut in range(_CUT_CHARACTER_BYTES + 1):
        candidate = head[: len(head) - cut] if cut else head
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            continue
    return head[: max(len(head) - _CUT_CHARACTER_BYTES, 0)]


def inspect_document(
    head: bytes,
    tail: bytes,
    *,
    source_format: SourceFormat,
    total_size: int,
    now: datetime,
    chars_per_mb: int,
) -> DocumentMetadata:
    """Inspect the head and the tail of a document (BR22.1〜BR22.3).

    Args:
        head: The first bytes (4MB, or the whole object if smaller).
        tail: The last bytes (1MB, or the whole object if smaller).
        source_format: ``pdf``, ``docx``, ``txt`` or ``md`` from the extension.
        total_size: Size of the whole object.
        now: The inspection time.
        chars_per_mb: Characters per MB for the estimate of a PDF / docx
            (``config``); txt and md use the ratio of the head instead.

    Returns:
        The metadata with the estimate (a guide only, never a refusal).

    Raises:
        SourceInspectionError: ``SOURCE_NOT_A_DOCUMENT``,
            ``DOCUMENT_ENCRYPTED`` or ``SOURCE_ENCODING_UNKNOWN``.
        ValueError: ``source_format`` is a table format.
    """
    if source_format.kind is not SourceKind.DOCUMENT:
        raise ValueError("not a document format")
    inspected = len(head) + (len(tail) if len(head) < total_size else 0)
    encoding: SourceEncoding | None = None
    if source_format is SourceFormat.PDF:
        _check_pdf(head, tail)
        estimate = math.ceil(total_size / 1_000_000 * chars_per_mb)
    elif source_format is SourceFormat.DOCX:
        _check_docx(head, tail, total_size)
        estimate = math.ceil(total_size / 1_000_000 * chars_per_mb)
    else:
        if b"\x00" in head:
            raise _not_document("binary")
        used = _text_head(head, total_size)
        encoding, text = _decode(used)
        estimate = (
            len(text)
            if len(used) >= total_size or not used
            else int(round(len(text) * total_size / len(used)))
        )
    return DocumentMetadata(
        format=source_format,
        estimated_chars=max(int(estimate), 0),
        inspected_bytes=inspected,
        inspected_at=now,
        encoding=encoding,
    )
