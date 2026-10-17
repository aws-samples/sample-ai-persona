"""
Knowledge Base data model for Bedrock Knowledge Base integration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Any
import uuid


@dataclass
class KnowledgeBase:
    """登録済みBedrock Knowledge Baseの情報

    U7: ``shared_group_ids`` は利用範囲（組織資産、BR5.1）。空なら admin だけが使える。
    書くのは利用範囲の変更だけ（BR1.5）。
    """

    id: str
    knowledge_base_id: str  # Bedrock KB ID (e.g., "KB12345678")
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    shared_group_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def create_new(
        cls, knowledge_base_id: str, name: str, description: str = ""
    ) -> "KnowledgeBase":
        now = datetime.now()
        return cls(
            id=str(uuid.uuid4()),
            knowledge_base_id=knowledge_base_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
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
        )


# ペルソナと KB の紐付け（1 ペルソナに 1 つ）は U5 から src/models/persona_binding.py の
# PersonaBinding（固定キー KB）が持つ。
