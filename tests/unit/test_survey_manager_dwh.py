"""
Unit tests for SurveyManager DWH segment extraction.
"""

import queue
from unittest.mock import Mock, patch

import pytest

from src.managers.survey_manager import (
    SurveyManager,
    SurveyExecutionError,
    SurveyValidationError,
)


@pytest.fixture
def mock_db() -> Mock:
    return Mock()


@pytest.fixture
def mock_survey_service() -> Mock:
    svc = Mock()
    svc.STANDARD_COLUMNS = {
        "persona": {"label": "ペルソナ概要", "required": True, "group": "プロフィール"},
        "sex": {"label": "性別", "required": False, "group": "属性"},
        "age": {"label": "年齢", "required": False, "group": "属性"},
        "occupation": {"label": "職業", "required": False, "group": "属性"},
        "region": {"label": "居住地域", "required": False, "group": "属性"},
        "prefecture": {"label": "都道府県", "required": False, "group": "属性"},
    }
    svc.parse_csv_columns.return_value = {
        "columns": ["id", "gender", "birth_year", "prefecture"],
        "samples": {
            "id": ["001", "002", "003"],
            "gender": ["女性", "男性", "女性"],
            "birth_year": ["1990", "1985", "1992"],
            "prefecture": ["東京都", "大阪府", "神奈川県"],
        },
        "row_count_preview": 3,
        "auto_mapping": {},
    }
    return svc


@pytest.fixture
def mock_ai_service() -> Mock:
    ai = Mock()
    ai._invoke_model.return_value = (
        '{"sex": "gender", "age": "birth_year", "prefecture": "prefecture"}'
    )
    return ai


@pytest.fixture
def manager(mock_db, mock_survey_service, mock_ai_service) -> SurveyManager:
    return SurveyManager(
        database_service=mock_db,
        survey_service=mock_survey_service,
        ai_service=mock_ai_service,
    )


@pytest.mark.unit
class TestExtractSegmentFromDwh:
    """extract_segment_from_dwh のテスト"""

    def test_empty_condition_raises_validation_error(self, manager):
        eq = queue.Queue()
        with pytest.raises(SurveyValidationError, match="抽出条件"):
            manager.extract_segment_from_dwh("", eq)

    @patch("src.config.config")
    def test_missing_runtime_arn_raises_execution_error(self, mock_config, manager):
        mock_config.DATA_AGENT_RUNTIME_ARN = None
        eq = queue.Queue()
        with pytest.raises(SurveyExecutionError, match="Runtime ARN"):
            manager.extract_segment_from_dwh("東京30代女性", eq)

    @patch("src.config.config")
    @patch("strands.Agent")
    @patch("strands.models.BedrockModel")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    @patch("src.services.data_agent_service.DataAgentService.download_csv")
    def test_successful_extraction(
        self,
        mock_download_csv,
        mock_create_tool,
        mock_bedrock_model,
        mock_agent_cls,
        mock_config,
        manager,
        mock_survey_service,
    ):
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/abc"
        )
        mock_config.DATA_AGENT_REGION = "ap-northeast-1"
        mock_config.BEDROCK_MODEL_ID = "anthropic.claude-sonnet-4-6"
        mock_config.AWS_REGION = "us-east-1"

        # create_data_agent_tool receives a capture_queue; simulate csv_url event
        def fake_create_tool(arn, region, event_queue=None):
            if event_queue is not None:
                event_queue.put(
                    {
                        "type": "csv_url",
                        "url": "https://s3.ap-northeast-1.amazonaws.com/bucket/export.csv",
                    }
                )
            return Mock()

        mock_create_tool.side_effect = fake_create_tool
        mock_agent_instance = Mock()
        mock_agent_cls.return_value = mock_agent_instance

        csv_content = b"id,gender,birth_year,prefecture\n001,F,1990,Tokyo\n" * 150
        mock_download_csv.return_value = csv_content
        mock_survey_service.count_csv_rows.return_value = 150

        eq = queue.Queue()

        result = manager.extract_segment_from_dwh("東京30代女性", eq)

        assert result["csv_bytes"] == csv_content
        assert result["row_count"] == 150
        assert result["columns"] == ["id", "gender", "birth_year", "prefecture"]
        mock_agent_instance.assert_called_once()

    @patch("src.config.config")
    @patch("strands.Agent")
    @patch("strands.models.BedrockModel")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    @patch("src.services.data_agent_service.DataAgentService.download_csv")
    def test_too_few_rows_raises_validation_error(
        self,
        mock_download_csv,
        mock_create_tool,
        mock_bedrock_model,
        mock_agent_cls,
        mock_config,
        manager,
        mock_survey_service,
    ):
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/abc"
        )
        mock_config.DATA_AGENT_REGION = "ap-northeast-1"
        mock_config.BEDROCK_MODEL_ID = "anthropic.claude-sonnet-4-6"
        mock_config.AWS_REGION = "us-east-1"

        def fake_create_tool(arn, region, event_queue=None):
            if event_queue is not None:
                event_queue.put(
                    {
                        "type": "csv_url",
                        "url": "https://s3.ap-northeast-1.amazonaws.com/bucket/export.csv",
                    }
                )
            return Mock()

        mock_create_tool.side_effect = fake_create_tool
        mock_agent_cls.return_value = Mock()

        csv_content = b"id,gender\n001,F\n002,M\n"
        mock_download_csv.return_value = csv_content
        mock_survey_service.count_csv_rows.return_value = 50

        eq = queue.Queue()

        with pytest.raises(SurveyValidationError, match="100件以上"):
            manager.extract_segment_from_dwh("テスト条件", eq)

    @patch("src.config.config")
    @patch("strands.Agent")
    @patch("strands.models.BedrockModel")
    @patch("src.services.data_agent_service.create_data_agent_tool")
    def test_no_csv_url_raises_execution_error(
        self,
        mock_create_tool,
        mock_bedrock_model,
        mock_agent_cls,
        mock_config,
        manager,
    ):
        mock_config.DATA_AGENT_RUNTIME_ARN = (
            "arn:aws:bedrock:us-east-1:123:agent-runtime/abc"
        )
        mock_config.DATA_AGENT_REGION = "ap-northeast-1"
        mock_config.BEDROCK_MODEL_ID = "anthropic.claude-sonnet-4-6"
        mock_config.AWS_REGION = "us-east-1"

        mock_create_tool.return_value = Mock()
        mock_agent_cls.return_value = Mock()

        eq = queue.Queue()

        with pytest.raises(SurveyExecutionError, match="CSV"):
            manager.extract_segment_from_dwh("テスト条件", eq)


