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

    def test_to_dict(self):
        entry = self._make_entry(metadata={"key": "val"}, relevance_score=0.95)
        data = entry.to_dict()
        assert data["id"] == "mem-1"
        assert data["metadata"] == {"key": "val"}
        assert data["relevance_score"] == 0.95
        datetime.fromisoformat(data["created_at"])

    def test_to_dict_from_dict_roundtrip(self):
        entry = self._make_entry(metadata={"k": "v"}, relevance_score=0.8)
        restored = MemoryEntry.from_dict(entry.to_dict())
        assert restored.id == entry.id
        assert restored.actor_id == entry.actor_id
        assert restored.session_id == entry.session_id
        assert restored.content == entry.content
        assert restored.metadata == entry.metadata
        assert restored.relevance_score == entry.relevance_score

    def test_to_json_from_json_roundtrip(self):
        entry = self._make_entry()
        json_str = entry.to_json()
        restored = MemoryEntry.from_json(json_str)
        assert restored.id == entry.id
        assert restored.content == entry.content

    def test_from_dict_string_created_at(self):
        data = {
            "id": "m1",
            "actor_id": "a1",
            "session_id": "s1",
            "content": "test",
            "created_at": "2025-01-01T00:00:00",
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.created_at == datetime(2025, 1, 1)

    def test_from_dict_missing_created_at(self):
        data = {
            "id": "m1",
            "actor_id": "a1",
            "session_id": "s1",
            "content": "test",
        }
        entry = MemoryEntry.from_dict(data)
        assert isinstance(entry.created_at, datetime)

    def test_from_dict_missing_optional_fields(self):
        data = {
            "id": "m1",
            "actor_id": "a1",
            "session_id": "s1",
            "content": "test",
            "created_at": datetime.now().isoformat(),
        }
        entry = MemoryEntry.from_dict(data)
        assert entry.metadata == {}
        assert entry.relevance_score is None
