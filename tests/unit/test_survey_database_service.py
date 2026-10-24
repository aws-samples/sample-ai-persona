"""
Unit tests for DatabaseService Survey CRUD operations.
"""

from unittest.mock import Mock, patch
from datetime import datetime

import pytest

from src.services.database_service import DatabaseConditionFailed, DatabaseService
from src.models.survey_template import SurveyTemplate, Question
from src.models.survey import InsightReport, Survey, TemplateSnapshot


def _make_service():
    """Create a DatabaseService with a mocked boto3 client."""
    with patch("boto3.client") as mock_boto3:
        mock_client = Mock()
        mock_client.list_tables.return_value = {"TableNames": []}
        mock_boto3.return_value = mock_client
        service = DatabaseService(table_prefix="Test")
    return service, mock_client


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
        error_code=None,
    )


class TestSurveyCRUD:
    """Test Survey CRUD operations on DatabaseService."""

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
            error_code=None,
        )

        serialized = service._serialize_survey(survey)
        restored = service._deserialize_survey(serialized)

        assert restored.id == "srv-002"
        assert restored.filters is None
        assert restored.s3_result_path is None
        assert restored.insight_report is None
        assert restored.error_code is None
        assert restored.persona_count == 10


# =============================================================================
# U7: 作成のトランザクション・部分更新・まとめ読み（BR1.1、BR1.5、NFR1.77、NFR1.83、
# NFR1.87、NFR2.21）。moto を使う
# =============================================================================


def _owner_index() -> dict:
    return {
        "IndexName": "OwnerIndex",
        "KeySchema": [
            {"AttributeName": "owner_sub", "KeyType": "HASH"},
            {"AttributeName": "created_at", "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "KEYS_ONLY"},
    }


@pytest.fixture
def moto_survey_service(monkeypatch):
    moto = pytest.importorskip("moto")
    import boto3

    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        service = DatabaseService(table_prefix="SvTest", region="us-east-1")
        client = boto3.client("dynamodb", region_name="us-east-1")
        for table in (service.surveys_table,):
            client.create_table(
                TableName=table,
                KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"},
                    {"AttributeName": "owner_sub", "AttributeType": "S"},
                    {"AttributeName": "created_at", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
                GlobalSecondaryIndexes=[_owner_index()],
            )
        yield service


def _raw(service: DatabaseService, table: str, item_id: str) -> dict | None:
    return service.dynamodb_client.get_item(
        TableName=table, Key={"id": {"S": item_id}}
    ).get("Item")


def _owned_template(owner: str = "user-a") -> SurveyTemplate:
    return SurveyTemplate.create_new(
        name="t", questions=[Question.create_free_text("Q")]
    ).owned_by(owner)


def _owned_survey(owner: str = "user-a") -> Survey:
    template = _owned_template(owner)
    return Survey.create_new(
        name="s",
        description="",
        template_id=template.id,
        persona_count=100,
        datasource="nemotron",
        owner_sub=owner,
        template_snapshot=TemplateSnapshot.from_template(template),
    )


def _share(service: DatabaseService, table: str, item_id: str) -> None:
    service.dynamodb_client.update_item(
        TableName=table,
        Key={"id": {"S": item_id}},
        UpdateExpression="SET shared_group_ids = :s",
        ExpressionAttributeValues={":s": {"SS": ["sales"]}},
    )


class TestSurveyWrites:
    def test_put_new_writes_owner_and_snapshot_without_shares(
        self, moto_survey_service
    ):
        survey = _owned_survey()
        moto_survey_service.put_new_survey(survey)
        stored = moto_survey_service.get_survey(survey.id, consistent=True)
        assert stored.owner_sub == "user-a"
        assert stored.template_snapshot == survey.template_snapshot
        item = _raw(moto_survey_service, moto_survey_service.surveys_table, survey.id)
        assert "shared_group_ids" not in item

    def test_put_new_refuses_an_existing_id(self, moto_survey_service):
        survey = _owned_survey()
        moto_survey_service.put_new_survey(survey)
        with pytest.raises(DatabaseConditionFailed):
            moto_survey_service.put_new_survey(survey)

    def test_update_fields_writes_only_those_columns(self, moto_survey_service):
        survey = _owned_survey()
        moto_survey_service.put_new_survey(survey)
        _share(moto_survey_service, moto_survey_service.surveys_table, survey.id)

        updated = moto_survey_service.update_survey_fields(
            survey.id,
            {
                "status": "completed",
                "s3_result_path": "s3://b/survey-results/x/results.csv",
                "persona_count": 120,
                "updated_at": "2030-01-01T00:00:00",
            },
            remove=("error_code",),
        )

        assert updated is True
        stored = moto_survey_service.get_survey(survey.id, consistent=True)
        assert stored.status == "completed"
        assert stored.persona_count == 120
        assert stored.owner_sub == "user-a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == survey.created_at
        assert stored.template_snapshot == survey.template_snapshot

    def test_update_fields_of_a_deleted_survey_creates_nothing(
        self, moto_survey_service
    ):
        assert (
            moto_survey_service.update_survey_fields("gone", {"status": "error"})
            is False
        )
        assert (
            _raw(moto_survey_service, moto_survey_service.surveys_table, "gone") is None
        )

    @pytest.mark.parametrize(
        "column", ["owner_sub", "shared_group_ids", "created_at", "id"]
    )
    def test_update_fields_refuses_owner_shares_and_keys(
        self, moto_survey_service, column
    ):
        with pytest.raises(ValueError):
            moto_survey_service.update_survey_fields("x", {column: "y"})

    def test_insight_report_is_written_as_a_map(self, moto_survey_service):
        survey = _owned_survey()
        moto_survey_service.put_new_survey(survey)
        report = InsightReport.create_new(survey.id, "内容")
        moto_survey_service.update_survey_fields(
            survey.id, {"insight_report": report.to_dict()}
        )
        stored = moto_survey_service.get_survey(survey.id, consistent=True)
        assert stored.insight_report.content == "内容"

    def test_batch_read(self, moto_survey_service):
        surveys = [_owned_survey() for _ in range(3)]
        for survey in surveys:
            moto_survey_service.put_new_survey(survey)
        found = moto_survey_service.get_surveys_by_ids([s.id for s in surveys])
        assert set(found) == {s.id for s in surveys}

    def test_item_size_grows_with_the_body(self):
        small = DatabaseService.item_size({"id": "a"})
        large = DatabaseService.item_size({"id": "a", "text": "あ" * 1000})
        assert small < large
        assert large >= 3000
