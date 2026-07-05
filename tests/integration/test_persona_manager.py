"""
Integration tests for Persona Manager.
Tests the complete persona management workflow including AI service and database integration.
"""

import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.persona import Persona
from src.services.ai_service import AIService
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

        def mock_get_all_personas(limit=20, cursor=None, search_all=False):
            return list(personas_storage.values()), None

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
        """Create a mock AI service for testing (kept for backward compat)."""
        mock_service = Mock(spec=AIService)
        return mock_service

    @pytest.fixture
    def persona_manager(self, database_service):
        """Create a persona manager with real database."""
        return PersonaManager(database_service=database_service)

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
        mock_sf.get_database_service.return_value = mock_database_service

        # Test with default services
        manager = PersonaManager()
        assert manager.database_service is not None

        # Test with custom services
        manager = PersonaManager(database_service=mock_database_service)
        assert manager.database_service == mock_database_service

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

    def test_save_persona_city_too_long(self, persona_manager, sample_persona_data):
        """居住都市が100文字超だと拒否される。"""
        persona = Persona.create_new(**sample_persona_data, city="あ" * 101)
        with pytest.raises(PersonaManagerError, match="居住都市は100文字以内"):
            persona_manager.save_persona(persona)

    def test_save_persona_tag_with_comma_rejected(
        self, persona_manager, sample_persona_data
    ):
        """タグにカンマが含まれると拒否される（フィルタ区切りの破損防止）。"""
        persona = Persona.create_new(**sample_persona_data, tags=["B2B, enterprise"])
        with pytest.raises(PersonaManagerError, match="タグにカンマ"):
            persona_manager.save_persona(persona)

    def test_save_persona_tag_too_long(self, persona_manager, sample_persona_data):
        """タグが50文字超だと拒否される。"""
        persona = Persona.create_new(**sample_persona_data, tags=["あ" * 51])
        with pytest.raises(PersonaManagerError, match="タグは1個あたり50文字以内"):
            persona_manager.save_persona(persona)

    def test_save_persona_valid_demographics(
        self, persona_manager, sample_persona_data
    ):
        """正常な city / tags は保存できる。"""
        persona = Persona.create_new(
            **sample_persona_data,
            city="東京都",
            tags=["VIP", "リピーター"],
        )
        saved_id = persona_manager.save_persona(persona)
        assert saved_id == persona.id

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
        personas, _ = persona_manager.get_all_personas()
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
        personas, _ = persona_manager.get_all_personas()
        assert len(personas) == 2

        # Verify personas are ordered by creation date (newest first)
        persona_names = [p.name for p in personas]
        assert "田中 花子" in persona_names
        assert "佐藤 太郎" in persona_names

    def test_update_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona update via update_persona (partial update)."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Update persona using partial update interface
        result = persona_manager.update_persona(
            persona.id,
            name="田中 花子（更新済み）",
            age=36,
            occupation="シニアマーケター",
        )

        # Verify update was successful
        assert result is not None
        assert result.name == "田中 花子（更新済み）"
        assert result.age == 36
        assert result.occupation == "シニアマーケター"
        assert result.updated_at > result.created_at

    def test_update_persona_not_found(self, persona_manager, sample_persona_data):
        """Test updating non-existent persona."""
        result = persona_manager.update_persona("non-existent-id", name="New Name")
        assert result is None

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

    def test_update_persona_partial_fields(self, persona_manager, sample_persona_data):
        """Test partial field update preserves other fields."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(persona)

        # Update only some fields
        result = persona_manager.update_persona(
            persona.id,
            name="田中 花子（編集済み）",
            age=37,
            values=["効率性", "革新性", "協調性"],
        )

        # Verify update was successful
        assert result is not None
        assert result.name == "田中 花子（編集済み）"
        assert result.age == 37
        assert result.values == ["効率性", "革新性", "協調性"]
        # Other fields should remain unchanged
        assert result.occupation == sample_persona_data["occupation"]
        assert result.background == sample_persona_data["background"]

    def test_update_persona_not_found_returns_none(self, persona_manager):
        """Test updating non-existent persona returns None."""
        result = persona_manager.update_persona("non-existent-id", name="New Name")
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

    def test_database_error_handling(self, mock_database_service):
        """Test handling of database errors."""
        # Create a mock database service that raises an error
        error_db_service = Mock()
        error_db_service.save_persona.side_effect = DatabaseError(
            "Database save failed"
        )

        manager = PersonaManager(database_service=error_db_service)

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
