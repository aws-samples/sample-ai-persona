"""PersonaManager の単体テスト（欠落メソッド分）"""

import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.persona import Persona


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
                id=f"m{i}",
                actor_id="p1",
                session_id="s1",
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


class TestGeneratePersonaFromInterview:
    """generate_persona_from_interview のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_empty_text_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="空です"):
            manager.generate_persona_from_interview("")

    def test_short_text_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="短すぎます"):
            manager.generate_persona_from_interview("短い文章")

    def test_too_long_text_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="長すぎます"):
            manager.generate_persona_from_interview("x" * 50001)

    def test_ai_service_error(self, manager):
        from src.services.ai_service import AIServiceError

        manager.ai_service.generate_persona.side_effect = AIServiceError("AI error")
        with pytest.raises(PersonaManagerError, match="AI service error"):
            manager.generate_persona_from_interview("x" * 100)

    def test_success(self, manager):
        persona = Persona.create_new(
            name="生成ペルソナ",
            age=30,
            occupation="会社員",
            background="テスト背景テスト背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        manager.ai_service.generate_persona.return_value = persona
        result = manager.generate_persona_from_interview("x" * 100)
        assert result.name == "生成ペルソナ"


class TestGeneratePersonas:
    """generate_personas 統合生成のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_invalid_count_zero(self, manager):
        with pytest.raises(PersonaManagerError, match="1-10の範囲"):
            manager.generate_personas([], "interview", 0)

    def test_invalid_count_over_10(self, manager):
        with pytest.raises(PersonaManagerError, match="1-10の範囲"):
            manager.generate_personas([], "interview", 11)

    def test_no_files_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="ファイルが選択されていません"):
            manager.generate_personas([], "interview", 3)

    @patch("src.services.agent_service.AgentService")
    @patch("src.managers.file_manager.FileManager")
    def test_success_text_file(self, mock_fm_cls, mock_as_cls, manager):
        mock_fm = Mock()
        mock_fm.extract_text_from_file.return_value = "インタビューテキスト"
        mock_fm_cls.return_value = mock_fm

        persona = Persona.create_new(
            name="P1",
            age=25,
            occupation="エンジニア",
            background="テスト",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        mock_as = Mock()
        mock_as.generate_personas_with_agent.return_value = (
            [persona],
            [{"role": "assistant", "content": "log"}],
        )
        mock_as_cls.return_value = mock_as

        personas, logs = manager.generate_personas(
            [(b"text data", "interview.txt")], "interview", 1
        )
        assert len(personas) == 1
        assert personas[0].name == "P1"

    def test_dwh_empty_angle_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="分析の切り口"):
            manager.generate_personas([], "dwh", 3, data_description="")


class TestSavePersona:
    """save_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_none_persona_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="無効"):
            manager.save_persona(None)

    def test_success(self, manager):
        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="会社員",
            background="テスト背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        manager.database_service.save_persona.return_value = persona.id
        result = manager.save_persona(persona)
        assert result == persona.id
        manager.database_service.save_persona.assert_called_once()

    def test_db_error(self, manager):
        from src.services.database_service import DatabaseError

        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="会社員",
            background="テスト背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        manager.database_service.save_persona.side_effect = DatabaseError("DB down")
        with pytest.raises(PersonaManagerError, match="Database error"):
            manager.save_persona(persona)


class TestGetPersona:
    """get_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_empty_id_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="IDが無効"):
            manager.get_persona("")

    def test_found(self, manager):
        persona = Persona.create_new(
            name="X",
            age=20,
            occupation="Y",
            background="Z",
            values=[],
            pain_points=[],
            goals=[],
        )
        manager.database_service.get_persona.return_value = persona
        result = manager.get_persona("p1")
        assert result == persona

    def test_not_found(self, manager):
        manager.database_service.get_persona.return_value = None
        result = manager.get_persona("p1")
        assert result is None

    def test_db_error(self, manager):
        from src.services.database_service import DatabaseError

        manager.database_service.get_persona.side_effect = DatabaseError("fail")
        with pytest.raises(PersonaManagerError):
            manager.get_persona("p1")


