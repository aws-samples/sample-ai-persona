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

