"""PersonaManager の単体テスト（欠落メソッド分）"""

import pytest

from src.models.errors import ErrorCode
from tests.error_helpers import raises_code
from unittest.mock import Mock, patch
from dataclasses import replace

from src.managers.persona_manager import PersonaManager, PersonaManagerError
from src.models.authz import Principal, Role
from src.models.errors import AccessDeniedError, CodedError
from src.models.persona import Persona

#: The user who saves and edits in these tests (owner of the personas).
OWNER = Principal("sub-owner", frozenset({Role.MEMBER}))


@pytest.fixture
def mock_db():
    mock = Mock()
    mock.get.return_value = Persona.create_new(
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
    return PersonaManager(persona_store=mock_db)


class TestGeneratePersonas:
    """PersonaGenerationManager.generate のバリデーションテスト"""

    @pytest.fixture
    def gen_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        return PersonaGenerationManager(agent_service=Mock())

    def test_invalid_count_zero(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_PERSONA_COUNT_INVALID
        ):
            gen_manager.generate(
                OWNER, file_contents=[], data_type="interview", persona_count=0
            )

    def test_invalid_count_over_10(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_PERSONA_COUNT_INVALID
        ):
            gen_manager.generate(
                OWNER, file_contents=[], data_type="interview", persona_count=11
            )

    def test_no_files_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with raises_code(
            PersonaGenerationManagerError, ErrorCode.GENERATION_FILES_REQUIRED
        ):
            gen_manager.generate(
                OWNER, file_contents=[], data_type="interview", persona_count=3
            )

    def test_dwh_empty_angle_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with raises_code(
            PersonaGenerationManagerError,
            ErrorCode.GENERATION_DATA_DESCRIPTION_REQUIRED,
        ):
            gen_manager.generate(
                OWNER,
                file_contents=[],
                data_type="dwh",
                persona_count=3,
                data_description="",
            )

    def test_capacity_error_converted(self):
        """Service層のGenerationCapacityErrorはPersonaGenerationCapacityErrorに変換される"""
        from src.managers.persona_generation_manager import (
            PersonaGenerationManager,
            PersonaGenerationCapacityError,
        )
        from src.services.agent_service import GenerationCapacityError

        agent_service = Mock()
        agent_service.create_generation_agent.return_value = Mock()
        agent_service.run_persona_generation.side_effect = GenerationCapacityError(
            "生成するデータ量が大きすぎて処理しきれませんでした。"
        )
        gen_manager = PersonaGenerationManager(agent_service=agent_service)

        with pytest.raises(PersonaGenerationCapacityError):
            gen_manager.generate(
                OWNER,
                file_contents=[(b"sample interview text", "interview.txt")],
                data_type="interview",
                persona_count=10,
            )


class TestExtractFileTexts:
    """PersonaGenerationManager._extract_file_texts の抽出後バリデーション"""

    @pytest.fixture
    def gen_manager(self):
        from src.managers.persona_generation_manager import PersonaGenerationManager

        return PersonaGenerationManager(agent_service=Mock())

    def test_content_too_short_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationManagerError,
        )

        with raises_code(
            PersonaGenerationManagerError,
            ErrorCode.INTERVIEW_FILE_CONTENT_TOO_SHORT,
        ):
            gen_manager._extract_file_texts([("短い".encode("utf-8"), "tiny.txt")])

    def test_total_chars_exceeded_raises(self, gen_manager):
        from src.managers.persona_generation_manager import (
            PersonaGenerationCapacityError,
        )

        with patch("src.managers.persona_generation_manager.config") as mock_config:
            mock_config.PERSONA_SOURCE_MAX_CHARS = 100
            with raises_code(
                PersonaGenerationCapacityError,
                ErrorCode.GENERATION_CAPACITY_EXCEEDED,
                max_chars=100,
            ):
                gen_manager._extract_file_texts(
                    [("あ".encode("utf-8") * 200, "big.txt")]
                )

    def test_valid_content_passes(self, gen_manager):
        combined, csv_paths = gen_manager._extract_file_texts(
            [("十分な長さのインタビュー内容です。".encode("utf-8"), "ok.txt")]
        )
        assert "ok.txt" in combined
        assert csv_paths == []


