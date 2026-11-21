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
from datetime import date, datetime, timedelta, timezone
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
        display_seq: Number that tells files of the same name apart, fixed
            at the start (U2 BR11.3). U1 records have none and read as 1.
        expires_at: When the file stops being usable (U2 BR13.2). The store
            writes it as epoch seconds (the TTL attribute). U1 records have
            none; :meth:`effective_expires_at` then counts from the last use
            or the creation.
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
    display_seq: int = 1
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SourceKind(self.kind))
        object.__setattr__(self, "status", SourceFileStatus(self.status))
        if self.url_refresh_count < 0:
            raise ValueError("url_refresh_count must not be negative")
        if self.display_seq < 1:
            raise ValueError("display_seq must be positive")
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
        display_seq: int = 1,
        expires_at: datetime | None = None,
    ) -> "SourceFile":
        """A new upload in ``uploading`` under the owner's key (BR2.3, BR11.3)."""
        new_id = source_file_id or str(uuid.uuid4())
        return cls(
            display_seq=int(display_seq),
            expires_at=expires_at,
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

    @property
    def display_name(self) -> str:
        """``filename`` for the first of a name, ``"filename (n)"`` after (BR11.3)."""
        if self.display_seq <= 1:
            return self.filename
        return f"{self.filename} ({self.display_seq})"

    def effective_expires_at(self, retention_days: int) -> datetime:
        """The stored expiry, or (last use or creation) + retention (BR13.2).

        The fallback covers U1 records written before ``expires_at`` existed.
        """
        if self.expires_at is not None:
            return self.expires_at
        base = self.last_used_at or self.created_at
        return base + timedelta(days=int(retention_days))

    def is_live(self, now: datetime, retention_days: int) -> bool:
        """Not being deleted and not past its expiry (BR13.5)."""
        return (
            self.status is not SourceFileStatus.DELETING
            and self.effective_expires_at(retention_days) > now
        )

    def is_in_use(self, now: datetime) -> bool:
        """A generation holds a mark that has not lapsed (BR14.2)."""
        return bool(
            self.in_use_by and self.in_use_until is not None and self.in_use_until > now
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the ``SourceFiles`` table without ``None`` values.

        ``expires_at`` is ISO text here like the other times; the store turns
        it into epoch seconds for the TTL.
        """
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
            "display_seq": int(self.display_seq),
        }
        optional: dict[str, Any] = {
            "expires_at": iso(self.expires_at) if self.expires_at else None,
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
            display_seq=int(data.get("display_seq") or 1),
            expires_at=_read_expiry(data.get("expires_at")),
        )


def _read_expiry(value: Any) -> datetime | None:
    """``expires_at`` as ISO text or epoch seconds (the TTL attribute)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return parse_dt(value)


class SourceAvailabilityState(StrEnum):
    """Whether the file an analysis record used is still there (BR14.3)."""

    AVAILABLE = "available"
    DELETED = "deleted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SourceAvailability:
    """The source file of an analysis record, judged when the record opens.

    Never stored (entities.md SourceAvailability).

    Attributes:
        source_file_id: The file ID kept in the record (empty for U1 records).
        display_name: The name kept in the record (shown even after deletion).
        state: ``available``, ``deleted`` or ``unknown`` (U1 record, no ID).
    """

    source_file_id: str
    display_name: str
    state: SourceAvailabilityState


@dataclass(frozen=True)
class SourceFileListItem:
    """One row of the uploaded-file list (entities.md SourceFileListItem).

    A read-only value built from a :class:`SourceFile`; never stored.

    Attributes:
        source_file_id: The file.
        display_name: ``filename`` or ``"filename (n)"`` (escape on display).
        status: The file's state.
        size_bytes: Declared size.
        expires_on: The day the file goes away; none while uploading or
            inspecting.
        error_code: The ``ErrorCode`` value of an ``error`` file.
        in_use: A running generation holds the file (delete is refused).
        selectable: ``ready`` and not in use.
    """

    source_file_id: str
    display_name: str
    status: SourceFileStatus
    size_bytes: int
    expires_on: date | None
    error_code: str | None
    in_use: bool
    selectable: bool

    @classmethod
    def of(
        cls, source_file: SourceFile, *, now: datetime, retention_days: int
    ) -> "SourceFileListItem":
        """The row of ``source_file`` at ``now``."""
        in_use = source_file.is_in_use(now)
        pending = source_file.status in (
            SourceFileStatus.UPLOADING,
            SourceFileStatus.INSPECTING,
        )
        return cls(
            source_file_id=source_file.source_file_id,
            display_name=source_file.display_name,
            status=source_file.status,
            size_bytes=source_file.size_bytes,
            expires_on=(
                None
                if pending
                else source_file.effective_expires_at(retention_days).date()
            ),
            error_code=source_file.error_code,
            in_use=in_use,
            selectable=source_file.status is SourceFileStatus.READY and not in_use,
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
