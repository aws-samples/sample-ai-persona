"""PersonaManager の単体テスト（欠落メソッド分）"""

import pytest
from unittest.mock import Mock, patch

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.dataset import Dataset, DatasetColumn
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


# --- Lines 306-542: _validate_persona_for_save, KB/Dataset bindings ---


def _valid_persona(**overrides):
    """バリデーションを通る最小限のペルソナを返すヘルパー"""
    defaults = dict(
        name="テスト太郎",
        age=30,
        occupation="会社員",
        background="テスト背景です",
        values=["価値観1"],
        pain_points=["課題1"],
        goals=["目標1"],
    )
    defaults.update(overrides)
    return Persona.create_new(**defaults)


@pytest.mark.unit
class TestValidatePersonaForSave:
    """_validate_persona_for_save のバリデーションを save_persona 経由でテスト"""

    @pytest.fixture
    def manager(self):
        mock_db = Mock()
        mock_db.save_persona.return_value = "id-123"
        return PersonaManager(database_service=mock_db)

    # --- 必須フィールド ---

    def test_empty_name_raises(self, manager):
        persona = _valid_persona(name="")
        with pytest.raises(PersonaManagerError, match="ペルソナ名が設定されていません"):
            manager.save_persona(persona)

    def test_whitespace_name_raises(self, manager):
        persona = _valid_persona(name="   ")
        with pytest.raises(PersonaManagerError, match="ペルソナ名が設定されていません"):
            manager.save_persona(persona)

    def test_age_none_raises(self, manager):
        persona = _valid_persona()
        persona.age = None
        with pytest.raises(PersonaManagerError, match="年齢は0から150"):
            manager.save_persona(persona)

    def test_age_negative_raises(self, manager):
        persona = _valid_persona(age=-1)
        with pytest.raises(PersonaManagerError, match="年齢は0から150"):
            manager.save_persona(persona)

    def test_age_over_150_raises(self, manager):
        persona = _valid_persona(age=151)
        with pytest.raises(PersonaManagerError, match="年齢は0から150"):
            manager.save_persona(persona)

    def test_empty_occupation_raises(self, manager):
        persona = _valid_persona(occupation="")
        with pytest.raises(PersonaManagerError, match="職業が設定されていません"):
            manager.save_persona(persona)

    def test_empty_background_raises(self, manager):
        persona = _valid_persona(background="")
        with pytest.raises(PersonaManagerError, match="背景が設定されていません"):
            manager.save_persona(persona)

    # --- リストフィールド（空リスト）---

    def test_empty_values_raises(self, manager):
        persona = _valid_persona(values=[])
        with pytest.raises(PersonaManagerError, match="価値観が設定されていません"):
            manager.save_persona(persona)

    def test_empty_pain_points_raises(self, manager):
        persona = _valid_persona(pain_points=[])
        with pytest.raises(PersonaManagerError, match="課題・悩みが設定されていません"):
            manager.save_persona(persona)

    def test_empty_goals_raises(self, manager):
        persona = _valid_persona(goals=[])
        with pytest.raises(PersonaManagerError, match="目標・願望が設定されていません"):
            manager.save_persona(persona)

    # --- リストフィールド（空文字項目）---

    def test_empty_string_in_values_raises(self, manager):
        persona = _valid_persona(values=["valid", ""])
        with pytest.raises(PersonaManagerError, match="価値観に空の項目"):
            manager.save_persona(persona)

    def test_empty_string_in_pain_points_raises(self, manager):
        persona = _valid_persona(pain_points=["valid", "  "])
        with pytest.raises(PersonaManagerError, match="課題・悩みに空の項目"):
            manager.save_persona(persona)

    def test_empty_string_in_goals_raises(self, manager):
        persona = _valid_persona(goals=["valid", ""])
        with pytest.raises(PersonaManagerError, match="目標・願望に空の項目"):
            manager.save_persona(persona)

    # --- 文字数上限 ---

    def test_name_over_100_chars_raises(self, manager):
        persona = _valid_persona(name="あ" * 101)
        with pytest.raises(PersonaManagerError, match="100文字以内"):
            manager.save_persona(persona)

    def test_occupation_over_200_chars_raises(self, manager):
        persona = _valid_persona(occupation="あ" * 201)
        with pytest.raises(PersonaManagerError, match="200文字以内"):
            manager.save_persona(persona)

    def test_background_over_2000_chars_raises(self, manager):
        persona = _valid_persona(background="あ" * 2001)
        with pytest.raises(PersonaManagerError, match="2000文字以内"):
            manager.save_persona(persona)

    # --- リスト項目数上限 ---

    def test_values_over_10_items_raises(self, manager):
        persona = _valid_persona(values=[f"v{i}" for i in range(11)])
        with pytest.raises(PersonaManagerError, match="10項目以内"):
            manager.save_persona(persona)

    def test_pain_points_over_10_items_raises(self, manager):
        persona = _valid_persona(pain_points=[f"p{i}" for i in range(11)])
        with pytest.raises(PersonaManagerError, match="10項目以内"):
            manager.save_persona(persona)

    def test_goals_over_10_items_raises(self, manager):
        persona = _valid_persona(goals=[f"g{i}" for i in range(11)])
        with pytest.raises(PersonaManagerError, match="10項目以内"):
            manager.save_persona(persona)

    # --- 性別バリデーション ---

    def test_invalid_gender_raises(self, manager):
        persona = _valid_persona(gender="invalid_gender")
        with pytest.raises(PersonaManagerError, match="性別は"):
            manager.save_persona(persona)

    def test_valid_gender_passes(self, manager):
        persona = _valid_persona(gender="male")
        result = manager.save_persona(persona)
        assert result == "id-123"

    def test_none_gender_passes(self, manager):
        persona = _valid_persona(gender=None)
        result = manager.save_persona(persona)
        assert result == "id-123"

    # --- 国コードバリデーション ---

    @patch("src.managers.persona_manager.country_service")
    def test_invalid_country_raises(self, mock_cs, manager):
        mock_cs.is_valid_country.return_value = False
        persona = _valid_persona(country="XX")
        with pytest.raises(PersonaManagerError, match="ISO 3166-1 alpha-2"):
            manager.save_persona(persona)

    @patch("src.managers.persona_manager.country_service")
    def test_valid_country_passes(self, mock_cs, manager):
        mock_cs.is_valid_country.return_value = True
        persona = _valid_persona(country="JP")
        result = manager.save_persona(persona)
        assert result == "id-123"

    # --- 都市バリデーション ---

    def test_city_over_100_chars_raises(self, manager):
        persona = _valid_persona(city="あ" * 101)
        with pytest.raises(PersonaManagerError, match="100文字以内"):
            manager.save_persona(persona)

    # --- タグバリデーション ---

    def test_tags_over_20_items_raises(self, manager):
        persona = _valid_persona(tags=[f"tag{i}" for i in range(21)])
        with pytest.raises(PersonaManagerError, match="20個以内"):
            manager.save_persona(persona)

    def test_empty_tag_raises(self, manager):
        persona = _valid_persona(tags=["valid", ""])
        with pytest.raises(PersonaManagerError, match="タグに空の項目"):
            manager.save_persona(persona)

    def test_tag_over_50_chars_raises(self, manager):
        persona = _valid_persona(tags=["あ" * 51])
        with pytest.raises(PersonaManagerError, match="50文字以内"):
            manager.save_persona(persona)

    def test_tag_with_comma_raises(self, manager):
        persona = _valid_persona(tags=["tag,with,comma"])
        with pytest.raises(PersonaManagerError, match="カンマ"):
            manager.save_persona(persona)

    def test_valid_tags_passes(self, manager):
        persona = _valid_persona(tags=["タグ1", "タグ2"])
        result = manager.save_persona(persona)
        assert result == "id-123"


@pytest.mark.unit
class TestGetKBBinding:
    """get_kb_binding のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_binding_exists_and_kb_exists(self, manager):
        mock_binding = Mock(id="b1", kb_id="kb1")
        mock_kb = Mock(id="kb1")
        manager.database_service.get_all_knowledge_bases.return_value = [mock_kb]
        manager.database_service.get_kb_binding_by_persona.return_value = mock_binding
        manager.database_service.get_knowledge_base.return_value = mock_kb

        kbs, binding = manager.get_kb_binding("p1")
        assert kbs == [mock_kb]
        assert binding == mock_binding
        manager.database_service.delete_kb_binding.assert_not_called()

    def test_binding_exists_but_kb_missing_deletes_binding(self, manager):
        mock_binding = Mock(id="b1", kb_id="kb-gone")
        manager.database_service.get_all_knowledge_bases.return_value = []
        manager.database_service.get_kb_binding_by_persona.return_value = mock_binding
        manager.database_service.get_knowledge_base.return_value = None

        kbs, binding = manager.get_kb_binding("p1")
        assert kbs == []
        assert binding is None
        manager.database_service.delete_kb_binding.assert_called_once_with("b1")

    def test_no_binding(self, manager):
        manager.database_service.get_all_knowledge_bases.return_value = [Mock()]
        manager.database_service.get_kb_binding_by_persona.return_value = None

        kbs, binding = manager.get_kb_binding("p1")
        assert len(kbs) == 1
        assert binding is None


@pytest.mark.unit
class TestCreateKBBinding:
    """create_kb_binding のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_creates_binding_without_filters(self, manager):
        result = manager.create_kb_binding("p1", "kb1")
        assert result.persona_id == "p1"
        assert result.kb_id == "kb1"
        assert result.metadata_filters == {}
        manager.database_service.save_kb_binding.assert_called_once()

    def test_creates_binding_with_filters(self, manager):
        filters = {"category": "tech"}
        result = manager.create_kb_binding("p1", "kb1", metadata_filters=filters)
        assert result.metadata_filters == {"category": "tech"}
        manager.database_service.save_kb_binding.assert_called_once()


