"""
Knowledge Base data model for Bedrock Knowledge Base integration.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any
import uuid


@dataclass
class KnowledgeBase:
    """登録済みBedrock Knowledge Baseの情報"""

    id: str
    knowledge_base_id: str  # Bedrock KB ID (e.g., "KB12345678")
    name: str
    description: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create_new(cls, knowledge_base_id: str, name: str, description: str = "") -> "KnowledgeBase":
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
        )


@dataclass
class PersonaKBBinding:
    """ペルソナとナレッジベースの紐付け情報（1ペルソナ:1KB）"""

    id: str
    persona_id: str
    kb_id: str  # KnowledgeBase.id（内部ID）
    metadata_filters: Dict[str, str]  # メタデータフィルタ（キー=値ペア）
    created_at: datetime

    @classmethod
    def create_new(
        cls, persona_id: str, kb_id: str, metadata_filters: Dict[str, str] | None = None
    ) -> "PersonaKBBinding":
        return cls(
            id=str(uuid.uuid4()),
            persona_id=persona_id,
            kb_id=kb_id,
            metadata_filters=metadata_filters or {},
            created_at=datetime.now(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "persona_id": self.persona_id,
            "kb_id": self.kb_id,
            "metadata_filters": self.metadata_filters,
            "created_at": self.created_at.isoformat()
            if isinstance(self.created_at, datetime)
            else self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PersonaKBBinding":
        return cls(
            id=data["id"],
            persona_id=data["persona_id"],
            kb_id=data["kb_id"],
            metadata_filters=data.get("metadata_filters", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
        )
