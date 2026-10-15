"""Uploaded files and their attachment to a parent (U5, entities.md UploadedFile).

An upload lives under ``uploads/{owner_sub}/{file_type}/{id}_{name}`` so two
users' files never collide (BR5.1). Until it is attached to a parent
(a discussion or a survey template) only its owner may use it, and only for
24 hours: the item carries ``expires_at`` (the DynamoDB TTL) and the S3 object
carries the ``pending=true`` tag (the lifecycle filter) (BR5.2). Attaching
removes both (BR5.3).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping

#: Files that may be uploaded and attached (knowledge imports are never stored).
FILE_TYPES: tuple[str, ...] = ("discussion_document", "survey_image")
#: Parents a file may be attached to.
PARENT_TYPES: tuple[str, ...] = ("discussion", "survey_template")
#: How long an unattached upload may be used (BR5.2).
UPLOAD_TTL_SECONDS = 24 * 3600
#: Key prefix of uploads.
UPLOAD_KEY_PREFIX = "uploads"
#: Tag that marks an unattached upload for the lifecycle rule.
PENDING_TAG = ("pending", "true")

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def is_uuid(value: str) -> bool:
    """Whether ``value`` has the shape of a UUID (IDs are checked before use)."""
    return bool(_UUID.match(value or ""))


def upload_key(owner_sub: str, file_type: str, file_id: str, filename: str) -> str:
    """The S3 key of an upload (BR5.1)."""
    return f"{UPLOAD_KEY_PREFIX}/{owner_sub}/{file_type}/{file_id}_{filename}"


def file_id_from_key(key: str) -> str | None:
    """The file ID inside an upload key, or ``None`` for any other shape.

    Used to accept only real upload keys where a path comes from the screen
    (``image-preview``, template images; security-design.md §4).
    """
    parts = (key or "").split("/", 3)
    if len(parts) != 4:
        return None
    prefix, owner, file_type, name = parts
    if prefix != UPLOAD_KEY_PREFIX or not owner or file_type not in FILE_TYPES:
        return None
    file_id, sep, rest = name.partition("_")
    if not sep or not rest or not is_uuid(file_id):
        return None
    return file_id


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


@dataclass(frozen=True)
class UploadedFile:
    """Metadata of one upload.

    Attributes:
        id: UUID.
        owner_sub: The uploader.
        file_type: ``discussion_document`` or ``survey_image``.
        s3_key: ``uploads/{owner_sub}/{file_type}/{id}_{name}``.
        original_filename: The name after the safety check.
        file_size: Bytes.
        file_hash: SHA-256 of the content.
        mime_type: MIME type.
        uploaded_at: Upload time.
        file_path: Where the bytes are (``s3://bucket/key``, or a local path
            when S3 is not configured).
        expires_at: Epoch seconds; only while unattached (TTL attribute).
        parent_type: ``discussion`` or ``survey_template`` once attached.
        parent_id: The parent's ID once attached.
    """

    id: str
    owner_sub: str
    file_type: str
    s3_key: str
    original_filename: str
    file_size: int
    file_hash: str
    mime_type: str
    uploaded_at: datetime
    file_path: str = ""
    expires_at: int | None = None
    parent_type: str | None = None
    parent_id: str | None = None

    def __post_init__(self) -> None:
        if self.file_type not in FILE_TYPES:
            raise ValueError(f"unknown file type {self.file_type!r}")
        if (self.parent_type is None) != (self.parent_id is None):
            raise ValueError("parent_type and parent_id come together")
        if self.parent_type is not None and self.parent_type not in PARENT_TYPES:
            raise ValueError(f"unknown parent type {self.parent_type!r}")

    @classmethod
    def create_new(
        cls,
        *,
        owner_sub: str,
        file_type: str,
        original_filename: str,
        file_size: int,
        file_hash: str,
        mime_type: str,
        now: datetime,
        file_id: str | None = None,
    ) -> "UploadedFile":
        """A new, unattached upload expiring in 24 hours (BR5.1, BR5.2)."""
        new_id = file_id or str(uuid.uuid4())
        return cls(
            id=new_id,
            owner_sub=owner_sub,
            file_type=file_type,
            s3_key=upload_key(owner_sub, file_type, new_id, original_filename),
            original_filename=original_filename,
            file_size=file_size,
            file_hash=file_hash,
            mime_type=mime_type,
            uploaded_at=now,
            expires_at=_epoch(now) + UPLOAD_TTL_SECONDS,
        )

    @property
    def is_attached(self) -> bool:
        return self.parent_id is not None

    def usable_before_attach(self, owner_sub: str, now_epoch: int) -> bool:
        """Unattached, owned by ``owner_sub`` and not expired (BR5.4, BR5.5).

        ``now_epoch`` at or past ``expires_at`` is expired.
        """
        return (
            not self.is_attached
            and bool(owner_sub)
            and self.owner_sub == owner_sub
            and self.expires_at is not None
            and now_epoch < self.expires_at
        )

    def attached_to(self, parent_type: str, parent_id: str) -> "UploadedFile":
        """A copy attached to the parent (no expiry)."""
        return replace(
            self, parent_type=parent_type, parent_id=parent_id, expires_at=None
        )

    def with_path(self, file_path: str) -> "UploadedFile":
        """A copy with the storage path set."""
        return replace(self, file_path=file_path)

    def to_dict(self) -> dict[str, Any]:
        """Serialize without ``None`` values."""
        data: dict[str, Any] = {
            "id": self.id,
            "owner_sub": self.owner_sub,
            "file_type": self.file_type,
            "s3_key": self.s3_key,
            "original_filename": self.original_filename,
            "file_size": int(self.file_size),
            "file_hash": self.file_hash,
            "mime_type": self.mime_type,
            "uploaded_at": self.uploaded_at.isoformat(),
        }
        if self.file_path:
            data["file_path"] = self.file_path
        if self.expires_at is not None:
            data["expires_at"] = int(self.expires_at)
        if self.parent_type is not None:
            data["parent_type"] = self.parent_type
        if self.parent_id is not None:
            data["parent_id"] = self.parent_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UploadedFile":
        expires = data.get("expires_at")
        return cls(
            id=str(data["id"]),
            owner_sub=str(data.get("owner_sub", "")),
            file_type=str(data["file_type"]),
            s3_key=str(data.get("s3_key", "")),
            original_filename=str(data.get("original_filename", "")),
            file_size=int(data.get("file_size", 0)),
            file_hash=str(data.get("file_hash", "")),
            mime_type=str(data.get("mime_type", "application/octet-stream")),
            uploaded_at=datetime.fromisoformat(str(data["uploaded_at"])),
            file_path=str(data.get("file_path", "")),
            expires_at=None if expires is None else int(expires),
            parent_type=data.get("parent_type"),
            parent_id=data.get("parent_id"),
        )


@dataclass(frozen=True)
class AttachResult:
    """What ``FileAttachments.attach_all`` attached (security-design.md §3).

    Attributes:
        parent_type: The parent's type.
        parent_id: The parent's ID.
        original_expires_at: file ID -> the ``expires_at`` it had before
            (``detach`` puts it back).
    """

    parent_type: str
    parent_id: str
    original_expires_at: dict[str, int]

    @classmethod
    def empty(cls, parent_type: str, parent_id: str) -> "AttachResult":
        """Nothing was attached."""
        return cls(parent_type=parent_type, parent_id=parent_id, original_expires_at={})

    @property
    def file_ids(self) -> tuple[str, ...]:
        return tuple(self.original_expires_at)
