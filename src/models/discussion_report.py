"""
DiscussionReport data model for the AI Persona System.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
import uuid


@dataclass
class DiscussionReport:
    """Represents a generated report from a discussion."""

    id: str
    template_type: str  # "summary", "review", "custom"
    content: str
    created_at: datetime
    custom_prompt: Optional[str] = None

    @classmethod
    def create_new(
        cls,
        template_type: str,
        content: str,
        custom_prompt: Optional[str] = None,
    ) -> "DiscussionReport":
        return cls(
            id=str(uuid.uuid4()),
            template_type=template_type,
            content=content,
            created_at=datetime.now(),
            custom_prompt=custom_prompt,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "template_type": self.template_type,
            "custom_prompt": self.custom_prompt,
            "content": self.content,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscussionReport":
        return cls(
            id=data["id"],
            template_type=data["template_type"],
            custom_prompt=data.get("custom_prompt"),
            content=data["content"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )
