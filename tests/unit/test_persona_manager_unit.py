"""PersonaManager の単体テスト（欠落メソッド分）"""
import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.persona import Persona


@pytest.fixture
def mock_db():
    mock = Mock()
    mock.get_persona.return_value = Persona.create_new(
        name="テスト", age=30, occupation="会社員",
        background="テスト背景", values=["v1"], pain_points=["p1"], goals=["g1"],
    )
    return mock


@pytest.fixture
def manager(mock_db):
    return PersonaManager(ai_service=Mock(), database_service=mock_db)


class TestAddPersonaKnowledge:
    """add_persona_knowledge のテスト"""

    def test_empty_persona_id(self, manager):
        with pytest.raises(PersonaManagerError, match="ペルソナIDが無効"):
            manager.add_persona_knowledge("", "topic", "content")

    def test_empty_topic_name(self, manager):
        with pytest.raises(PersonaManagerError, match="トピック名"):
            manager.add_persona_knowledge("p1", "", "content")

    def test_empty_content(self, manager):
        with pytest.raises(PersonaManagerError, match="内容を入力"):
            manager.add_persona_knowledge("p1", "topic", "")

    def test_topic_name_too_long(self, manager):
        with pytest.raises(PersonaManagerError, match="100文字以内"):
            manager.add_persona_knowledge("p1", "x" * 101, "content")

    def test_content_too_long(self, manager):
        with pytest.raises(PersonaManagerError, match="10000文字以内"):
            manager.add_persona_knowledge("p1", "topic", "x" * 10001)

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get_persona.return_value = None
        with pytest.raises(PersonaManagerError, match="ペルソナが見つかりません"):
            manager.add_persona_knowledge("p1", "topic", "content")

    @patch("src.managers.persona_manager.service_factory")
    def test_memory_disabled(self, mock_sf, manager):
        mock_sf.get_memory_service.return_value = None
        with pytest.raises(PersonaManagerError, match="長期記憶機能が無効"):
            manager.add_persona_knowledge("p1", "topic", "content")

    @patch("src.managers.persona_manager.service_factory")
    def test_success(self, mock_sf, manager):
        mock_memory = Mock()
        mock_memory._semantic_strategy = Mock()
        mock_memory._semantic_strategy.save_directly_to_ltm.return_value = "mem-id"
        mock_sf.get_memory_service.return_value = mock_memory

        result = manager.add_persona_knowledge("p1", "好きな食べ物", "ラーメン")
        assert result == "mem-id"


class TestGetPersonaMemories:
    """get_persona_memories のテスト"""

    def test_empty_persona_id(self, manager):
        with pytest.raises(PersonaManagerError, match="ペルソナIDが無効"):
            manager.get_persona_memories("")

    def test_persona_not_found(self, manager, mock_db):
        mock_db.get_persona.return_value = None
        with pytest.raises(PersonaManagerError, match="ペルソナが見つかりません"):
            manager.get_persona_memories("p1")

    @patch("src.managers.persona_manager.service_factory")
    def test_memory_disabled_returns_empty(self, mock_sf, manager):
        mock_sf.get_memory_service.return_value = None
        memories, page, total = manager.get_persona_memories("p1")
        assert memories == []
        assert page == 1
        assert total == 1

    @patch("src.managers.persona_manager.service_factory")
    def test_success_with_memories(self, mock_sf, manager):
        from src.models.memory import MemoryEntry
        from datetime import datetime
        mock_memory = Mock()
        entries = [
            MemoryEntry(
                id=f"m{i}", actor_id="p1", session_id="s1",
                content=f"content {i}",
                metadata={"strategy_type": "summary"},
                created_at=datetime.now(),
            )
            for i in range(3)
        ]
        mock_memory.list_memories.return_value = entries
        mock_sf.get_memory_service.return_value = mock_memory

        memories, page, total = manager.get_persona_memories("p1", "summary")
        assert len(memories) == 3
        assert page == 1


class TestGeneratePersonasFromMarketReport:
    """generate_personas_from_market_report のテスト"""

    def test_invalid_persona_count_zero(self, manager):
        with pytest.raises(PersonaManagerError, match="1-10の範囲"):
            manager.generate_personas_from_market_report(b"content", "report.txt", 0)

    def test_invalid_persona_count_over(self, manager):
        with pytest.raises(PersonaManagerError, match="1-10の範囲"):
            manager.generate_personas_from_market_report(b"content", "report.txt", 11)

    @patch("src.managers.file_manager.FileManager")
    def test_report_too_short(self, mock_fm_cls, manager):
        mock_fm = Mock()
        mock_fm.extract_text_from_file.return_value = "短い"
        mock_fm_cls.return_value = mock_fm

        with pytest.raises(PersonaManagerError, match="短すぎます"):
            manager.generate_personas_from_market_report(b"content", "report.txt", 3)
