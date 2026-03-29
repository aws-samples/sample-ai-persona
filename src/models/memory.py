"""
Memory Entry Model
長期記憶エントリのデータモデル
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class MemoryEntry:
    """記憶エントリのデータモデル"""

    id: str
    actor_id: str  # ペルソナID
    session_id: str  # 議論ID
    content: str  # 記憶の内容
    metadata: Dict[str, Any] = field(default_factory=dict)  # 追加メタデータ
    created_at: datetime = field(default_factory=datetime.now)
    relevance_score: Optional[float] = None  # 検索時の関連度スコア
    topic: Optional[Dict[str, Any]] = None  # パース済みトピック情報（表示用）

    def to_dict(self) -> Dict[str, Any]:
        """辞書に変換"""
        return {
            "id": self.id,
            "actor_id": self.actor_id,
            "session_id": self.session_id,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "relevance_score": self.relevance_score,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        """辞書から作成"""
        # created_atが文字列の場合はdatetimeに変換
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now()

        return cls(
            id=data["id"],
            actor_id=data["actor_id"],
            session_id=data["session_id"],
            content=data["content"],
            metadata=data.get("metadata", {}),
            created_at=created_at,
            relevance_score=data.get("relevance_score"),
        )

    def to_json(self) -> str:
        """JSON文字列に変換"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "MemoryEntry":
        """JSON文字列から作成"""
        data = json.loads(json_str)
        return cls.from_dict(data)
