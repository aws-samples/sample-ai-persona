"""
Job data model for async job tracking (contract C14).

Since v1.0.0 every job records who submitted it (``owner_sub``); results are
returned only to that user (BR2.12). Jobs written before v1.0.0 carry no owner
and are returned to nobody.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


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
        error: Failure marker of a failed job (never shown as is).
        owner_sub: ``sub`` of the user who submitted it. ``None`` for jobs
            written before v1.0.0.
        job_type: What kind of work the job does (for logs).
    """

    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime = field(default_factory=datetime.now)
    result: Optional[Any] = None
    error: Optional[str] = None
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
            "error": self.error,
            "owner_sub": self.owner_sub,
            "job_type": self.job_type,
        }
        data.update({k: v for k, v in optional.items() if v is not None})
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        """Build a Job from :meth:`to_dict` values.

        A missing or empty ``owner_sub`` becomes ``None`` (a pre-v1.0.0 job).
        """
        return cls(
            id=str(data["id"]),
            status=JobStatus(data["status"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            result=data.get("result"),
            error=data.get("error") or None,
            owner_sub=data.get("owner_sub") or None,
            job_type=data.get("job_type") or None,
        )
