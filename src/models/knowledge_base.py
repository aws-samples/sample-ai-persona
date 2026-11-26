"""
Knowledge Base data model for Bedrock Knowledge Base integration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any
import uuid

#: A knowledge base the team runs on its own vector store (Self-managed KB).
KB_TYPE_SELF_MANAGED = "self_managed"
#: An Amazon Bedrock Managed Knowledge Base (searched with managed search).
KB_TYPE_MANAGED = "managed"
#: The knowledge base types an admin may register.
KB_TYPES = frozenset({KB_TYPE_SELF_MANAGED, KB_TYPE_MANAGED})
#: The type AWS recommends; the registration screen preselects it.
KB_TYPE_RECOMMENDED = KB_TYPE_MANAGED
#: Display labels per knowledge base type, in the order the screens list them
#: (the recommended type first).
KB_TYPE_LABELS: Dict[str, str] = {
    KB_TYPE_MANAGED: "Managed KB",
    KB_TYPE_SELF_MANAGED: "Self-managed KB",
}


@dataclass
class KnowledgeBase:
    """Information about a registered Bedrock Knowledge Base.

    ``shared_group_ids`` is the usage scope (organization asset). When empty,
    only admins can use it. Only a change of the usage scope is written.
    ``kb_type`` is how it is searched (Self-managed KB or Managed KB).
    Older records without this field were registered from an external KB, so
    they are Self-managed KB.
    """

    id: str
    knowledge_base_id: str  # Bedrock KB ID (e.g., "KB12345678")
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)
    kb_type: str = KB_TYPE_SELF_MANAGED

    @classmethod
    def create_new(
        cls,
        knowledge_base_id: str,
        name: str,
        description: str = "",
        kb_type: str = KB_TYPE_SELF_MANAGED,
    ) -> "KnowledgeBase":
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            knowledge_base_id=knowledge_base_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            kb_type=kb_type,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = self._base_dict()
        if self.shared_group_ids:
            data["shared_group_ids"] = sorted(self.shared_group_ids)
        return data

    def _base_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "knowledge_base_id": self.knowledge_base_id,
            "name": self.name,
            "description": self.description,
            "kb_type": self.kb_type,
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else self.created_at,
            "updated_at": self.updated_at.isoformat()
            if isinstance(self.updated_at, datetime)
            else self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeBase":
        return cls(
            id=data["id"],
            knowledge_base_id=data["knowledge_base_id"],
            name=data["name"],
            description=data.get("description", ""),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            shared_group_ids=frozenset(data.get("shared_group_ids") or ()),
            kb_type=_known_kb_type(data.get("kb_type")),
        )


def _known_kb_type(value: Any) -> str:
    """Return ``value`` when it is a known type, else ``self_managed``.

    Records written before the type existed, or holding an unexpected value,
    read as a Self-managed KB.
    """
    return value if value in KB_TYPES else KB_TYPE_SELF_MANAGED


# The persona-to-KB binding (one KB per persona) is held by PersonaBinding
# (fixed key KB) in src/models/persona_binding.py.
