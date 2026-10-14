"""
Memory Entry Model
長期記憶エントリのデータモデル
"""

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
    parsed_topic: Optional[Dict[str, Any]] = None  # パース済みトピック情報（表示用）


@dataclass(frozen=True)
class DeleteAllResult:
    """Outcome of deleting every long-term memory of one actor (U5, NFR2.11).

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
