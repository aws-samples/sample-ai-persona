"""PersonaMemoryManager の単体テスト"""

import pytest
from datetime import datetime
from unittest.mock import Mock

from src.managers.persona_memory_manager import (
    PersonaMemoryManager,
    PersonaMemoryManagerError,
)
from src.models.memory import MemoryEntry
from src.models.persona import Persona
from src.services.memory.memory_service import MemoryServiceError


@pytest.fixture
def mock_db():
    mock = Mock()
    mock.get_persona.return_value = Persona.create_new(
        name="テスト",
        age=30,
        occupation="会社員",
        background="テスト背景",
        values=["v1"],
        pain_points=["p1"],
        goals=["g1"],
    )
    return mock


@pytest.fixture
def mock_memory_service():
    mock = Mock()
    mock.is_semantic_enabled = True
    mock.save_knowledge.return_value = "mem-123"
    mock.list_memories.return_value = []
    mock.delete_memory.return_value = True
    return mock


@pytest.fixture
def manager(mock_db, mock_memory_service):
    return PersonaMemoryManager(
        database_service=mock_db, memory_service=mock_memory_service
    )


@pytest.mark.unit
class TestAddKnowledge:
    """add_knowledge のテスト"""

    def test_success(self, manager, mock_memory_service):
        result = manager.add_knowledge("p1", "好きな食べ物", "ラーメン")
        assert result == "mem-123"
        mock_memory_service.save_knowledge.assert_called_once()
        call_kwargs = mock_memory_service.save_knowledge.call_args[1]
        assert call_kwargs["actor_id"] == "p1"
        assert '<topic name="好きな食べ物">ラーメン</topic>' in call_kwargs["content"]

    def test_empty_persona_id(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="ペルソナIDが無効"):
            manager.add_knowledge("", "topic", "content")

    def test_whitespace_persona_id(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="ペルソナIDが無効"):
            manager.add_knowledge("   ", "topic", "content")

    def test_empty_topic_name(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="トピック名"):
            manager.add_knowledge("p1", "", "content")

    def test_empty_topic_content(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="内容を入力"):
            manager.add_knowledge("p1", "topic", "")

    def test_topic_name_too_long(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="100文字以内"):
            manager.add_knowledge("p1", "x" * 101, "content")

    def test_topic_content_too_long(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="10000文字以内"):
            manager.add_knowledge("p1", "topic", "x" * 10001)

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get_persona.return_value = None
        with pytest.raises(PersonaMemoryManagerError, match="ペルソナが見つかりません"):
            manager.add_knowledge("p1", "topic", "content")

    def test_memory_service_disabled(self, mock_db):
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)
        # _memory_service_resolved=True なので service_factory は呼ばれない
        with pytest.raises(PersonaMemoryManagerError, match="長期記憶機能が無効"):
            mgr.add_knowledge("p1", "topic", "content")

    def test_semantic_not_enabled(self, manager, mock_memory_service):
        mock_memory_service.is_semantic_enabled = False
        with pytest.raises(PersonaMemoryManagerError, match="Semantic記憶戦略"):
            manager.add_knowledge("p1", "topic", "content")

    def test_memory_service_error(self, manager, mock_memory_service):
        mock_memory_service.save_knowledge.side_effect = MemoryServiceError("fail")
        with pytest.raises(PersonaMemoryManagerError, match="知識の追加中にエラー"):
            manager.add_knowledge("p1", "topic", "content")


@pytest.mark.unit
class TestGetMemories:
    """get_memories のテスト"""

    def test_success_with_memories(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1, 0, 0, i),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, page, total_pages = manager.get_memories("p1", "summary")
        assert len(memories) == 3
        assert page == 1
        assert total_pages == 1

    def test_filters_by_strategy_type(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="summary content",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            ),
            MemoryEntry(
                id="m2",
                actor_id="p1",
                session_id="s1",
                content='<topic name="知識">内容</topic>',
                metadata={"strategy_type": "semantic"},
                created_at=datetime(2024, 1, 2),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, _, _ = manager.get_memories("p1", "semantic")
        assert len(memories) == 1
        assert memories[0].id == "m2"
        assert memories[0].parsed_topic == {"name": "知識", "content": "内容"}

    def test_pagination(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1, 0, 0, i),
            )
            for i in range(15)
        ]
        mock_memory_service.list_memories.return_value = entries

        memories, page, total_pages = manager.get_memories(
            "p1", "summary", page=2, per_page=10
        )
        assert len(memories) == 5
        assert page == 2
        assert total_pages == 2

    def test_empty_persona_id(self, manager):
        with pytest.raises(PersonaMemoryManagerError, match="ペルソナIDが無効"):
            manager.get_memories("")

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get_persona.return_value = None
        with pytest.raises(PersonaMemoryManagerError, match="ペルソナが見つかりません"):
            manager.get_memories("p1")

    def test_memory_disabled_returns_empty(self, mock_db):
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)
        memories, page, total = mgr.get_memories("p1")
        assert memories == []
        assert page == 1
        assert total == 1


