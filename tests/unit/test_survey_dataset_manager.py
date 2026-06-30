"""SurveyDatasetManager のユニットテスト"""

import json
from unittest.mock import Mock

import pytest

from src.managers.survey_dataset_manager import (
    SurveyDatasetManager,
    SurveyDatasetValidationError,
)


@pytest.fixture
def mock_batch_service() -> Mock:
    svc = Mock()
    svc._parquet_s3_uri = "s3://bucket/key.parquet"
    return svc


@pytest.fixture
def mock_agent_service() -> Mock:
    return Mock()


@pytest.fixture
def mock_ai_service() -> Mock:
    return Mock()


class _FakeClientError(Exception):
    pass


@pytest.fixture
def mock_s3_service() -> Mock:
    svc = Mock()
    svc.bucket_name = "test-bucket"
    svc.region_name = "us-east-1"
    svc.s3_client.exceptions.ClientError = _FakeClientError
    return svc


@pytest.fixture
def mgr(
    mock_batch_service: Mock,
    mock_agent_service: Mock,
    mock_ai_service: Mock,
    mock_s3_service: Mock,
) -> SurveyDatasetManager:
    return SurveyDatasetManager(
        survey_batch_service=mock_batch_service,
        agent_service=mock_agent_service,
        ai_service=mock_ai_service,
        s3_service=mock_s3_service,
    )


