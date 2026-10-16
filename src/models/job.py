"""
Job data model for async job tracking (contract C14).

Since v1.0.0 every job records who submitted it (``owner_sub``); results are
returned only to that user (BR2.12). Jobs written before v1.0.0 carry no owner
and are returned to nobody.

A failed job keeps only an error code (U6, BR8.3, ``error-catalog.md``):
the exception text is logged, never stored. Old records that stored the
exception text (``error``) are read as ``UNKNOWN`` and the text is dropped.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

#: ``ErrorCode.UNKNOWN`` (models may not depend on the catalog's helpers).
_UNKNOWN_CODE = "unknown"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Job:
    """Represents an async job (persona generation, discussion, etc.).

    Attributes:
        id: Job ID.
        status: Current status.
        created_at: When the job was submitted.
        updated_at: Last status change.
        result: The result once completed.
        error_code: ``ErrorCode`` value of a failed job (never the exception text).
        owner_sub: ``sub`` of the user who submitted it. ``None`` for jobs
            written before v1.0.0.
        job_type: What kind of work the job does (for logs).
    """

    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime = field(default_factory=datetime.now)
    result: Optional[Any] = None
    error_code: Optional[str] = None
    owner_sub: Optional[str] = None
    job_type: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Plain values; fields that are ``None`` are left out."""
        data: dict[str, Any] = {
            "id": self.id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        optional = {
            "result": self.result,
            "error_code": self.error_code,
            "owner_sub": self.owner_sub,
            "job_type": self.job_type,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        """Build a Job from :meth:`to_dict` values.

        A missing or empty ``owner_sub`` becomes ``None`` (a pre-v1.0.0 job).
        An old ``error`` text becomes ``unknown`` and is not kept.
        """
        error_code = data.get("error_code") or None
        if error_code is None and data.get("error"):
            error_code = _UNKNOWN_CODE
        return cls(
            id=str(data["id"]),
            status=JobStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(
                data.get("updated_at") or data["created_at"]
            ),
            result=data.get("result"),
            error_code=error_code,
            owner_sub=data.get("owner_sub") or None,
            job_type=data.get("job_type") or None,
        )