class TestGetAllPersonas:
    """get_all_personas のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_success(self, manager):
        manager.database_service.get_all_personas.return_value = (["p1", "p2"], None)
        personas, cursor = manager.get_all_personas()
        assert personas == ["p1", "p2"]
        assert cursor is None

    def test_with_cursor(self, manager):
        manager.database_service.get_all_personas.return_value = (["p3"], {"id": "x"})
        personas, cursor = manager.get_all_personas(cursor={"id": "prev"})
        assert cursor == {"id": "x"}

    def test_db_error(self, manager):
        from src.services.database_service import DatabaseError

        manager.database_service.get_all_personas.side_effect = DatabaseError("fail")
        with pytest.raises(PersonaManagerError):
            manager.get_all_personas()


class TestEditPersona:
    """edit_persona のテスト"""

    @pytest.fixture
    def manager(self):
        mock_db = Mock()
        persona = Persona.create_new(
            name="旧名前",
            age=30,
            occupation="旧職業",
            background="旧背景テスト",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        mock_db.get_persona.return_value = persona
        mock_db.update_persona.return_value = True
        return PersonaManager(ai_service=Mock(), database_service=mock_db)

    def test_empty_id_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="IDが無効"):
            manager.edit_persona("")

    def test_persona_not_found(self, manager):
        manager.database_service.get_persona.return_value = None
        result = manager.edit_persona("p1", name="新名前")
        assert result is None

    def test_success(self, manager):
        result = manager.edit_persona("p1", name="新名前")
        assert result is not None
        assert result.name == "新名前"
        manager.database_service.update_persona.assert_called_once()

    def test_update_failure_raises(self, manager):
        manager.database_service.update_persona.return_value = False
        with pytest.raises(PersonaManagerError, match="更新に失敗"):
            manager.edit_persona("p1", name="新名前")


class TestDeletePersona:
    """delete_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_empty_id_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="IDが無効"):
            manager.delete_persona("")

    def test_success(self, manager):
        manager.database_service.delete_persona.return_value = True
        assert manager.delete_persona("p1") is True

    def test_not_found(self, manager):
        manager.database_service.delete_persona.return_value = False
        assert manager.delete_persona("p1") is False

    def test_db_error(self, manager):
        from src.services.database_service import DatabaseError

        manager.database_service.delete_persona.side_effect = DatabaseError("fail")
        with pytest.raises(PersonaManagerError):
            manager.delete_persona("p1")


class TestSearchPersonas:
    """search_personas のテスト"""

    @pytest.fixture
    def manager(self):
        mock_db = Mock()
        personas = [
            Persona.create_new(
                name="田中花子",
                age=30,
                occupation="マーケター",
                background="東京在住",
                values=[],
                pain_points=[],
                goals=[],
            ),
            Persona.create_new(
                name="佐藤太郎",
                age=40,
                occupation="エンジニア",
                background="大阪在住",
                values=[],
                pain_points=[],
                goals=[],
            ),
        ]
        mock_db.get_all_personas.return_value = (personas, None)
        return PersonaManager(ai_service=Mock(), database_service=mock_db)

    def test_empty_query_returns_empty(self, manager):
        assert manager.search_personas("") == []

    def test_search_by_name(self, manager):
        result = manager.search_personas("田中")
        assert len(result) == 1
        assert result[0].name == "田中花子"

    def test_search_by_occupation(self, manager):
        result = manager.search_personas("エンジニア")
        assert len(result) == 1
        assert result[0].name == "佐藤太郎"

    def test_search_no_match(self, manager):
        result = manager.search_personas("存在しない")
        assert result == []


class TestPersonaCount:
    """get_persona_count / persona_exists のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(ai_service=Mock(), database_service=Mock())

    def test_count(self, manager):
        manager.database_service.get_persona_count.return_value = 5
        assert manager.get_persona_count() == 5

    def test_exists_true(self, manager):
        persona = Persona.create_new(
            name="X",
            age=20,
            occupation="Y",
            background="Z",
            values=[],
            pain_points=[],
            goals=[],
        )
        manager.database_service.get_persona.return_value = persona
        assert manager.persona_exists("p1") is True

    def test_exists_false(self, manager):
        manager.database_service.get_persona.return_value = None
        assert manager.persona_exists("p1") is False

    def test_exists_empty_id(self, manager):
        assert manager.persona_exists("") is False
