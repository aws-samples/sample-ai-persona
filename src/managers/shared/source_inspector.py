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

A head that is not a table (binary, empty, an empty or duplicated column
name, nothing decodes) raises :class:`SourceInspectionError` with
``UPLOAD_SOURCE_INVALID``; ``context["reason"]`` names the reason with a fixed
word (never file content).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from typing import Iterable, Sequence

from ...models.errors import CodedError, ErrorCode
from ...models.source_file import (
    SAMPLE_ROWS_MAX,
    SourceColumn,
    SourceEncoding,
    SourceFormat,
    SourceMetadata,
)

#: Rows used to infer column types (enough for a stable guess, cheap to scan).
TYPE_SAMPLE_ROWS = 1000

_BOM = b"\xef\xbb\xbf"
_DECODE_ORDER = (SourceEncoding.UTF8, SourceEncoding.CP932, SourceEncoding.EUC_JP)
_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_DATE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?$")
_BOOLEAN = frozenset({"true", "false"})


class SourceInspectionError(CodedError):
    """The head is not a table this feature can read (``UPLOAD_SOURCE_INVALID``)."""

    code = ErrorCode.UPLOAD_SOURCE_INVALID


def _invalid(reason: str) -> SourceInspectionError:
    return SourceInspectionError(
        f"not a table: {reason}",
        code=ErrorCode.UPLOAD_SOURCE_INVALID,
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
    names = [name.strip() for name in header]
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
        SourceInspectionError: ``UPLOAD_SOURCE_INVALID`` when the head is not
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
