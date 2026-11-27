"""Memory Entry Model: data model of long-term memory entries."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class MemoryEntry:
    """Data model of a memory entry"""

    id: str
    actor_id: str  # Persona ID
    session_id: str  # Discussion ID
    content: str  # Content of the memory
    metadata: Dict[str, Any] = field(default_factory=dict)  # Additional metadata
    created_at: datetime = field(default_factory=datetime.now)
    relevance_score: Optional[float] = None  # Relevance score at search time
    parsed_topic: Optional[Dict[str, Any]] = None  # Parsed topic (for display)


@dataclass(frozen=True)
class DeleteAllResult:
    """Outcome of deleting every long-term memory of one actor.

    ``MemoryService.delete_all`` never raises; it reports here instead.

    Attributes:
        deleted: Records deleted.
        remaining: Records found but not deleted, or ``None`` when not every
            page could be read (unknown).
        timed_out: The deadline stopped the work.
        error_type: Class name of the failure that stopped it, if any.
    """

    deleted: int
    remaining: Optional[int]
    timed_out: bool
    error_type: Optional[str] = None

    @classmethod
    def nothing(cls) -> "DeleteAllResult":
        """Nothing to delete (for example, long-term memory is off)."""
        return cls(deleted=0, remaining=0, timed_out=False)

    @property
    def complete(self) -> bool:
        """Every record was read and deleted."""
        return self.remaining == 0 and not self.timed_out and self.error_type is None
