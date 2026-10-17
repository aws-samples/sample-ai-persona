"""CustomPersonaData・PersonaDataRef・AssetScopeChange の単体テスト（U7 entities.md）。"""

from datetime import datetime

import pytest

from src.models.custom_persona_data import (
    NAME_MAX_LENGTH,
    NAME_RESERVATION_PREFIX,
    CustomPersonaData,
    ExtraColumn,
    PersonaDataRef,
    custom_file_key,
    is_name_reservation,
    normalize_name,
    reservation_id,
)
from src.models.errors import CodedError, ErrorCode
from src.models.sharing import AssetScopeChange
from tests.error_helpers import raises_code

NOW = datetime(2026, 10, 1, 12, 0, 0)


def _data(**overrides: object) -> CustomPersonaData:
    kwargs: dict = {
        "name": "顧客データ",
        "columns": ["sex", "age", "hobby"],
        "row_count": 150,
        "registered_by": "admin-sub",
        "standard_mapping": {"sex": "性別"},
        "extra_columns": [ExtraColumn(csv_column="hobby", description="趣味")],
        "now": NOW,
    }
    kwargs.update(overrides)
    return CustomPersonaData.create_new(**kwargs)


class TestCreateNew:
    def test_gets_a_new_id_and_a_key_made_from_it(self):
        first, second = _data(), _data()
        assert first.id != second.id
        assert first.s3_key == f"persona-dataset/custom/{first.id}.parquet"
        assert custom_file_key(first.id) == first.s3_key

    def test_records_registration_and_defaults_to_admin_only(self):
        data = _data()
        assert data.registered_by == "admin-sub"
        assert data.registered_at == NOW
        assert data.created_at == NOW
        assert data.row_count == 150
        assert data.shared_group_ids == frozenset()

    def test_scope_chosen_at_registration(self):
        data = _data(shared_group_ids=frozenset({"sales"}))
        assert data.shared_group_ids == frozenset({"sales"})

    def test_name_is_stripped(self):
        assert _data(name="  顧客  ").name == "顧客"

    def test_key_does_not_depend_on_the_name(self):
        data = _data(name="../../etc/passwd")
        assert ".." not in data.s3_key
        assert data.s3_key == custom_file_key(data.id)


class TestNameValidation:
    @pytest.mark.parametrize("name", ["", "   ", "あ" * (NAME_MAX_LENGTH + 1)])
    def test_rejects_blank_and_too_long(self, name):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_INVALID):
            normalize_name(name)

    def test_accepts_the_bounds(self):
        assert normalize_name("a") == "a"
        assert normalize_name(" " + "あ" * NAME_MAX_LENGTH + " ") == (
            "あ" * NAME_MAX_LENGTH
        )

    def test_is_case_sensitive(self):
        assert reservation_id("Data") != reservation_id("data")

    def test_reservation_id(self):
        assert reservation_id(" 顧客 ") == f"{NAME_RESERVATION_PREFIX}顧客"
        assert is_name_reservation(reservation_id("x"))
        assert not is_name_reservation("2b0f8d2e-0000-4000-8000-000000000000")


class TestSerialization:
    def test_roundtrip(self):
        data = _data(shared_group_ids=frozenset({"sales", "plan"}))
        item = data.to_dict()
        assert item["shared_group_ids"] == ["plan", "sales"]
        assert item["extra_columns"] == [{"csv_column": "hobby", "description": "趣味"}]
        assert CustomPersonaData.from_dict(item) == data

    def test_to_dict_omits_empty_scope(self):
        assert "shared_group_ids" not in _data().to_dict()

    def test_from_dict_reads_sets_and_decimals(self):
        from decimal import Decimal

        item = _data().to_dict()
        item["row_count"] = Decimal("150")
        item["shared_group_ids"] = {"sales"}
        restored = CustomPersonaData.from_dict(item)
        assert restored.row_count == 150
        assert restored.shared_group_ids == frozenset({"sales"})

    def test_extra_columns_as_prompt_dicts(self):
        data = _data(
            extra_columns=[
                ExtraColumn(csv_column="hobby", label="趣味", description="d")
            ]
        )
        assert data.extra_column_dicts() == [
            {"csv_column": "hobby", "label": "趣味", "description": "d"}
        ]

    def test_extra_column_reads_meta_json_shape(self):
        column = ExtraColumn.from_dict({"csv_column": "c", "description": "x"})
        assert column == ExtraColumn(csv_column="c", description="x")

    def test_renamed_returns_a_new_instance(self):
        data = _data()
        renamed = data.renamed(" 新しい名前 ")
        assert renamed.name == "新しい名前"
        assert data.name == "顧客データ"
        assert renamed.id == data.id

    def test_renamed_rejects_an_invalid_name(self):
        with raises_code(CodedError, ErrorCode.CUSTOM_PERSONA_DATA_NAME_INVALID):
            _data().renamed(" ")


class TestPersonaDataRef:
    def test_nemotron(self):
        ref = PersonaDataRef.parse("nemotron")
        assert ref.is_nemotron
        assert ref.custom_id is None
        assert ref.value == "nemotron"

    def test_empty_means_nemotron(self):
        assert PersonaDataRef.parse(None).is_nemotron
        assert PersonaDataRef.parse("").is_nemotron

    def test_custom_id(self):
        data_id = "2b0f8d2e-1111-4000-8000-000000000000"
        ref = PersonaDataRef.parse(f"custom:{data_id}")
        assert not ref.is_nemotron
        assert ref.custom_id == data_id
        assert ref.value == f"custom:{data_id}"
        assert PersonaDataRef.custom(data_id) == ref

    @pytest.mark.parametrize(
        "value",
        ["custom:", "custom:顧客データ", "custom:../x", "other", "custom:name#x"],
    )
    def test_rejects_anything_else(self, value):
        with pytest.raises(ValueError):
            PersonaDataRef.parse(value)


class TestAssetScopeChange:
    def test_holds_add_and_remove(self):
        change = AssetScopeChange(add=frozenset({"a"}), remove=frozenset({"b"}))
        assert not change.is_empty
        assert AssetScopeChange().is_empty

    def test_rejects_overlap(self):
        with pytest.raises(ValueError):
            AssetScopeChange(add=frozenset({"a"}), remove=frozenset({"a"}))

    def test_rejects_non_frozensets(self):
        with pytest.raises(TypeError):
            AssetScopeChange(add={"a"}, remove=frozenset())  # type: ignore[arg-type]

    def test_applied_to(self):
        change = AssetScopeChange(add=frozenset({"a"}), remove=frozenset({"b"}))
        assert change.applied_to(frozenset({"b", "c"})) == frozenset({"a", "c"})
