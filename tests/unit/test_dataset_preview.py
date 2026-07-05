"""
DatasetManager.preview_binding_data 単体テスト
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime

from src.models.dataset import Dataset, DatasetColumn, PersonaDatasetBinding


class TestPreviewBindingData:
    """preview_binding_dataメソッドのテスト"""

    @pytest.fixture
    def dataset_manager(self):
        with patch("src.managers.dataset_manager.service_factory") as mock_factory:
            mock_db = Mock()
            mock_s3 = Mock()
            mock_s3.region_name = "us-east-1"
            mock_batch = Mock()
            mock_factory.get_database_service.return_value = mock_db
            mock_factory.get_s3_service.return_value = mock_s3
            mock_factory.get_survey_batch_service.return_value = mock_batch
            from src.managers.dataset_manager import DatasetManager

            mgr = DatasetManager()
            mgr._mock_db = mock_db
            mgr._mock_batch = mock_batch
            return mgr

    @pytest.fixture
    def sample_dataset(self):
        return Dataset(
            id="ds-001",
            name="購買データ",
            description="",
            s3_path="s3://bucket/datasets/test.csv",
            columns=[
                DatasetColumn(name="user_id", data_type="string"),
                DatasetColumn(name="product", data_type="string"),
                DatasetColumn(name="amount", data_type="integer"),
            ],
            row_count=100,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    @pytest.fixture
    def sample_binding(self):
        return PersonaDatasetBinding(
            id="bind-001",
            persona_id="persona-001",
            dataset_id="ds-001",
            binding_keys={"user_id": "U123"},
            created_at=datetime.now(),
        )

    def test_binding_not_found(self, dataset_manager):
        """存在しないbinding_idでValueError"""
        dataset_manager._mock_db.get_bindings_by_persona.return_value = []

        with pytest.raises(ValueError, match="Binding not found"):
            dataset_manager.preview_binding_data("persona-001", "nonexistent")

    def test_dataset_not_found(self, dataset_manager, sample_binding):
        """データセットが見つからない場合ValueError"""
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = None

        with pytest.raises(ValueError, match="Dataset not found"):
            dataset_manager.preview_binding_data("persona-001", "bind-001")

    def test_invalid_column_in_binding_keys(
        self, dataset_manager, sample_binding, sample_dataset
    ):
        """binding_keysに無効なカラム名がある場合ValueError"""
        sample_binding.binding_keys = {"invalid_col": "value"}
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset

        with pytest.raises(ValueError, match="Invalid column name"):
            dataset_manager.preview_binding_data("persona-001", "bind-001")

    def test_unsafe_column_name_rejected(
        self, dataset_manager, sample_binding, sample_dataset
    ):
        """SQLインジェクションを含むカラム名が拒否される"""
        sample_dataset.columns.append(
            DatasetColumn(name='a" OR 1=1--', data_type="string")
        )
        sample_binding.binding_keys = {'a" OR 1=1--': "value"}
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset

        with pytest.raises(ValueError, match="Unsafe column name"):
            dataset_manager.preview_binding_data("persona-001", "bind-001")

    def test_successful_preview(self, dataset_manager, sample_binding, sample_dataset):
        """正常にプレビューデータを取得"""
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset
        dataset_manager._mock_batch.execute_dataset_preview.return_value = (
            3,
            ["user_id", "product", "amount"],
            [
                ["U123", "商品A", 1000],
                ["U123", "商品B", 2000],
                ["U123", "商品C", 500],
            ],
        )

        result = dataset_manager.preview_binding_data("persona-001", "bind-001")

        assert result["columns"] == ["user_id", "product", "amount"]
        assert len(result["rows"]) == 3
        assert result["total_count"] == 3

        call_args = dataset_manager._mock_batch.execute_dataset_preview.call_args
        assert call_args.kwargs["s3_path"] == "s3://bucket/datasets/test.csv"
        assert '"user_id" = $1' in call_args.kwargs["count_sql"]
        assert call_args.kwargs["params"] == ["U123"]

    def test_empty_binding_keys(self, dataset_manager, sample_dataset):
        """binding_keysが空の場合、フィルタなしで全件取得"""
        binding = PersonaDatasetBinding(
            id="bind-002",
            persona_id="persona-001",
            dataset_id="ds-001",
            binding_keys={},
            created_at=datetime.now(),
        )
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset
        dataset_manager._mock_batch.execute_dataset_preview.return_value = (
            0,
            ["user_id", "product", "amount"],
            [],
        )

        result = dataset_manager.preview_binding_data("persona-001", "bind-002")

        assert result["rows"] == []
        assert result["total_count"] == 0

        call_args = dataset_manager._mock_batch.execute_dataset_preview.call_args
        assert "WHERE" not in call_args.kwargs["count_sql"]
        assert call_args.kwargs["params"] is None
