"""
DiscussionReport data model for the AI Persona System.

Since v1.0.0 (U6, ADR-002) a saved report is one item of
``DiscussionReports`` (PK ``discussion_id``, SK ``report_id``). The server
assigns ``report_id`` (BR4.4); the screen never sends it.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Optional, Dict, Any, Mapping
import uuid

#: The report templates (entities.md DiscussionReport).
REPORT_TEMPLATE_TYPES: tuple[str, ...] = ("summary", "review", "custom", "data_driven")


@dataclass(frozen=True)
class DiscussionReport:
    """A report generated from a discussion and saved.

    Attributes:
        discussion_id: The discussion it belongs to.
        report_id: UUID assigned by the server.
        template_type: One of :data:`REPORT_TEMPLATE_TYPES`.
        content: Markdown.
        created_at: When it was saved.
        custom_prompt: The user's extra instruction, when given.
        updated_at: Last content change.
    """

    discussion_id: str
    report_id: str
    template_type: str
    content: str
    created_at: datetime
    custom_prompt: Optional[str] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.template_type not in REPORT_TEMPLATE_TYPES:
            raise ValueError(f"unknown report template {self.template_type!r}")

    @property
    def id(self) -> str:
        """Alias of :attr:`report_id` (the screens address reports by ``id``)."""
        return self.report_id

    @classmethod
    def create_new(
        cls,
        discussion_id: str,
        template_type: str,
        content: str,
        custom_prompt: Optional[str] = None,
        *,
        now: Optional[datetime] = None,
    ) -> "DiscussionReport":
        """A new report with a server-assigned ID (BR4.4)."""
        return cls(
            discussion_id=discussion_id,
            report_id=str(uuid.uuid4()),
            template_type=template_type,
            content=content,
            created_at=now or datetime.now(),
            custom_prompt=custom_prompt or None,
        )

    def with_content(
        self, content: str, *, now: Optional[datetime] = None
    ) -> "DiscussionReport":
        """Return a copy with new content and ``updated_at``."""
        return replace(self, content=content, updated_at=now or datetime.now())

    def to_dict(self) -> Dict[str, Any]:
        """Plain values for storage; ``None`` fields are left out."""
        data: Dict[str, Any] = {
            "discussion_id": self.discussion_id,
            "report_id": self.report_id,
            "template_type": self.template_type,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }
        if self.custom_prompt is not None:
            data["custom_prompt"] = self.custom_prompt
        if self.updated_at is not None:
            data["updated_at"] = self.updated_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DiscussionReport":
        """Read a stored report (the pre-v1.0.0 inner shape uses ``id``)."""
        updated_at = data.get("updated_at")
        return cls(
            discussion_id=str(data.get("discussion_id") or ""),
            report_id=str(data.get("report_id") or data.get("id") or ""),
            template_type=str(data["template_type"]),
            content=str(data.get("content") or ""),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            custom_prompt=data.get("custom_prompt") or None,
            updated_at=datetime.fromisoformat(str(updated_at)) if updated_at else None,
        )
