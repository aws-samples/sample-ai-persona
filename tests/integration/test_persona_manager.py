"""
Integration tests for Persona Manager.
Tests the complete persona management workflow including AI service and database integration.
"""

import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.persona import Persona
from src.services.ai_service import AIService, AIServiceError
from src.services.database_service import DatabaseError


class TestPersonaManagerIntegration:
    """Integration tests for PersonaManager class."""

    @pytest.fixture
    def mock_database_service(self):
        """Create a mock database service for testing."""
        personas_storage = {}
        mock_db = Mock()

        # Dynamic mock implementations
        def mock_save_persona(persona):
            personas_storage[persona.id] = persona
            return persona.id

        def mock_get_persona(persona_id):
            return personas_storage.get(persona_id)

        def mock_get_all_personas():
            return list(personas_storage.values())

        def mock_update_persona(persona):
            if persona.id in personas_storage:
                personas_storage[persona.id] = persona
                return True
            return False

        def mock_delete_persona(persona_id):
            if persona_id in personas_storage:
                del personas_storage[persona_id]
                return True
            return False

        def mock_persona_exists(persona_id):
            return persona_id in personas_storage

        def mock_get_persona_count():
            return len(personas_storage)

        def mock_get_personas_by_name(name_pattern):
            return [
                p
                for p in personas_storage.values()
                if name_pattern.lower() in p.name.lower()
            ]

        def mock_get_personas_by_occupation(occupation_pattern):
            return [
                p
                for p in personas_storage.values()
                if occupation_pattern.lower() in p.occupation.lower()
            ]

        mock_db.save_persona.side_effect = mock_save_persona
        mock_db.get_persona.side_effect = mock_get_persona
        mock_db.get_all_personas.side_effect = mock_get_all_personas
        mock_db.update_persona.side_effect = mock_update_persona
        mock_db.delete_persona.side_effect = mock_delete_persona
        mock_db.persona_exists.side_effect = mock_persona_exists
        mock_db.get_persona_count.side_effect = mock_get_persona_count
        mock_db.get_personas_by_name.side_effect = mock_get_personas_by_name
        mock_db.get_personas_by_occupation.side_effect = mock_get_personas_by_occupation

        return mock_db

    @pytest.fixture
    def database_service(self, mock_database_service):
        """Alias for mock_database_service for backward compatibility."""
        return mock_database_service

    @pytest.fixture
    def mock_ai_service(self):
        """Create a mock AI service for testing."""
        mock_service = Mock(spec=AIService)
        return mock_service

    @pytest.fixture
    def persona_manager(self, mock_ai_service, database_service):
        """Create a persona manager with mocked AI service and real database."""
        return PersonaManager(
            ai_service=mock_ai_service, database_service=database_service
        )

    @pytest.fixture
    def sample_interview_text(self):
        """Sample interview text for testing."""
        return """
        インタビュー対象者: 田中花子さん（35歳、会社員）
        
        Q: 普段の生活について教えてください。
        A: 東京都内で一人暮らしをしています。マーケティング部で働いていて、
        新商品の企画や宣伝活動に携わっています。仕事は忙しいですが、
        やりがいを感じています。
        
        Q: 大切にしていることは何ですか？
        A: 効率性を重視しています。限られた時間の中で最大の成果を出したいと
        思っています。また、新しいことに挑戦することも大切にしています。
        
        Q: 現在抱えている課題はありますか？
        A: 時間管理が難しいです。仕事が忙しくて、プライベートの時間が
        なかなか取れません。また、情報が多すぎて、何を選択すべきか
        迷うことが多いです。
        
        Q: 将来の目標について教えてください。
        A: キャリアアップを目指しています。マネージャーになって、
        チームを率いてみたいです。また、ワークライフバランスを
        改善したいと思っています。
        """

    @pytest.fixture
    def sample_persona_data(self):
        """Sample persona data for testing."""
        return {
            "name": "田中 花子",
            "age": 35,
            "occupation": "会社員（マーケティング部）",
            "background": "東京都在住。大学卒業後、現在の会社に就職し10年目。一人暮らしで、仕事とプライベートのバランスを重視している。",
            "values": [
                "効率性を重視する",
                "新しいことへの挑戦を大切にする",
                "人とのつながりを大事にする",
            ],
            "pain_points": [
                "時間管理が難しい",
                "情報過多で選択に迷う",
                "仕事のストレスが多い",
            ],
            "goals": [
                "キャリアアップを目指す",
                "ワークライフバランスを改善する",
                "新しいスキルを身につける",
            ],
        }

    @patch("src.managers.persona_manager.service_factory")
    def test_persona_manager_initialization(self, mock_sf, mock_database_service):
        """Test persona manager initialization."""
        mock_sf.get_ai_service.return_value = Mock(spec=AIService)
        mock_sf.get_database_service.return_value = mock_database_service

        # Test with default services
        manager = PersonaManager()
        assert manager.ai_service is not None
        assert manager.database_service is not None

        # Test with custom services
        ai_service = Mock(spec=AIService)
        manager = PersonaManager(
            ai_service=ai_service, database_service=mock_database_service
        )
        assert manager.ai_service == ai_service
        assert manager.database_service == mock_database_service

    def test_generate_persona_from_interview_success(
        self,
        persona_manager,
        mock_ai_service,
        sample_interview_text,
        sample_persona_data,
    ):
        """Test successful persona generation from interview text."""
        # Setup mock AI service to return a valid persona
        expected_persona = Persona.create_new(**sample_persona_data)
        mock_ai_service.generate_persona.return_value = expected_persona

        # Generate persona
        result = persona_manager.generate_persona_from_interview(sample_interview_text)

        # Verify AI service was called correctly
        mock_ai_service.generate_persona.assert_called_once_with(sample_interview_text)

        # Verify result
        assert result == expected_persona
        assert result.name == "田中 花子"
        assert result.age == 35
        assert len(result.values) == 3
        assert len(result.pain_points) == 3
        assert len(result.goals) == 3

    def test_generate_persona_from_interview_empty_text(self, persona_manager):
        """Test persona generation with empty interview text."""
        with pytest.raises(PersonaManagerError, match="インタビューテキストが空です"):
            persona_manager.generate_persona_from_interview("")

        with pytest.raises(PersonaManagerError, match="インタビューテキストが空です"):
            persona_manager.generate_persona_from_interview("   ")

    def test_generate_persona_from_interview_short_text(self, persona_manager):
        """Test persona generation with too short interview text."""
        short_text = "短いテキスト"
        with pytest.raises(
            PersonaManagerError, match="インタビューテキストが短すぎます"
        ):
            persona_manager.generate_persona_from_interview(short_text)

    def test_generate_persona_from_interview_long_text(self, persona_manager):
        """Test persona generation with too long interview text."""
        long_text = "a" * 50001  # Exceed 50KB limit
        with pytest.raises(
            PersonaManagerError, match="インタビューテキストが長すぎます"
        ):
            persona_manager.generate_persona_from_interview(long_text)

    def test_generate_persona_ai_service_error(
        self, persona_manager, mock_ai_service, sample_interview_text
    ):
        """Test persona generation when AI service fails."""
        # Setup mock to raise AIServiceError
        mock_ai_service.generate_persona.side_effect = AIServiceError(
            "AI service failed"
        )

        with pytest.raises(
            PersonaManagerError, match="AI service error during persona generation"
        ):
            persona_manager.generate_persona_from_interview(sample_interview_text)

    def test_save_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona saving."""
        persona = Persona.create_new(**sample_persona_data)

        # Save persona
        result_id = persona_manager.save_persona(persona)

        # Verify result
        assert result_id == persona.id

        # Verify persona was saved to database
        saved_persona = persona_manager.get_persona(persona.id)
        assert saved_persona is not None
        assert saved_persona.name == persona.name
        assert saved_persona.age == persona.age

    def test_save_persona_invalid_data(self, persona_manager):
        """Test saving persona with invalid data."""
        # Test with None persona
        with pytest.raises(PersonaManagerError, match="ペルソナオブジェクトが無効です"):
            persona_manager.save_persona(None)

        # Test with invalid persona (missing name)
        invalid_persona = Persona.create_new(
            name="",  # Empty name
            age=30,
            occupation="Test",
            background="Test background",
            values=["value1"],
            pain_points=["pain1"],
            goals=["goal1"],
        )
        with pytest.raises(PersonaManagerError, match="ペルソナ名が設定されていません"):
            persona_manager.save_persona(invalid_persona)

    def test_get_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona retrieval."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Retrieve persona
        result = persona_manager.get_persona(persona.id)

        # Verify result
        assert result is not None
        assert result.id == persona.id
        assert result.name == persona.name
        assert result.age == persona.age

    def test_get_persona_not_found(self, persona_manager):
        """Test persona retrieval when persona doesn't exist."""
        result = persona_manager.get_persona("non-existent-id")
        assert result is None

    def test_get_persona_invalid_id(self, persona_manager):
        """Test persona retrieval with invalid ID."""
        with pytest.raises(PersonaManagerError, match="ペルソナIDが無効です"):
            persona_manager.get_persona("")

        with pytest.raises(PersonaManagerError, match="ペルソナIDが無効です"):
            persona_manager.get_persona("   ")

    def test_get_all_personas(self, persona_manager, sample_persona_data):
        """Test retrieving all personas."""
        # Initially should be empty
        personas = persona_manager.get_all_personas()
        assert len(personas) == 0

        # Save multiple personas
        persona1 = Persona.create_new(**sample_persona_data)
        persona2_data = sample_persona_data.copy()
        persona2_data["name"] = "佐藤 太郎"
        persona2_data["age"] = 28
        persona2 = Persona.create_new(**persona2_data)

        persona_manager.save_persona(persona1)
        persona_manager.save_persona(persona2)

        # Retrieve all personas
        personas = persona_manager.get_all_personas()
        assert len(personas) == 2

        # Verify personas are ordered by creation date (newest first)
        persona_names = [p.name for p in personas]
        assert "田中 花子" in persona_names
        assert "佐藤 太郎" in persona_names

    def test_update_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona update."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Update persona
        updated_persona = persona.update(
            name="田中 花子（更新済み）", age=36, occupation="シニアマーケター"
        )

        result = persona_manager.update_persona(updated_persona)

        # Verify update was successful
        assert result is True

        # Verify updated data in database
        saved_persona = persona_manager.get_persona(persona.id)
        assert saved_persona.name == "田中 花子（更新済み）"
        assert saved_persona.age == 36
        assert saved_persona.occupation == "シニアマーケター"
        assert saved_persona.updated_at > saved_persona.created_at

    def test_update_persona_not_found(self, persona_manager, sample_persona_data):
        """Test updating non-existent persona."""
        persona = Persona.create_new(**sample_persona_data)
        # Don't save the persona, so it won't exist in database

        result = persona_manager.update_persona(persona)
        assert result is False

    def test_delete_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona deletion."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Verify persona exists
        assert persona_manager.get_persona(persona.id) is not None

        # Delete persona
        result = persona_manager.delete_persona(persona.id)

        # Verify deletion was successful
        assert result is True
        assert persona_manager.get_persona(persona.id) is None

    def test_delete_persona_not_found(self, persona_manager):
        """Test deleting non-existent persona."""
        result = persona_manager.delete_persona("non-existent-id")
        assert result is False

    def test_edit_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona editing."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Edit persona
        result = persona_manager.edit_persona(
            persona.id,
            name="田中 花子（編集済み）",
            age=37,
            values=["効率性", "革新性", "協調性"],
        )

        # Verify edit was successful
        assert result is not None
        assert result.name == "田中 花子（編集済み）"
        assert result.age == 37
        assert result.values == ["効率性", "革新性", "協調性"]
        # Other fields should remain unchanged
        assert result.occupation == sample_persona_data["occupation"]
        assert result.background == sample_persona_data["background"]

    def test_edit_persona_not_found(self, persona_manager):
        """Test editing non-existent persona."""
        result = persona_manager.edit_persona("non-existent-id", name="New Name")
        assert result is None

    def test_get_persona_count(self, persona_manager, sample_persona_data):
        """Test getting persona count."""
        # Initially should be 0
        assert persona_manager.get_persona_count() == 0

        # Save personas and check count
        persona1 = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona1)
        assert persona_manager.get_persona_count() == 1

        persona2_data = sample_persona_data.copy()
        persona2_data["name"] = "佐藤 太郎"
        persona2 = Persona.create_new(**persona2_data)
        persona_manager.save_persona(persona2)
        assert persona_manager.get_persona_count() == 2

    def test_persona_exists(self, persona_manager, sample_persona_data):
        """Test checking persona existence."""
        persona = Persona.create_new(**sample_persona_data)

        # Should not exist initially
        assert persona_manager.persona_exists(persona.id) is False

        # Save persona
        persona_manager.save_persona(persona)

        # Should exist now
        assert persona_manager.persona_exists(persona.id) is True

        # Test with invalid ID
        assert persona_manager.persona_exists("") is False
        assert persona_manager.persona_exists("   ") is False

    def test_search_personas(self, persona_manager, sample_persona_data):
        """Test searching personas."""
        # Save multiple personas
        persona1 = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona1)

        persona2_data = sample_persona_data.copy()
        persona2_data["name"] = "佐藤 太郎"
        persona2_data["occupation"] = "エンジニア"
        persona2_data["background"] = "大阪在住のソフトウェアエンジニア"
        persona2 = Persona.create_new(**persona2_data)
        persona_manager.save_persona(persona2)

        # Search by name
        results = persona_manager.search_personas("田中")
        assert len(results) == 1
        assert results[0].name == "田中 花子"

        # Search by occupation
        results = persona_manager.search_personas("エンジニア")
        assert len(results) == 1
        assert results[0].name == "佐藤 太郎"

        # Search by background
        results = persona_manager.search_personas("大阪")
        assert len(results) == 1
        assert results[0].name == "佐藤 太郎"

        # Search with no matches
        results = persona_manager.search_personas("存在しない")
        assert len(results) == 0

        # Search with empty query
        results = persona_manager.search_personas("")
        assert len(results) == 0

    def test_complete_persona_workflow(
        self,
        persona_manager,
        mock_ai_service,
        sample_interview_text,
        sample_persona_data,
    ):
        """Test complete persona management workflow."""
        # Setup mock AI service
        generated_persona = Persona.create_new(**sample_persona_data)
        mock_ai_service.generate_persona.return_value = generated_persona

        # Step 1: Generate persona from interview
        persona = persona_manager.generate_persona_from_interview(sample_interview_text)
        assert persona.name == "田中 花子"

        # Step 2: Save persona
        persona_id = persona_manager.save_persona(persona)
        assert persona_id == persona.id

        # Step 3: Retrieve and verify
        saved_persona = persona_manager.get_persona(persona_id)
        assert saved_persona is not None
        assert saved_persona.name == persona.name

        # Step 4: Edit persona
        edited_persona = persona_manager.edit_persona(
            persona_id, name="田中 花子（編集済み）", age=36
        )
        assert edited_persona is not None
        assert edited_persona.name == "田中 花子（編集済み）"
        assert edited_persona.age == 36

        # Step 5: Verify in list
        all_personas = persona_manager.get_all_personas()
        assert len(all_personas) == 1
        assert all_personas[0].name == "田中 花子（編集済み）"

        # Step 6: Search persona
        search_results = persona_manager.search_personas("田中")
        assert len(search_results) == 1
        assert search_results[0].name == "田中 花子（編集済み）"

        # Step 7: Check existence
        assert persona_manager.persona_exists(persona_id) is True
        assert persona_manager.get_persona_count() == 1

        # Step 8: Delete persona
        delete_result = persona_manager.delete_persona(persona_id)
        assert delete_result is True
        assert persona_manager.get_persona(persona_id) is None
        assert persona_manager.get_persona_count() == 0

    def test_database_error_handling(self, mock_ai_service, mock_database_service):
        """Test handling of database errors."""
        # Create a mock database service that raises an error
        error_db_service = Mock()
        error_db_service.save_persona.side_effect = DatabaseError(
            "Database save failed"
        )

        manager = PersonaManager(
            ai_service=mock_ai_service, database_service=error_db_service
        )

        # Test that database errors are properly handled
        persona = Persona.create_new(
            name="Test User",
            age=30,
            occupation="Test",
            background="Test background",
            values=["test"],
            pain_points=["test"],
            goals=["test"],
        )

        with pytest.raises(PersonaManagerError, match="Database"):
            manager.save_persona(persona)

    def test_validation_edge_cases(self, persona_manager):
        """Test validation with edge cases."""
        # Test persona with maximum valid age
        valid_persona = Persona.create_new(
            name="テスト ユーザー",
            age=150,  # Maximum valid age
            occupation="テスト職業",
            background="テスト背景",
            values=["価値観1"],
            pain_points=["課題1"],
            goals=["目標1"],
        )

        # Should not raise exception
        persona_id = persona_manager.save_persona(valid_persona)
        assert persona_id == valid_persona.id

        # Test persona with invalid age
        invalid_persona = Persona.create_new(
            name="テスト ユーザー",
            age=151,  # Exceeds maximum
            occupation="テスト職業",
            background="テスト背景",
            values=["価値観1"],
            pain_points=["課題1"],
            goals=["目標1"],
        )

        with pytest.raises(
            PersonaManagerError, match="年齢は0から150の範囲で設定してください"
        ):
            persona_manager.save_persona(invalid_persona)
