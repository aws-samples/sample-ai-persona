"""MemoryEntry モデルの単体テスト"""
from datetime import datetime

from src.models.memory import MemoryEntry


class TestMemoryEntry:
    def _make_entry(self, **kwargs):
        defaults = {
            "id": "mem-1",
            "actor_id": "persona-1",
            "session_id": "session-1",
            "content": "テスト記憶内容",
        }
        defaults.update(kwargs)
        return MemoryEntry(**defaults)

    def test_basic_creation(self):
        entry = self._make_entry()
        assert entry.id == "mem-1"
        assert entry.content == "テスト記憶内容"
        assert entry.metadata == {}
        assert entry.relevance_score is None
