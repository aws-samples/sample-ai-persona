"""
Job data model for async job tracking.
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
    """Represents an async job (persona generation, discussion, etc.)."""

    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime = field(default_factory=datetime.now)
    result: Optional[Any] = None
    error: Optional[str] = None
