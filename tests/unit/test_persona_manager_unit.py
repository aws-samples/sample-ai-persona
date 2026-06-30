"""PersonaManager の単体テスト（欠落メソッド分）"""

import pytest
from unittest.mock import Mock

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
    return PersonaManager(database_service=mock_db)


class TestGeneratePersonas:
    """PersonaGenerationManager.generate_and_cache のバリデーションテスト"""

    @pytest.fixture
    def gen_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        return PersonaGenerationManager(agent_service=Mock(), database_service=Mock())

    def test_invalid_count_zero(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with pytest.raises(PersonaGenerationManagerError, match="1-10の範囲"):
            gen_manager.generate_and_cache([], "interview", 0)

    def test_invalid_count_over_10(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with pytest.raises(PersonaGenerationManagerError, match="1-10の範囲"):
            gen_manager.generate_and_cache([], "interview", 11)

    def test_no_files_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with pytest.raises(
            PersonaGenerationManagerError, match="ファイルが選択されていません"
        ):
            gen_manager.generate_and_cache([], "interview", 3)

    def test_dwh_empty_angle_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with pytest.raises(PersonaGenerationManagerError, match="分析の切り口"):
            gen_manager.generate_and_cache([], "dwh", 3, data_description="")


class TestSavePersona:
    """save_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

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
        return PersonaManager(database_service=Mock())

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
        return PersonaManager(database_service=Mock())

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
    """update_persona のテスト"""

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
        return PersonaManager(database_service=mock_db)

    def test_empty_id_raises(self, manager):
        with pytest.raises(PersonaManagerError, match="IDが無効"):
            manager.update_persona("")

    def test_persona_not_found(self, manager):
        manager.database_service.get_persona.return_value = None
        result = manager.update_persona("p1", name="新名前")
        assert result is None

    def test_success(self, manager):
        result = manager.update_persona("p1", name="新名前")
        assert result is not None
        assert result.name == "新名前"
        manager.database_service.update_persona.assert_called_once()

    def test_update_failure_raises(self, manager):
        manager.database_service.update_persona.return_value = False
        with pytest.raises(PersonaManagerError, match="更新に失敗"):
            manager.update_persona("p1", name="新名前")


class TestDeletePersona:
    """delete_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

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
        return PersonaManager(database_service=mock_db)


class TestPersonaCount:
    """get_persona_count / persona_exists のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_count(self, manager):
        manager.database_service.get_persona_count.return_value = 5
        assert manager.get_persona_count() == 5