class TestSavePersona:
    """save_persona のテスト"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(persona_store=Mock())

    def test_none_persona_raises(self, manager):
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_INVALID):
            manager.save_persona(OWNER, None)

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
        result = manager.save_persona(OWNER, persona)
        assert result == persona.id
        manager._personas.put_new.assert_called_once()

    def test_db_error(self, manager):
        from src.services.stores.persona_store import PersonaStoreError

        persona = Persona.create_new(
            name="テスト",
            age=30,
            occupation="会社員",
            background="テスト背景",
            values=["v"],
            pain_points=["p"],
            goals=["g"],
        )
        manager._personas.put_new.side_effect = PersonaStoreError("DB down")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.save_persona(OWNER, persona)


class TestGetPersonasForUse:
    """get_personas_for_use（議論・インタビューの開始、U6 BR2.8）

    参加者は ParticipantReader が判定してから本体を読む。判定を通らない読み出し
    （get_persona_unchecked）はなくした。利用者を受け取る get_persona の判定は
    test_persona_manager_bindings.py が確かめる。
    """

    @pytest.fixture
    def world(self):
        from tests.unit.discussion_world import World

        return World()

    @pytest.fixture
    def manager(self, world):
        return PersonaManager(persona_store=Mock(), participants=world.participants)

    def test_readable_personas_in_order(self, manager, world):
        from tests.unit.discussion_world import OWNER as RUNNER

        personas = manager.get_personas_for_use(RUNNER, ["p2", "p1"])
        assert [p.id for p in personas] == ["p2", "p1"]

    def test_one_unreadable_persona_stops_all(self, manager, world):
        from tests.unit.discussion_world import OWNER as RUNNER

        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_personas_for_use(RUNNER, ["p1", "p3"])
        world.bodies.get_many.assert_not_called()

    def test_missing_persona_is_not_found(self, manager):
        from tests.unit.discussion_world import OWNER as RUNNER

        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.get_personas_for_use(RUNNER, ["p1", "nonexistent"])

    def test_no_ids_is_invalid(self, manager):
        from tests.unit.discussion_world import OWNER as RUNNER

        with raises_code(PersonaManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.get_personas_for_use(RUNNER, ["", " "])

    def test_ai_execute_comes_before_the_personas(self, manager, world):
        """C12: 権限がなければ、ペルソナの有無を知らせず 403。"""
        from tests.unit.discussion_world import NO_ROLE

        with pytest.raises(AccessDeniedError):
            manager.get_personas_for_use(NO_ROLE, ["nonexistent"])
        assert world.personas.calls == 0

    def test_the_unchecked_read_is_gone(self, manager):
        assert not hasattr(manager, "get_persona_unchecked")


class TestEditPersona:
    """update_persona のテスト（所有者として編集する。判定は test_persona_manager_sharing）"""

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
        ).owned_by(OWNER.sub)
        mock_db.get.return_value = persona
        mock_db.update_editable.return_value = True
        return PersonaManager(persona_store=mock_db, resource_sharing=Mock())

    def test_empty_id_raises(self, manager):
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.update_persona(OWNER, "")

    def test_persona_not_found(self, manager):
        manager._personas.get.return_value = None
        with raises_code(CodedError, ErrorCode.PERSONA_NOT_FOUND):
            manager.update_persona(OWNER, "p1", name="新名前")

    def test_success(self, manager):
        result = manager.update_persona(OWNER, "p1", name="新名前")
        assert result.name == "新名前"
        manager._personas.update_editable.assert_called_once()

    def test_deleted_while_editing_is_not_found(self, manager):
        manager._personas.update_editable.return_value = False
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.update_persona(OWNER, "p1", name="新名前")

    def test_validation_error_is_kept(self, manager):
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED):
            manager.update_persona(OWNER, "p1", name=" ")

    def test_db_error(self, manager):
        manager._personas.update_editable.side_effect = RuntimeError("down")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.update_persona(OWNER, "p1", name="新名前")

    def test_read_error(self, manager):
        manager._personas.get.side_effect = RuntimeError("down")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.update_persona(OWNER, "p1", name="新名前")

    def test_other_member_cannot_edit(self, manager):
        shared = replace(
            manager._personas.get.return_value,
            shared_group_ids=frozenset({"sales"}),
        )
        manager._personas.get.return_value = shared
        member = Principal("sub-other", frozenset({Role.MEMBER}), frozenset({"sales"}))
        with pytest.raises(AccessDeniedError):
            manager.update_persona(member, "p1", name="新名前")


class TestDeletePersona:
    """delete_persona のテスト（ResourceSharing.delete_with_shares を通す）"""

    @pytest.fixture
    def manager(self):
        return PersonaManager(persona_store=Mock(), resource_sharing=Mock())

    def test_empty_id_raises(self, manager):
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_ID_INVALID):
            manager.delete_persona(OWNER, "")

    def test_success(self, manager):
        manager._resource_sharing.delete_with_shares.return_value = {"id": "p1"}
        assert manager.delete_persona(OWNER, "p1") is True

    def test_not_found(self, manager):
        manager._resource_sharing.delete_with_shares.return_value = None
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_NOT_FOUND):
            manager.delete_persona(OWNER, "p1")

    def test_unexpected_error(self, manager):
        manager._resource_sharing.delete_with_shares.side_effect = RuntimeError("x")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.delete_persona(OWNER, "p1")

    def test_coded_error_passes_through(self, manager):
        manager._resource_sharing.delete_with_shares.side_effect = CodedError(
            "x", code=ErrorCode.PERSONA_OPERATION_FAILED
        )
        with raises_code(CodedError, ErrorCode.PERSONA_OPERATION_FAILED):
            manager.delete_persona(OWNER, "p1")


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
        mock_db.put_new.return_value = None
        return PersonaManager(persona_store=mock_db)

    # --- 必須フィールド ---

    def test_empty_name_raises(self, manager):
        persona = _valid_persona(name="")
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED, field="name"
        ):
            manager.save_persona(OWNER, persona)

    def test_whitespace_name_raises(self, manager):
        persona = _valid_persona(name="   ")
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED, field="name"
        ):
            manager.save_persona(OWNER, persona)

    def test_age_none_raises(self, manager):
        persona = _valid_persona()
        persona.age = None
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AGE_OUT_OF_RANGE):
            manager.save_persona(OWNER, persona)

    def test_age_negative_raises(self, manager):
        persona = _valid_persona(age=-1)
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AGE_OUT_OF_RANGE):
            manager.save_persona(OWNER, persona)

    def test_age_over_150_raises(self, manager):
        persona = _valid_persona(age=151)
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_AGE_OUT_OF_RANGE):
            manager.save_persona(OWNER, persona)

    def test_empty_occupation_raises(self, manager):
        persona = _valid_persona(occupation="")
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED, field="occupation"
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_background_raises(self, manager):
        persona = _valid_persona(background="")
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_REQUIRED, field="background"
        ):
            manager.save_persona(OWNER, persona)

    # --- リストフィールド（空リスト）---

    def test_empty_values_raises(self, manager):
        persona = _valid_persona(values=[])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_EMPTY, field="values"
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_pain_points_raises(self, manager):
        persona = _valid_persona(pain_points=[])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_EMPTY, field="pain_points"
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_goals_raises(self, manager):
        persona = _valid_persona(goals=[])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_EMPTY, field="goals"
        ):
            manager.save_persona(OWNER, persona)

    # --- リストフィールド（空文字項目）---

    def test_empty_string_in_values_raises(self, manager):
        persona = _valid_persona(values=["valid", ""])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM, field="values"
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_string_in_pain_points_raises(self, manager):
        persona = _valid_persona(pain_points=["valid", "  "])
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM,
            field="pain_points",
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_string_in_goals_raises(self, manager):
        persona = _valid_persona(goals=["valid", ""])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM, field="goals"
        ):
            manager.save_persona(OWNER, persona)

    # --- 文字数上限 ---

    def test_name_over_100_chars_raises(self, manager):
        persona = _valid_persona(name="あ" * 101)
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_FIELD_TOO_LONG,
            field="name",
            max_length=100,
        ):
            manager.save_persona(OWNER, persona)

    def test_occupation_over_200_chars_raises(self, manager):
        persona = _valid_persona(occupation="あ" * 201)
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_TOO_LONG, max_length=200
        ):
            manager.save_persona(OWNER, persona)

    def test_background_over_2000_chars_raises(self, manager):
        persona = _valid_persona(background="あ" * 2001)
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_FIELD_TOO_LONG, max_length=2000
        ):
            manager.save_persona(OWNER, persona)

    # --- リスト項目数上限 ---

    def test_values_over_10_items_raises(self, manager):
        persona = _valid_persona(values=[f"v{i}" for i in range(11)])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS, max_items=10
        ):
            manager.save_persona(OWNER, persona)

    def test_pain_points_over_10_items_raises(self, manager):
        persona = _valid_persona(pain_points=[f"p{i}" for i in range(11)])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS, max_items=10
        ):
            manager.save_persona(OWNER, persona)

    def test_goals_over_10_items_raises(self, manager):
        persona = _valid_persona(goals=[f"g{i}" for i in range(11)])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS, max_items=10
        ):
            manager.save_persona(OWNER, persona)

    # --- 性別バリデーション ---

    def test_invalid_gender_raises(self, manager):
        persona = _valid_persona(gender="invalid_gender")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_GENDER_INVALID):
            manager.save_persona(OWNER, persona)

    def test_valid_gender_passes(self, manager):
        persona = _valid_persona(gender="male")
        result = manager.save_persona(OWNER, persona)
        assert result == persona.id

    def test_none_gender_passes(self, manager):
        persona = _valid_persona(gender=None)
        result = manager.save_persona(OWNER, persona)
        assert result == persona.id

    # --- 国コードバリデーション ---

    @patch("src.managers.persona_manager.country_service")
    def test_invalid_country_raises(self, mock_cs, manager):
        mock_cs.is_valid_country.return_value = False
        persona = _valid_persona(country="XX")
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_COUNTRY_INVALID):
            manager.save_persona(OWNER, persona)

    @patch("src.managers.persona_manager.country_service")
    def test_valid_country_passes(self, mock_cs, manager):
        mock_cs.is_valid_country.return_value = True
        persona = _valid_persona(country="JP")
        result = manager.save_persona(OWNER, persona)
        assert result == persona.id

    # --- 都市バリデーション ---

    def test_city_over_100_chars_raises(self, manager):
        persona = _valid_persona(city="あ" * 101)
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_FIELD_TOO_LONG,
            field="city",
            max_length=100,
        ):
            manager.save_persona(OWNER, persona)

    # --- タグバリデーション ---

    def test_tags_over_20_items_raises(self, manager):
        persona = _valid_persona(tags=[f"tag{i}" for i in range(21)])
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_LIST_TOO_MANY_ITEMS,
            field="tags",
            max_items=20,
        ):
            manager.save_persona(OWNER, persona)

    def test_empty_tag_raises(self, manager):
        persona = _valid_persona(tags=["valid", ""])
        with raises_code(
            PersonaManagerError, ErrorCode.PERSONA_LIST_HAS_EMPTY_ITEM, field="tags"
        ):
            manager.save_persona(OWNER, persona)

    def test_tag_over_50_chars_raises(self, manager):
        persona = _valid_persona(tags=["あ" * 51])
        with raises_code(
            PersonaManagerError,
            ErrorCode.PERSONA_LIST_ITEM_TOO_LONG,
            field="tags",
            max_length=50,
        ):
            manager.save_persona(OWNER, persona)

    def test_tag_with_comma_raises(self, manager):
        persona = _valid_persona(tags=["tag,with,comma"])
        with raises_code(PersonaManagerError, ErrorCode.PERSONA_TAG_COMMA_NOT_ALLOWED):
            manager.save_persona(OWNER, persona)

    def test_valid_tags_passes(self, manager):
        persona = _valid_persona(tags=["タグ1", "タグ2"])
        result = manager.save_persona(OWNER, persona)
        assert result == persona.id


# 紐付けの作成・一覧・削除（U5、PersonaBindings）は
# tests/unit/test_persona_manager_bindings.py が判定と一緒に確かめる。