@pytest.mark.unit
class TestDeleteMemory:
    """delete_memory のテスト"""

    def test_success(self, manager, mock_memory_service):
        result = manager.delete_memory("p1", "mem-1")
        assert result is True
        mock_memory_service.delete_memory.assert_called_once_with(
            actor_id="p1", memory_id="mem-1"
        )

    def test_not_found(self, manager, mock_memory_service):
        mock_memory_service.delete_memory.return_value = False
        result = manager.delete_memory("p1", "mem-nonexist")
        assert result is False

    def test_memory_disabled(self, mock_db):
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)
        with pytest.raises(PersonaMemoryManagerError, match="長期記憶機能が無効"):
            mgr.delete_memory("p1", "mem-1")

    def test_connection_error(self, manager, mock_memory_service):
        mock_memory_service.delete_memory.side_effect = ConnectionError("timeout")
        with pytest.raises(PersonaMemoryManagerError, match="接続に失敗"):
            manager.delete_memory("p1", "mem-1")

    def test_service_error(self, manager, mock_memory_service):
        mock_memory_service.delete_memory.side_effect = MemoryServiceError("fail")
        with pytest.raises(PersonaMemoryManagerError, match="記憶の削除中にエラー"):
            manager.delete_memory("p1", "mem-1")


@pytest.mark.unit
class TestDeleteAllMemories:
    """delete_all_memories のテスト"""

    def test_success(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries

        count = manager.delete_all_memories("p1", "summary")
        assert count == 3
        assert mock_memory_service.delete_memory.call_count == 3

    def test_no_matching_memories(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="content",
                metadata={"strategy_type": "semantic"},
                created_at=datetime(2024, 1, 1),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        count = manager.delete_all_memories("p1", "summary")
        assert count == 0

    def test_memory_disabled(self, mock_db):
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)
        with pytest.raises(PersonaMemoryManagerError, match="長期記憶機能が無効"):
            mgr.delete_all_memories("p1")

    def test_partial_failure(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            )
            for i in range(3)
        ]
        mock_memory_service.list_memories.return_value = entries
        mock_memory_service.delete_memory.side_effect = [True, Exception("fail"), True]

        count = manager.delete_all_memories("p1", "summary")
        assert count == 2


@pytest.mark.unit
class TestSafeGetMemories:
    """safe_get_memories のテスト"""

    def test_success(self, manager, mock_memory_service):
        entries = [
            MemoryEntry(
                id="m1",
                actor_id="p1",
                session_id="s1",
                content="content",
                metadata={"strategy_type": "summary"},
                created_at=datetime(2024, 1, 1),
            ),
        ]
        mock_memory_service.list_memories.return_value = entries

        result = manager.safe_get_memories("p1", "summary")
        assert len(result) == 1

    def test_error_returns_empty(self, manager, mock_memory_service):
        mock_memory_service.list_memories.side_effect = Exception("fail")
        result = manager.safe_get_memories("p1")
        assert result == []

    def test_memory_disabled_returns_empty(self, mock_db):
        mgr = PersonaMemoryManager(database_service=mock_db, memory_service=None)
        result = mgr.safe_get_memories("p1")
        assert result == []


@pytest.mark.unit
class TestParseTopicContent:
    """_parse_topic_content のテスト"""

    def test_valid_topic(self, manager):
        content = '<topic name="好きな食べ物">ラーメンが好き</topic>'
        result = manager._parse_topic_content(content)
        assert result == {"name": "好きな食べ物", "content": "ラーメンが好き"}

    def test_multiline_content(self, manager):
        content = '<topic name="趣味">\n読書\nプログラミング\n</topic>'
        result = manager._parse_topic_content(content)
        assert result == {"name": "趣味", "content": "読書\nプログラミング"}

    def test_non_topic_content(self, manager):
        content = "plain text without topic tags"
        result = manager._parse_topic_content(content)
        assert result is None
