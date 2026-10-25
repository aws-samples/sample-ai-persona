"""
Integration tests for Persona Manager.
Tests the persona management workflow with an in-memory persona store.
"""

import pytest

from src.models.errors import ErrorCode
from tests.error_helpers import raises_code
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.authz import Principal, Role
from src.models.errors import CodedError
from src.models.persona import Persona
from src.services.stores.persona_store import PersonaStore, PersonaStoreError

#: The saver / owner and an admin (U4: saving and editing take the user).
OWNER = Principal("local:owner@example.com", frozenset({Role.MEMBER}))
ADMIN = Principal("local:admin@example.com", frozenset({Role.ADMIN}))


class TestPersonaManagerIntegration:
    """Integration tests for PersonaManager class."""

    @pytest.fixture
    def mock_persona_store(self):
        """An in-memory stand-in for ``PersonaStore`` (only its real methods)."""
        personas_storage = {}
        store = Mock(spec=PersonaStore)

        def put_new(persona):
            personas_storage[persona.id] = persona

        def update_editable(persona):
            if persona.id in personas_storage:
                personas_storage[persona.id] = persona
                return True
            return False

        store.put_new.side_effect = put_new
        store.get.side_effect = lambda persona_id, consistent=False: (
            personas_storage.get(persona_id)
        )
        store.update_editable.side_effect = update_editable
        store.storage = personas_storage
        return store

    @pytest.fixture
    def persona_manager(self, mock_persona_store):
        """Create a persona manager whose deletes go through a fake ResourceSharing."""
        storage = mock_persona_store.storage

        def delete_with_shares(principal, resource_type, persona_id):
            deleted = storage.pop(persona_id, None)
            return None if deleted is None else {"id": deleted.id}

        sharing = Mock()
        sharing.delete_with_shares.side_effect = delete_with_shares
        bindings = Mock()
        bindings.list_for_persona.return_value = []
        return PersonaManager(
            persona_store=mock_persona_store,
            resource_sharing=sharing,
            binding_store=bindings,
            memory_service=None,
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
    def test_persona_manager_initialization(self, mock_sf, mock_persona_store):
        """Test persona manager initialization."""
        mock_sf.get_persona_store.return_value = mock_persona_store

        # Test with default services
        manager = PersonaManager()
        assert manager._personas is mock_persona_store

        # Test with custom services
        manager = PersonaManager(persona_store=mock_persona_store)
        assert manager._personas is mock_persona_store

    def test_save_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona saving."""
        persona = Persona.create_new(**sample_persona_data)

        # Save persona
        result_id = persona_manager.save_persona(OWNER, persona)

        # Verify result
        assert result_id == persona.id

        # Verify persona was saved to database
        saved_persona = persona_manager.get_persona(OWNER, persona.id)
        assert saved_persona is not None
        assert saved_persona.name == persona.name
        assert saved_persona.age == persona.age

    def test_save_persona_invalid_data(self, persona_manager):
        """Test saving persona with invalid data."""
        # Test with None persona
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_INVALID):
            persona_manager.save_persona(OWNER, None)

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
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED, field="name"
        ):
            persona_manager.save_persona(OWNER, invalid_persona)

    def test_save_persona_city_too_long(self, persona_manager, sample_persona_data):
        """居住都市が100文字超だと拒否される。"""
        persona = Persona.create_new(**sample_persona_data, city="あ" * 101)
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_FIELD_TOO_LONG,
            field="city",
            max_length=100,
        ):
            persona_manager.save_persona(OWNER, persona)

    def test_save_persona_tag_with_comma_rejected(
        self, persona_manager, sample_persona_data
    ):
        """タグにカンマが含まれると拒否される（フィルタ区切りの破損防止）。"""
        persona = Persona.create_new(**sample_persona_data, tags=["B2B, enterprise"])
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_TAG_COMMA_NOT_ALLOWED):
            persona_manager.save_persona(OWNER, persona)

    def test_save_persona_tag_too_long(self, persona_manager, sample_persona_data):
        """タグが50文字超だと拒否される。"""
        persona = Persona.create_new(**sample_persona_data, tags=["あ" * 51])
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_LIST_ITEM_TOO_LONG,
            field="tags",
            max_length=50,
        ):
            persona_manager.save_persona(OWNER, persona)

    def test_save_persona_valid_demographics(
        self, persona_manager, sample_persona_data
    ):
        """正常な city / tags は保存できる。"""
        persona = Persona.create_new(
            **sample_persona_data,
            city="東京都",
            tags=["VIP", "リピーター"],
        )
        saved_id = persona_manager.save_persona(OWNER, persona)
        assert saved_id == persona.id

    def test_get_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona retrieval."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)

        # Retrieve persona
        result = persona_manager.get_persona(OWNER, persona.id)

        # Verify result
        assert result is not None
        assert result.id == persona.id
        assert result.name == persona.name
        assert result.age == persona.age

    def test_get_persona_not_found(self, persona_manager):
        """Test persona retrieval when persona doesn't exist."""
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            persona_manager.get_persona(OWNER, "non-existent-id")

    def test_get_persona_invalid_id(self, persona_manager):
        """Test persona retrieval with invalid ID."""
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_ID_INVALID):
            persona_manager.get_persona(OWNER, "")

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_ID_INVALID):
            persona_manager.get_persona(OWNER, "   ")

    def test_saved_persona_is_owned_by_the_saver(
        self, persona_manager, sample_persona_data
    ):
        """BR6.1: 保存した人が所有者になる。"""
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)
        assert persona_manager.get_persona(OWNER, persona.id).owner_sub == OWNER.sub

    def test_update_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona update via update_persona (partial update)."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)

        # Update persona using partial update interface
        result = persona_manager.update_persona(
            OWNER,
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
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            persona_manager.update_persona(OWNER, "non-existent-id", name="New Name")

    def test_delete_persona_success(self, persona_manager, sample_persona_data):
        """Test successful persona deletion."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)

        # Verify persona exists
        assert persona_manager.get_persona(OWNER, persona.id) is not None

        # Delete persona
        result = persona_manager.delete_persona(OWNER, persona.id)

        # Verify deletion was successful
        assert result is True
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            persona_manager.get_persona(OWNER, persona.id)

    def test_delete_persona_not_found(self, persona_manager):
        """Test deleting non-existent persona (the same answer as unreadable)."""
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            persona_manager.delete_persona(OWNER, "non-existent-id")

    def test_update_persona_partial_fields(self, persona_manager, sample_persona_data):
        """Test partial field update preserves other fields."""
        # Save a persona first
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)

        # Update only some fields
        result = persona_manager.update_persona(
            OWNER,
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

    def test_admin_update_keeps_the_owner(self, persona_manager, sample_persona_data):
        """BR6.6: admin が編集しても所有者は変わらない。"""
        persona = Persona.create_new(**sample_persona_data)
        persona_manager.save_persona(OWNER, persona)
        result = persona_manager.update_persona(ADMIN, persona.id, name="管理者が編集")
        assert result.owner_sub == OWNER.sub

    def test_database_error_handling(self, mock_persona_store):
        """Test handling of database errors."""
        # Create a mock database service that raises an error
        failing_store = Mock()
        failing_store.put_new.side_effect = PersonaStoreError("Database save failed")

        manager = PersonaManager(persona_store=failing_store)

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

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.save_persona(OWNER, persona)

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
        persona_id = persona_manager.save_persona(OWNER, valid_persona)
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

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AGE_OUT_OF_RANGE):
            persona_manager.save_persona(OWNER, invalid_persona)