@pytest.mark.unit
class TestDeleteKBBinding:
    """delete_kb_binding のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_delegates_to_db(self, manager):
        manager.delete_kb_binding("b1")
        manager.database_service.delete_kb_binding.assert_called_once_with("b1")


@pytest.mark.unit
class TestGetDatasetBindings:
    """get_dataset_bindings のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_returns_datasets_and_bindings_map(self, manager):
        mock_ds = Mock(id="ds1")
        mock_binding = Mock(dataset_id="ds1")
        manager.database_service.get_all_datasets.return_value = [mock_ds]
        manager.database_service.get_bindings_by_persona.return_value = [mock_binding]

        datasets, bindings_map = manager.get_dataset_bindings("p1")
        assert datasets == [mock_ds]
        assert bindings_map == {"ds1": mock_binding}

    def test_empty_bindings(self, manager):
        manager.database_service.get_all_datasets.return_value = [Mock()]
        manager.database_service.get_bindings_by_persona.return_value = []

        datasets, bindings_map = manager.get_dataset_bindings("p1")
        assert len(datasets) == 1
        assert bindings_map == {}


@pytest.mark.unit
class TestCreateDatasetBinding:
    """create_dataset_binding のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_invalid_column_raises(self, manager):
        dataset = Dataset.create_new(
            name="テストDS",
            description="テスト",
            s3_path="s3://bucket/key",
            columns=[DatasetColumn(name="user_id", data_type="string")],
        )
        manager.database_service.get_dataset.return_value = dataset

        with pytest.raises(PersonaManagerError, match="データセットに存在しません"):
            manager.create_dataset_binding(
                "p1", "ds1", key_name="invalid_col", key_value="v1"
            )

    def test_valid_column_saves_binding(self, manager):
        dataset = Dataset.create_new(
            name="テストDS",
            description="テスト",
            s3_path="s3://bucket/key",
            columns=[DatasetColumn(name="user_id", data_type="string")],
        )
        manager.database_service.get_dataset.return_value = dataset

        result = manager.create_dataset_binding(
            "p1", "ds1", key_name="user_id", key_value="U123"
        )
        assert result.persona_id == "p1"
        assert result.dataset_id == "ds1"
        assert result.binding_keys == {"user_id": "U123"}
        manager.database_service.save_binding.assert_called_once()

    def test_no_key_saves_with_empty_binding_keys(self, manager):
        result = manager.create_dataset_binding("p1", "ds1")
        assert result.binding_keys == {}
        manager.database_service.save_binding.assert_called_once()

    def test_empty_key_name_saves_with_empty_binding_keys(self, manager):
        result = manager.create_dataset_binding("p1", "ds1", key_name="", key_value="")
        assert result.binding_keys == {}


@pytest.mark.unit
class TestDeleteDatasetBinding:
    """delete_dataset_binding のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(database_service=Mock())

    def test_delegates_to_db(self, manager):
        manager.delete_dataset_binding("b1")
        manager.database_service.delete_binding.assert_called_once_with("b1")
