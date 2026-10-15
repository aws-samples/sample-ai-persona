"""
紐付けデータのプレビューの SQL の組み立て（U5 で DatasetManager から PersonaManager へ移した）

判定（BR2.6）は tests/unit/test_persona_manager_bindings.py が確かめる。ここでは、
binding_keys から作る WHERE 句とその安全確認を確かめる（今の動きのまま）。
"""

from dataclasses import replace

import pytest

from src.managers.persona_manager import PersonaManagerError
from src.models.dataset import DatasetColumn
from src.models.errors import ErrorCode
from src.models.persona_binding import PersonaBinding
from tests.error_helpers import raises_code
from tests.unit.persona_u5_world import A, NOW, World, make_persona


@pytest.fixture
def world() -> World:
    return World()


@pytest.fixture
def setup(world):
    persona = world.add_persona(make_persona(A.sub))
    dataset = world.add_dataset(
        owner_persona_id=persona.id, s3_path="s3://bucket/datasets/test.csv"
    )
    return persona, dataset


def _bind(world: World, persona, dataset, keys: dict[str, str]) -> None:
    world.bindings.put(
        PersonaBinding.for_dataset(
            persona.id, dataset.id, binding_keys=keys, dedicated=True, now=NOW
        )
    )


class TestPreviewSql:
    def test_dataset_not_found(self, world, setup):
        persona, dataset = setup
        _bind(world, persona, dataset, {})
        del world.datasets[dataset.id]
        with raises_code(PersonaManagerError, ErrorCode.DATASET_NOT_FOUND):
            world.manager().preview_binding_data(A, persona.id, dataset.id)

    def test_invalid_column_in_binding_keys(self, world, setup):
        persona, dataset = setup
        _bind(world, persona, dataset, {"invalid_col": "value"})
        with raises_code(PersonaManagerError, ErrorCode.DATASET_OPERATION_FAILED):
            world.manager().preview_binding_data(A, persona.id, dataset.id)

    def test_unsafe_column_name_rejected(self, world, setup):
        """SQLインジェクションを含むカラム名が拒否される"""
        persona, dataset = setup
        world.datasets[dataset.id] = replace(
            dataset,
            columns=[
                *dataset.columns,
                DatasetColumn(name='a" OR 1=1--', data_type="string"),
            ],
        )
        _bind(world, persona, dataset, {'a" OR 1=1--': "value"})
        with raises_code(PersonaManagerError, ErrorCode.DATASET_OPERATION_FAILED):
            world.manager().preview_binding_data(A, persona.id, dataset.id)
        world.preview.execute_dataset_preview.assert_not_called()

    def test_successful_preview(self, world, setup):
        persona, dataset = setup
        _bind(world, persona, dataset, {"user_id": "U123"})
        world.preview.execute_dataset_preview.return_value = (
            3,
            ["user_id", "product", "amount"],
            [["U123", "商品A", 1000], ["U123", "商品B", 2000], ["U123", "商品C", 500]],
        )

        result = world.manager().preview_binding_data(A, persona.id, dataset.id)

        assert result["columns"] == ["user_id", "product", "amount"]
        assert result["total_count"] == 3
        call = world.preview.execute_dataset_preview.call_args
        assert call.kwargs["s3_path"] == "s3://bucket/datasets/test.csv"
        assert '"user_id" = $1' in call.kwargs["count_sql"]
        assert call.kwargs["params"] == ["U123"]

    def test_empty_binding_keys(self, world, setup):
        """binding_keysが空の場合、フィルタなしで全件取得"""
        persona, dataset = setup
        _bind(world, persona, dataset, {})
        world.preview.execute_dataset_preview.return_value = (0, ["user_id"], [])

        result = world.manager().preview_binding_data(A, persona.id, dataset.id)

        assert result == {"columns": ["user_id"], "rows": [], "total_count": 0}
        call = world.preview.execute_dataset_preview.call_args
        assert "WHERE" not in call.kwargs["count_sql"]
        assert call.kwargs["params"] is None

    def test_preview_failure_is_transient(self, world, setup):
        persona, dataset = setup
        _bind(world, persona, dataset, {})
        world.preview.execute_dataset_preview.side_effect = RuntimeError("duckdb")
        with raises_code(PersonaManagerError, ErrorCode.DATASET_OPERATION_FAILED):
            world.manager().preview_binding_data(A, persona.id, dataset.id)