class TestCheckNemotronStatus:
    def test_exists(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mock_s3_service.s3_client.head_object.return_value = {
            "ContentLength": 1024 * 1024 * 50
        }
        result = mgr.check_nemotron_status()
        assert result["exists"] is True
        assert result["size_mb"] == 50.0

    def test_not_exists(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mock_s3_service.s3_client.head_object.side_effect = _FakeClientError(
            "not found"
        )
        result = mgr.check_nemotron_status()
        assert result["exists"] is False


class TestDownloadNemotronDataset:
    def test_success(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock, mock_s3_service: Mock
    ) -> None:
        mock_batch_service.download_nemotron_dataset.return_value = b"parquet_data"
        mock_s3_service.s3_client.head_object.return_value = {
            "ContentLength": 1024 * 1024 * 10
        }
        result = mgr.download_nemotron_dataset()
        mock_s3_service.upload_file.assert_called_once()
        mock_batch_service.set_parquet_s3_uri.assert_called_once()
        assert result["exists"] is True


class TestUploadCustomDataset:
    def test_success(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock, mock_s3_service: Mock
    ) -> None:
        import polars as pl

        df = pl.DataFrame({"persona": ["a"], "uuid": ["u1"]})
        mock_batch_service.convert_csv_to_parquet.return_value = (df, b"pq_bytes")

        result = mgr.upload_custom_dataset(b"csv_data", "test.csv", {"persona": "col1"})
        assert result["name"] == "test"
        assert result["row_count"] == 1
        mock_s3_service.upload_file.assert_called()


class TestDeleteCustomDataset:
    def test_success(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock, mock_batch_service: Mock
    ) -> None:
        mgr.delete_custom_dataset("mydata")
        mock_s3_service.s3_client.delete_object.assert_called()
        mock_batch_service.invalidate_datasource.assert_called_once_with(
            "custom:mydata"
        )


class TestLoadDatasetMetadata:
    def test_found(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        meta = {"standard_mapping": {}, "extra_columns": []}
        mock_s3_service.download_file.return_value = json.dumps(meta).encode()
        result = mgr.load_dataset_metadata("test")
        assert result == meta

    def test_not_found(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mock_s3_service.download_file.side_effect = Exception("not found")
        result = mgr.load_dataset_metadata("missing")
        assert result is None


class TestParseCsvColumns:
    def test_returns_dict(self, mgr: SurveyDatasetManager) -> None:
        csv_data = b"name,age\nAlice,30\nBob,25"
        result = mgr.parse_csv_columns(csv_data)
        assert "columns" in result
        assert "samples" in result


class TestPreviewSystemPrompt:
    def test_returns_string(self, mgr: SurveyDatasetManager) -> None:
        csv_data = "sex,age,persona\n男性,30,テスト概要".encode("utf-8-sig")
        result = mgr.preview_system_prompt(
            csv_data, {"sex": "sex", "age": "age", "persona": "persona"}
        )
        assert "男性" in result
        assert "テスト概要" in result


class TestGetFilteredCount:
    def test_delegates_to_batch_service(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock
    ) -> None:
        mock_batch_service.get_filtered_count.return_value = 500
        result = mgr.get_filtered_count({"性別": "男性"}, datasource="nemotron")
        assert result == 500


class TestGenerateDatasetName:
    def test_ai_success(self, mgr: SurveyDatasetManager, mock_ai_service: Mock) -> None:
        mock_ai_service.invoke_model.return_value = "テストデータセット"
        result = mgr._generate_dataset_name("20代女性")
        assert result == "テストデータセット"

    def test_ai_failure_fallback(
        self, mgr: SurveyDatasetManager, mock_ai_service: Mock
    ) -> None:
        mock_ai_service.invoke_model.side_effect = Exception("API error")
        result = mgr._generate_dataset_name("20代女性の購買行動データ")
        assert len(result) <= 20


class TestValidateSegmentRowCount:
    def test_too_few(self, mgr: SurveyDatasetManager) -> None:
        with pytest.raises(SurveyDatasetValidationError):
            mgr._validate_segment_row_count(50)

    def test_too_many(self, mgr: SurveyDatasetManager) -> None:
        with pytest.raises(SurveyDatasetValidationError):
            mgr._validate_segment_row_count(20000)

    def test_valid(self, mgr: SurveyDatasetManager) -> None:
        mgr._validate_segment_row_count(500)


class TestTempFileOperations:
    def test_upload(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mgr.upload_temp_file(b"data", "tmp/key")
        mock_s3_service.upload_file.assert_called_once_with(b"data", "tmp/key")

    def test_download(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mock_s3_service.download_file.return_value = b"data"
        result = mgr.download_temp_file("tmp/key")
        assert result == b"data"

    def test_delete(self, mgr: SurveyDatasetManager, mock_s3_service: Mock) -> None:
        mgr.delete_temp_file("tmp/key")
        mock_s3_service.s3_client.delete_object.assert_called_once()


# =========================================================================
# 以下、カバレッジ拡充テスト (lines 96-128, 180-181, 329-359, 390, 408-409,
# 426-427, 431-432, 440, 450)
# =========================================================================


@pytest.mark.unit
class TestEnsureParquetUri:
    """_ensure_parquet_uri のテスト (lines 96-105)"""

    def test_success_sets_uri(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock, mock_batch_service: Mock
    ) -> None:
        mock_s3_service.s3_client.head_object.return_value = {"ContentLength": 100}
        mgr._ensure_parquet_uri()
        mock_batch_service.set_parquet_s3_uri.assert_called_once_with(
            "s3://test-bucket/persona-dataset/nemotron-personas-japan.parquet"
        )

    def test_not_found_raises_error(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock
    ) -> None:
        from src.managers.survey_dataset_manager import SurveyDatasetManagerError

        mock_s3_service.s3_client.head_object.side_effect = _FakeClientError(
            "not found"
        )
        with pytest.raises(
            SurveyDatasetManagerError, match="まだダウンロードされていません"
        ):
            mgr._ensure_parquet_uri()


@pytest.mark.unit
class TestListCustomDatasets:
    """list_custom_datasets のテスト (lines 111-128)"""

    def test_returns_parquet_files_only(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock
    ) -> None:
        mock_s3_service.s3_client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "persona-dataset/custom/test.parquet", "Size": 1048576},
                {"Key": "persona-dataset/custom/test.meta.json", "Size": 100},
                {"Key": "persona-dataset/custom/other.parquet", "Size": 2097152},
            ]
        }
        result = mgr.list_custom_datasets()
        assert len(result) == 2
        assert result[0]["name"] == "test"
        assert result[0]["size_mb"] == 1.0
        assert result[1]["name"] == "other"
        assert result[1]["size_mb"] == 2.0

    def test_empty_bucket(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock
    ) -> None:
        mock_s3_service.s3_client.list_objects_v2.return_value = {}
        result = mgr.list_custom_datasets()
        assert result == []

    def test_exception_returns_empty(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock
    ) -> None:
        mock_s3_service.s3_client.list_objects_v2.side_effect = Exception("S3 error")
        result = mgr.list_custom_datasets()
        assert result == []


@pytest.mark.unit
class TestDeleteCustomDatasetMetaError:
    """delete_custom_dataset のメタデータ削除例外パス (lines 180-181)"""

    def test_meta_delete_exception_is_swallowed(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock, mock_batch_service: Mock
    ) -> None:
        call_count = [0]

        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 2:
                raise Exception("meta delete failed")

        mock_s3_service.s3_client.delete_object.side_effect = side_effect
        mgr.delete_custom_dataset("mydata")
        mock_batch_service.invalidate_datasource.assert_called_once_with(
            "custom:mydata"
        )


@pytest.mark.unit
class TestSuggestColumnMapping:
    """suggest_column_mapping のテスト (lines 329-359)"""

    def test_valid_result_filters_correctly(
        self, mgr: SurveyDatasetManager, mock_agent_service: Mock
    ) -> None:
        mock_agent_service.suggest_column_mapping_with_llm.return_value = {
            "mapping": {"sex": "gender_col", "age": "age_col", "invalid_key": "x"},
            "extra_columns": [
                {"csv_column": "hobby_col", "label": "趣味"},
                {"csv_column": "gender_col", "label": "性別"},  # 既にmappingで使用済み
                {"csv_column": "missing_col", "label": "存在しない"},
            ],
        }
        columns = ["gender_col", "age_col", "hobby_col"]
        samples = {"gender_col": ["男性"], "age_col": ["30"], "hobby_col": ["読書"]}
        result = mgr.suggest_column_mapping(columns, samples)

        assert result["mapping"] == {"sex": "gender_col", "age": "age_col"}
        assert len(result["extra_columns"]) == 1
        assert result["extra_columns"][0]["csv_column"] == "hobby_col"

    def test_exception_returns_empty(
        self, mgr: SurveyDatasetManager, mock_agent_service: Mock
    ) -> None:
        mock_agent_service.suggest_column_mapping_with_llm.side_effect = Exception(
            "LLM error"
        )
        result = mgr.suggest_column_mapping(["col1"], {"col1": ["val"]})
        assert result == {"mapping": {}, "extra_columns": []}

    def test_no_agent_service_returns_empty(
        self, mock_batch_service: Mock, mock_ai_service: Mock, mock_s3_service: Mock
    ) -> None:
        mgr_no_agent = SurveyDatasetManager(
            survey_batch_service=mock_batch_service,
            agent_service=None,
            ai_service=mock_ai_service,
            s3_service=mock_s3_service,
        )
        mgr_no_agent.agent_service = None
        result = mgr_no_agent.suggest_column_mapping(["col1"], {"col1": ["val"]})
        assert result == {"mapping": {}, "extra_columns": []}


@pytest.mark.unit
class TestGetAvailableFilterValues:
    """get_available_filter_values のテスト (lines 408-409)"""

    def test_delegates_to_batch_service(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock
    ) -> None:
        expected = {"性別": {"values": ["男性", "女性"]}}
        mock_batch_service.has_parquet_uri.return_value = True
        mock_batch_service.get_available_filter_values.return_value = expected
        result = mgr.get_available_filter_values("nemotron")
        assert result == expected
        mock_batch_service.get_available_filter_values.assert_called_once_with(
            "nemotron"
        )


@pytest.mark.unit
class TestGetPreviewStats:
    """get_preview_stats のテスト (lines 426-427)"""

    def test_delegates_to_batch_service(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock
    ) -> None:
        expected = {"total": 1000, "filtered": 500}
        mock_batch_service.has_parquet_uri.return_value = True
        mock_batch_service.get_preview_stats.return_value = expected
        result = mgr.get_preview_stats({"性別": "男性"}, datasource="nemotron")
        assert result == expected
        mock_batch_service.get_preview_stats.assert_called_once_with(
            {"性別": "男性"}, "nemotron"
        )


@pytest.mark.unit
class TestGetDatasourceCount:
    """get_datasource_count のテスト (lines 431-432)"""

    def test_delegates_to_batch_service(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = True
        mock_batch_service.get_total_count.return_value = 2000
        result = mgr.get_datasource_count("nemotron")
        assert result == 2000
        mock_batch_service.get_total_count.assert_called_once_with("nemotron")


@pytest.mark.unit
class TestGetImagePresignedUrl:
    """get_image_presigned_url のテスト (line 440)"""

    def test_delegates_to_s3_service(
        self, mgr: SurveyDatasetManager, mock_s3_service: Mock
    ) -> None:
        mock_s3_service.generate_presigned_url.return_value = "https://presigned.url"
        result = mgr.get_image_presigned_url("images/test.png")
        assert result == "https://presigned.url"
        mock_s3_service.generate_presigned_url.assert_called_once_with(
            "images/test.png"
        )


@pytest.mark.unit
class TestEnsureParquetUriIfNemotron:
    """_ensure_parquet_uri_if_nemotron のテスト (lines 446-450)"""

    def test_custom_datasource_does_nothing(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock, mock_s3_service: Mock
    ) -> None:
        mgr._ensure_parquet_uri_if_nemotron("custom:mydata")
        mock_batch_service.has_parquet_uri.assert_not_called()
        mock_s3_service.s3_client.head_object.assert_not_called()

    def test_nemotron_with_uri_does_nothing(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock, mock_s3_service: Mock
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = True
        mgr._ensure_parquet_uri_if_nemotron("nemotron")
        mock_s3_service.s3_client.head_object.assert_not_called()

    def test_nemotron_without_uri_calls_ensure(
        self, mgr: SurveyDatasetManager, mock_batch_service: Mock, mock_s3_service: Mock
    ) -> None:
        mock_batch_service.has_parquet_uri.return_value = False
        mock_s3_service.s3_client.head_object.return_value = {"ContentLength": 100}
        mgr._ensure_parquet_uri_if_nemotron("nemotron")
        mock_batch_service.set_parquet_s3_uri.assert_called_once()


@pytest.mark.unit
class TestGetStandardColumns:
    """get_standard_columns のテスト (line 390)"""

    def test_returns_dict(self, mgr: SurveyDatasetManager) -> None:
        result = mgr.get_standard_columns()
        assert isinstance(result, dict)
        assert "sex" in result or "age" in result or len(result) > 0
