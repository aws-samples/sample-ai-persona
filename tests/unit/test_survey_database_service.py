"""
Unit tests for DatabaseService Survey/SurveyTemplate CRUD operations.
"""

from unittest.mock import Mock, patch
from datetime import datetime

from src.services.database_service import DatabaseService
from src.models.survey_template import SurveyTemplate, Question
from src.models.survey import Survey, InsightReport


def _make_service():
    """Create a DatabaseService with a mocked boto3 client."""
    with patch("boto3.client") as mock_boto3:
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3.return_value = mock_client
        service = DatabaseService(table_prefix="Test")
    return service, mock_client


def _sample_template() -> SurveyTemplate:
    return SurveyTemplate(
        id="tmpl-001",
        name="顧客満足度調査",
        questions=[
            Question(id="q1", text="満足度は？", question_type="scale_rating"),
            Question(
                id="q2",
                text="好きな色は？",
                question_type="multiple_choice",
                options=["赤", "青", "緑"],
            ),
            Question(id="q3", text="ご意見をどうぞ", question_type="free_text"),
        ],
        created_at=datetime(2025, 1, 1, 12, 0, 0),
        updated_at=datetime(2025, 1, 2, 12, 0, 0),
    )


def _sample_survey() -> Survey:
    return Survey(
        id="srv-001",
        name="2025年1月調査",
        description="テスト調査",
        template_id="tmpl-001",
        persona_count=100,
        filters={"gender": "女性"},
        status="completed",
        s3_result_path="s3://bucket/results.csv",
        insight_report=InsightReport(
            id="rpt-001",
            survey_id="srv-001",
            content="インサイト内容",
            created_at=datetime(2025, 1, 3, 12, 0, 0),
        ),
        created_at=datetime(2025, 1, 1, 12, 0, 0),
        updated_at=datetime(2025, 1, 2, 12, 0, 0),
        error_message=None,
    )


class TestSurveyTemplateCRUD:
    """Test SurveyTemplate CRUD operations on DatabaseService."""

    def test_save_survey_template(self):
        service, mock_client = _make_service()
        mock_client.put_item.return_value = {}
        template = _sample_template()

        result = service.save_survey_template(template)

        assert result == "tmpl-001"
        mock_client.put_item.assert_called_once()
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_SurveyTemplates"

    def test_get_survey_template_found(self):
        service, mock_client = _make_service()
        template = _sample_template()
        serialized = service._serialize_survey_template(template)
        mock_client.get_item.return_value = {"Item": serialized}

        result = service.get_survey_template("tmpl-001")

        assert result is not None
        assert result.id == "tmpl-001"
        assert result.name == "顧客満足度調査"
        assert len(result.questions) == 3
        assert result.questions[1].options == ["赤", "青", "緑"]

    def test_get_survey_template_not_found(self):
        service, mock_client = _make_service()
        mock_client.get_item.return_value = {}

        result = service.get_survey_template("nonexistent")

        assert result is None

    def test_get_all_survey_templates(self):
        service, mock_client = _make_service()
        t1 = _sample_template()
        serialized = service._serialize_survey_template(t1)
        mock_client.scan.return_value = {"Items": [serialized]}

        result = service.get_all_survey_templates()

        assert len(result) == 1
        assert result[0].id == "tmpl-001"

    def test_update_survey_template(self):
        service, mock_client = _make_service()
        mock_client.put_item.return_value = {}
        template = _sample_template()

        result = service.update_survey_template(template)

        assert result is True
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_SurveyTemplates"

    def test_delete_survey_template(self):
        service, mock_client = _make_service()
        mock_client.delete_item.return_value = {}

        result = service.delete_survey_template("tmpl-001")

        assert result is True
        mock_client.delete_item.assert_called_once()

    def test_serialization_round_trip(self):
        """Verify serialize → deserialize produces equivalent object."""
        service, _ = _make_service()
        template = _sample_template()

        serialized = service._serialize_survey_template(template)
        restored = service._deserialize_survey_template(serialized)

        assert restored.id == template.id
        assert restored.name == template.name
        assert len(restored.questions) == len(template.questions)
        for orig, rest in zip(template.questions, restored.questions):
            assert orig.id == rest.id
            assert orig.text == rest.text
            assert orig.question_type == rest.question_type
            assert orig.options == rest.options
            assert orig.scale_min == rest.scale_min
            assert orig.scale_max == rest.scale_max
        assert restored.created_at == template.created_at
        assert restored.updated_at == template.updated_at


class TestSurveyCRUD:
    """Test Survey CRUD operations on DatabaseService."""

    def test_save_survey(self):
        service, mock_client = _make_service()
        mock_client.put_item.return_value = {}
        survey = _sample_survey()

        result = service.save_survey(survey)

        assert result == "srv-001"
        mock_client.put_item.assert_called_once()
        call_args = mock_client.put_item.call_args
        assert call_args[1]["TableName"] == "Test_Surveys"

    def test_get_survey_found(self):
        service, mock_client = _make_service()
        survey = _sample_survey()
        serialized = service._serialize_survey(survey)
        mock_client.get_item.return_value = {"Item": serialized}

        result = service.get_survey("srv-001")

        assert result is not None
        assert result.id == "srv-001"
        assert result.name == "2025年1月調査"
        assert result.persona_count == 100
        assert result.filters == {"gender": "女性"}
        assert result.insight_report is not None
        assert result.insight_report.content == "インサイト内容"

    def test_get_survey_not_found(self):
        service, mock_client = _make_service()
        mock_client.get_item.return_value = {}

        result = service.get_survey("nonexistent")

        assert result is None

    def test_get_all_surveys(self):
        service, mock_client = _make_service()
        survey = _sample_survey()
        serialized = service._serialize_survey(survey)
        mock_client.scan.return_value = {"Items": [serialized]}

        result = service.get_all_surveys()

        assert len(result) == 1
        assert result[0].id == "srv-001"

    def test_update_survey(self):
        service, mock_client = _make_service()
        mock_client.put_item.return_value = {}
        survey = _sample_survey()

        result = service.update_survey(survey)

        assert result is True

    def test_delete_survey(self):
        service, mock_client = _make_service()
        mock_client.delete_item.return_value = {}

        result = service.delete_survey("srv-001")

        assert result is True
        mock_client.delete_item.assert_called_once()

    def test_survey_without_optional_fields(self):
        """Test Survey with None optional fields serializes/deserializes correctly."""
        service, _ = _make_service()
        survey = Survey(
            id="srv-002",
            name="テスト",
            description="",
            template_id="tmpl-001",
            persona_count=10,
            filters=None,
            status="pending",
            s3_result_path=None,
            insight_report=None,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
            error_message=None,
        )

        serialized = service._serialize_survey(survey)
        restored = service._deserialize_survey(serialized)

        assert restored.id == "srv-002"
        assert restored.filters is None
        assert restored.s3_result_path is None
        assert restored.insight_report is None
        assert restored.error_message is None
        assert restored.persona_count == 10
