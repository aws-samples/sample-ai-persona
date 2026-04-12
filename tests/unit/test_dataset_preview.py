"""
DatasetManager.preview_binding_data 単体テスト
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
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
            mock_factory.get_database_service.return_value = mock_db
            mock_factory.get_s3_service.return_value = mock_s3
            from src.managers.dataset_manager import DatasetManager

            mgr = DatasetManager()
            mgr._mock_db = mock_db
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

    def test_invalid_column_in_binding_keys(self, dataset_manager, sample_binding, sample_dataset):
        """binding_keysに無効なカラム名がある場合ValueError"""
        sample_binding.binding_keys = {"invalid_col": "value"}
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset

        mock_conn = MagicMock()
        with patch.object(dataset_manager, "_create_duckdb_conn", return_value=mock_conn):
            with pytest.raises(ValueError, match="Invalid column name"):
                dataset_manager.preview_binding_data("persona-001", "bind-001")

    def test_unsafe_column_name_rejected(self, dataset_manager, sample_binding, sample_dataset):
        """SQLインジェクションを含むカラム名が拒否される"""
        sample_dataset.columns.append(DatasetColumn(name='a" OR 1=1--', data_type="string"))
        sample_binding.binding_keys = {'a" OR 1=1--': "value"}
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset

        mock_conn = MagicMock()
        with patch.object(dataset_manager, "_create_duckdb_conn", return_value=mock_conn):
            with pytest.raises(ValueError, match="Unsafe column name"):
                dataset_manager.preview_binding_data("persona-001", "bind-001")

    def test_successful_preview(self, dataset_manager, sample_binding, sample_dataset):
        """正常にプレビューデータを取得"""
        dataset_manager._mock_db.get_bindings_by_persona.return_value = [sample_binding]
        dataset_manager._mock_db.get_dataset.return_value = sample_dataset

        mock_conn = MagicMock()
        # COUNT結果
        mock_conn.execute.return_value.fetchone.return_value = [3]
        # データ結果
        mock_conn.execute.return_value.description = [
            ("user_id",), ("product",), ("amount",)
        ]
        mock_conn.execute.return_value.fetchall.return_value = [
            ("U123", "商品A", 1000),
            ("U123", "商品B", 2000),
            ("U123", "商品C", 500),
        ]

        with patch.object(dataset_manager, "_create_duckdb_conn", return_value=mock_conn):
            result = dataset_manager.preview_binding_data("persona-001", "bind-001")

        assert result["columns"] == ["user_id", "product", "amount"]
        assert len(result["rows"]) == 3
        assert result["total_count"] == 3
        mock_conn.close.assert_called_once()

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

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = [0]
        mock_conn.execute.return_value.description = [
            ("user_id",), ("product",), ("amount",)
        ]
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch.object(dataset_manager, "_create_duckdb_conn", return_value=mock_conn):
            result = dataset_manager.preview_binding_data("persona-001", "bind-002")

        assert result["rows"] == []
        assert result["total_count"] == 0


class TestCreateDuckdbConn:
    """_create_duckdb_connメソッドのテスト"""

    @pytest.fixture
    def dataset_manager(self):
        with patch("src.managers.dataset_manager.service_factory") as mock_factory:
            mock_s3 = Mock()
            mock_s3.region_name = "us-east-1"
            mock_factory.get_database_service.return_value = Mock()
            mock_factory.get_s3_service.return_value = mock_s3
            from src.managers.dataset_manager import DatasetManager

            return DatasetManager()

    def test_invalid_s3_uri_rejected(self, dataset_manager):
        """不正なS3 URIが拒否される"""
        with patch("src.managers.dataset_manager.duckdb") as mock_duckdb:
            mock_conn = MagicMock()
            mock_duckdb.connect.return_value = mock_conn

            with pytest.raises(ValueError, match="Invalid S3 URI"):
                dataset_manager._create_duckdb_conn("s3://bucket/path; DROP TABLE x")

    def test_invalid_local_path_rejected(self, dataset_manager):
        """パストラバーサルを含むローカルパスが拒否される"""
        with pytest.raises(ValueError, match="Invalid local path"):
            dataset_manager._create_duckdb_conn("local://../../etc/passwd")