@pytest.mark.unit
class TestSuggestColumnMapping:
    """suggest_column_mapping のテスト"""

    def test_returns_valid_mapping(self, manager, mock_ai_service):
        mock_ai_service._invoke_model.return_value = (
            '{"mapping": {"sex": "gender", "age": "birth_year", "prefecture": "prefecture"}, '
            '"extra_columns": [{"csv_column": "id", "label": "顧客ID", "description": "一意識別子"}]}'
        )
        columns = ["id", "gender", "birth_year", "prefecture"]
        samples = {
            "id": ["001", "002"],
            "gender": ["女性", "男性"],
            "birth_year": ["1990", "1985"],
            "prefecture": ["東京都", "大阪府"],
        }

        result = manager.suggest_column_mapping(columns, samples)

        assert result["mapping"] == {
            "sex": "gender",
            "age": "birth_year",
            "prefecture": "prefecture",
        }
        assert len(result["extra_columns"]) == 1
        assert result["extra_columns"][0]["csv_column"] == "id"

    def test_returns_empty_when_no_ai_service(self, mock_db, mock_survey_service):
        mgr = SurveyManager(
            database_service=mock_db,
            survey_service=mock_survey_service,
            ai_service=None,
        )
        result = mgr.suggest_column_mapping(["col1"], {"col1": ["val"]})
        assert result == {"mapping": {}, "extra_columns": []}

    def test_handles_invalid_json_gracefully(self, manager, mock_ai_service):
        mock_ai_service._invoke_model.return_value = "not valid json"
        result = manager.suggest_column_mapping(["col1"], {"col1": ["val"]})
        assert result == {"mapping": {}, "extra_columns": []}

    def test_filters_invalid_columns(self, manager, mock_ai_service):
        mock_ai_service._invoke_model.return_value = (
            '{"mapping": {"sex": "gender", "invalid_key": "col1"}, "extra_columns": []}'
        )
        columns = ["gender", "col1"]
        result = manager.suggest_column_mapping(
            columns, {"gender": ["M"], "col1": ["x"]}
        )
        assert "invalid_key" not in result["mapping"]
        assert result["mapping"] == {"sex": "gender"}
