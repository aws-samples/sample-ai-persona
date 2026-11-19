"""Uploaded source files of advanced generation (beta) U1 (entities.md, contract C4).

A :class:`SourceFile` is one table (CSV / TSV) the user uploaded straight to S3
with presigned part URLs. Its bytes live under ``sources/<owner_sub>/<id>``
and never pass through the application (BR2.6). The application keeps the
record (``SourceFiles`` table) and, once the head of the file has been
inspected, the :class:`SourceMetadata` it found (BR3.3).

The types here are plain data. Conditional writes live in
``SourceFileStore`` (Service layer), the rules in ``SourceUploadManager`` and
``SourceFileCatalog``.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Sequence

from ._advanced_common import drop_none, iso, parse_dt, parse_optional_dt

#: Version of the stored item (contract C4).
SCHEMA_VERSION = 1
#: Key prefix of uploaded sources (lifecycle rules filter on it).
SOURCE_KEY_PREFIX = "sources"
#: Longest file name kept for display (security-design.md §6).
FILENAME_MAX_CHARS = 255
#: Display sample limits; the sample may contain personal data and must fit
#: in one DynamoDB item (BR3.3).
SAMPLE_ROWS_MAX = 5
SAMPLE_COLUMNS_MAX = 50
SAMPLE_CELL_MAX_CHARS = 200


class SourceKind(StrEnum):
    """What a source file holds. U1 handles tables only (documents are U4)."""

    TABLE = "table"


class SourceFileStatus(StrEnum):
    """Lifecycle of a source file (functional-spec.md §3). ``deleting`` is U2."""

    UPLOADING = "uploading"
    INSPECTING = "inspecting"
    READY = "ready"
    ERROR = "error"
    DELETING = "deleting"


class SourceFormat(StrEnum):
    """Table formats accepted in U1 (BR2.1)."""

    CSV = "csv"
    TSV = "tsv"

    @property
    def delimiter(self) -> str:
        return "\t" if self is SourceFormat.TSV else ","


class SourceEncoding(StrEnum):
    """Encodings the inspection may report (BR3.2).

    ``undetermined`` means the inspected head was ASCII only; the preparation
    in the sandbox settles it (BR4.3).
    """

    UTF8 = "utf-8"
    UTF8_SIG = "utf-8-sig"
    CP932 = "cp932"
    EUC_JP = "euc-jp"
    UNDETERMINED = "undetermined"


def source_key(owner_sub: str, source_file_id: str) -> str:
    """The S3 key of a source file (``sources/<owner_sub>/<id>``, contract C2)."""
    return f"{SOURCE_KEY_PREFIX}/{owner_sub}/{source_file_id}"


def part_count_for(size_bytes: int, part_size_bytes: int) -> int:
    """Number of multipart parts for ``size_bytes`` (BR2.3: ceil(size / part)).

    Raises:
        ValueError: Either value is not positive.
    """
    if size_bytes < 1 or part_size_bytes < 1:
        raise ValueError("size and part size must be positive")
    return math.ceil(size_bytes / part_size_bytes)


def _clean_filename(name: str) -> str:
    """Drop control characters and cut to :data:`FILENAME_MAX_CHARS`."""
    cleaned = "".join(ch for ch in name if ch.isprintable())
    return cleaned[:FILENAME_MAX_CHARS]


@dataclass(frozen=True)
class SourceColumn:
    """One column found by the inspection (name is data: escape on display)."""

    name: str
    inferred_type: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "inferred_type": self.inferred_type}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceColumn":
        return cls(name=str(data["name"]), inferred_type=str(data["inferred_type"]))


@dataclass(frozen=True)
class SourceMetadata:
    """What the inspection of the head of a file found (FR4.4, contract C4).

    Use :meth:`create` to build one: it cuts the display sample to
    :data:`SAMPLE_ROWS_MAX` rows, :data:`SAMPLE_COLUMNS_MAX` columns and
    :data:`SAMPLE_CELL_MAX_CHARS` characters per cell.

    Attributes:
        format: ``csv`` or ``tsv``.
        encoding: One of :class:`SourceEncoding`.
        delimiter: ``,`` for CSV, a tab for TSV.
        columns: Columns with an inferred type.
        sample_rows: First rows for the "view columns" display.
        estimated_rows: Size divided by the mean line length of the head.
        inspected_bytes: How many bytes of the head were read.
        inspected_at: When the inspection ran.
    """

    format: SourceFormat
    encoding: SourceEncoding
    delimiter: str
    columns: tuple[SourceColumn, ...]
    sample_rows: tuple[tuple[str, ...], ...]
    estimated_rows: int
    inspected_bytes: int
    inspected_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", SourceFormat(self.format))
        object.__setattr__(self, "encoding", SourceEncoding(self.encoding))
        if self.delimiter != self.format.delimiter:
            raise ValueError("delimiter does not match the format")
        if self.estimated_rows < 0 or self.inspected_bytes < 0:
            raise ValueError("counts must not be negative")

    @classmethod
    def create(
        cls,
        *,
        format: SourceFormat | str,
        encoding: SourceEncoding | str,
        delimiter: str,
        columns: Sequence[SourceColumn],
        sample_rows: Sequence[Sequence[str]],
        estimated_rows: int,
        inspected_bytes: int,
        inspected_at: datetime,
    ) -> "SourceMetadata":
        """Build metadata with the display sample cut to the limits (BR3.3)."""
        rows = tuple(
            tuple(
                str(cell)[:SAMPLE_CELL_MAX_CHARS] for cell in row[:SAMPLE_COLUMNS_MAX]
            )
            for row in sample_rows[:SAMPLE_ROWS_MAX]
        )
        return cls(
            format=SourceFormat(format),
            encoding=SourceEncoding(encoding),
            delimiter=delimiter,
            columns=tuple(columns),
            sample_rows=rows,
            estimated_rows=int(estimated_rows),
            inspected_bytes=int(inspected_bytes),
            inspected_at=inspected_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format.value,
            "encoding": self.encoding.value,
            "delimiter": self.delimiter,
            "columns": [c.to_dict() for c in self.columns],
            "sample_rows": [list(row) for row in self.sample_rows],
            "estimated_rows": int(self.estimated_rows),
            "inspected_bytes": int(self.inspected_bytes),
            "inspected_at": iso(self.inspected_at),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceMetadata":
        return cls.create(
            format=str(data["format"]),
            encoding=str(data["encoding"]),
            delimiter=str(data["delimiter"]),
            columns=[SourceColumn.from_dict(c) for c in data.get("columns", [])],
            sample_rows=[
                [str(cell) for cell in row] for row in data.get("sample_rows", [])
            ],
            estimated_rows=int(data.get("estimated_rows", 0)),
            inspected_bytes=int(data.get("inspected_bytes", 0)),
            inspected_at=parse_dt(data["inspected_at"]),
        )


@dataclass(frozen=True)
class SourceFile:
    """One uploaded source file (entities.md SourceFile, contract C4).

    Attributes:
        source_file_id: UUID.
        owner_sub: The uploader; every read checks it.
        filename: Display name (control characters removed, 255 characters).
        kind: ``table`` in U1.
        size_bytes: Declared size (checked against S3 at completion, BR2.7).
        s3_key: ``sources/<owner_sub>/<source_file_id>``.
        part_count: Number of multipart parts.
        status: One of :class:`SourceFileStatus`.
        created_at: When the upload started.
        multipart_upload_id: S3 multipart upload ID until completion.
        metadata: Inspection result; always present when ``ready``.
        checksum: ``ChecksumCRC64NVME`` of the whole object.
        error_code: ``ErrorCode`` value when ``error``.
        in_use_by: Generation using the file.
        in_use_until: The mark is free once this time has passed.
        last_used_at: Last time a generation used the file.
        updated_at: Last state change (the 15-minute retry of a stuck
            inspection compares against it, reliability-design.md §4).
        url_refresh_count: Part URL refreshes so far (24 at most, BR2.4).
    """

    source_file_id: str
    owner_sub: str
    filename: str
    kind: SourceKind
    size_bytes: int
    s3_key: str
    part_count: int
    status: SourceFileStatus
    created_at: datetime
    multipart_upload_id: str | None = None
    metadata: SourceMetadata | None = None
    checksum: str | None = None
    error_code: str | None = None
    in_use_by: str | None = None
    in_use_until: datetime | None = None
    last_used_at: datetime | None = None
    updated_at: datetime | None = field(default=None)
    url_refresh_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SourceKind(self.kind))
        object.__setattr__(self, "status", SourceFileStatus(self.status))
        if self.url_refresh_count < 0:
            raise ValueError("url_refresh_count must not be negative")
        if self.size_bytes < 1:
            raise ValueError("size_bytes must be positive")
        if not 1 <= self.part_count <= 10000:
            raise ValueError("part_count must be 1..10000")

    @classmethod
    def create_new(
        cls,
        *,
        owner_sub: str,
        filename: str,
        size_bytes: int,
        part_count: int,
        now: datetime,
        multipart_upload_id: str | None = None,
        source_file_id: str | None = None,
    ) -> "SourceFile":
        """A new upload in ``uploading`` under the owner's key (BR2.3)."""
        new_id = source_file_id or str(uuid.uuid4())
        return cls(
            source_file_id=new_id,
            owner_sub=owner_sub,
            filename=_clean_filename(filename),
            kind=SourceKind.TABLE,
            size_bytes=int(size_bytes),
            s3_key=source_key(owner_sub, new_id),
            part_count=int(part_count),
            status=SourceFileStatus.UPLOADING,
            created_at=now,
            multipart_upload_id=multipart_upload_id,
            updated_at=now,
        )

    def update(self, **changes: Any) -> "SourceFile":
        """A copy with ``changes`` applied (the instance itself never changes)."""
        return replace(self, **changes)

    def is_in_use(self, now: datetime) -> bool:
        """Whether a generation holds the in-use mark (an expired mark is free)."""
        return (
            self.in_use_by is not None
            and self.in_use_until is not None
            and now < self.in_use_until
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ``SourceFiles`` table without ``None`` values."""
        data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source_file_id": self.source_file_id,
            "owner_sub": self.owner_sub,
            "filename": self.filename,
            "kind": self.kind.value,
            "size_bytes": int(self.size_bytes),
            "s3_key": self.s3_key,
            "part_count": int(self.part_count),
            "status": self.status.value,
            "created_at": iso(self.created_at),
        }
        optional: dict[str, Any] = {
            "multipart_upload_id": self.multipart_upload_id,
            "source_metadata": self.metadata.to_dict() if self.metadata else None,
            "checksum": self.checksum,
            "error_code": self.error_code,
            "in_use_by": self.in_use_by,
            "in_use_until": iso(self.in_use_until) if self.in_use_until else None,
            "last_used_at": iso(self.last_used_at) if self.last_used_at else None,
            "updated_at": iso(self.updated_at) if self.updated_at else None,
            "url_refresh_count": self.url_refresh_count or None,
        }
        data.update(drop_none(optional))
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceFile":
        """Read an item written by :meth:`to_dict` (unknown fields are ignored)."""
        metadata = data.get("source_metadata")
        return cls(
            source_file_id=str(data["source_file_id"]),
            owner_sub=str(data["owner_sub"]),
            filename=str(data.get("filename", "")),
            kind=SourceKind(str(data.get("kind", SourceKind.TABLE.value))),
            size_bytes=int(data["size_bytes"]),
            s3_key=str(data["s3_key"]),
            part_count=int(data["part_count"]),
            status=SourceFileStatus(str(data["status"])),
            created_at=parse_dt(data["created_at"]),
            multipart_upload_id=data.get("multipart_upload_id"),
            metadata=SourceMetadata.from_dict(metadata) if metadata else None,
            checksum=data.get("checksum"),
            error_code=data.get("error_code"),
            in_use_by=data.get("in_use_by"),
            in_use_until=parse_optional_dt(data.get("in_use_until")),
            last_used_at=parse_optional_dt(data.get("last_used_at")),
            updated_at=parse_optional_dt(data.get("updated_at")),
            url_refresh_count=int(data.get("url_refresh_count", 0)),
        )


@dataclass(frozen=True)
class PartUrl:
    """A presigned ``UploadPart`` URL for one part number (never stored)."""

    part_number: int
    url: str

    def to_dict(self) -> dict[str, Any]:
        return {"part_number": int(self.part_number), "url": self.url}


@dataclass(frozen=True)
class UploadSession:
    """Part URLs handed to the browser at start or refresh (contract C1).

    It is a response value only: URLs are never written to the table, logs or
    events (security-design.md §3).

    Attributes:
        source_file_id: The upload.
        part_size_bytes: Fixed part size (the last part may be smaller).
        part_count: Parts of the whole upload.
        part_urls: URLs issued now (all parts at start, the requested ones
            at refresh).
        urls_expire_at: When the URLs stop working.
    """

    source_file_id: str
    part_size_bytes: int
    part_count: int
    part_urls: tuple[PartUrl, ...]
    urls_expire_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file_id": self.source_file_id,
            "part_size_bytes": int(self.part_size_bytes),
            "part_count": int(self.part_count),
            "part_urls": [p.to_dict() for p in self.part_urls],
            "expires_at": iso(self.urls_expire_at),
        }
