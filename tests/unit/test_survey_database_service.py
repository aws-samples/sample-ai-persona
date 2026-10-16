"""
Unit tests for DatabaseService Survey/SurveyTemplate CRUD operations.
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
        error_code=None,
    )


class TestSurveyTemplateCRUD:
    """Test SurveyTemplate CRUD operations on DatabaseService."""

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
        for table in (service.survey_templates_table, service.surveys_table):
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


class TestTemplateWrites:
    def test_put_new_writes_owner_without_shares_and_lands_in_owner_index(
        self, moto_survey_service
    ):
        template = _owned_template()
        template = SurveyTemplate.from_dict(
            {**template.to_dict(), "shared_group_ids": ["sales"]}
        )
        moto_survey_service.put_new_survey_template(template)

        item = _raw(
            moto_survey_service, moto_survey_service.survey_templates_table, template.id
        )
        assert item["owner_sub"] == {"S": "user-a"}
        assert "shared_group_ids" not in item
        owned = moto_survey_service.dynamodb_client.query(
            TableName=moto_survey_service.survey_templates_table,
            IndexName="OwnerIndex",
            KeyConditionExpression="owner_sub = :o",
            ExpressionAttributeValues={":o": {"S": "user-a"}},
        )["Items"]
        assert [i["id"]["S"] for i in owned] == [template.id]

    def test_put_new_refuses_an_existing_id(self, moto_survey_service):
        template = _owned_template()
        moto_survey_service.put_new_survey_template(template)
        with pytest.raises(DatabaseConditionFailed):
            moto_survey_service.put_new_survey_template(template.owned_by("other"))
        item = _raw(
            moto_survey_service, moto_survey_service.survey_templates_table, template.id
        )
        assert item["owner_sub"] == {"S": "user-a"}

    def test_put_new_writes_the_callers_items_in_the_same_transaction(
        self, moto_survey_service
    ):
        template = _owned_template()
        extra = {
            "Put": {
                "TableName": moto_survey_service.survey_templates_table,
                "Item": {"id": {"S": "extra-item"}},
                "ConditionExpression": "attribute_not_exists(id)",
            }
        }
        moto_survey_service.put_new_survey_template(template, extra_items=[extra])
        assert _raw(
            moto_survey_service,
            moto_survey_service.survey_templates_table,
            "extra-item",
        )
        # 2 回目は本体も追加の項目も書かれない
        second = _owned_template()
        with pytest.raises(DatabaseConditionFailed):
            moto_survey_service.put_new_survey_template(second, extra_items=[extra])
        assert (
            _raw(
                moto_survey_service,
                moto_survey_service.survey_templates_table,
                second.id,
            )
            is None
        )

    def test_update_content_keeps_owner_shares_and_created_at(
        self, moto_survey_service
    ):
        template = _owned_template()
        moto_survey_service.put_new_survey_template(template)
        _share(
            moto_survey_service, moto_survey_service.survey_templates_table, template.id
        )
        edited = SurveyTemplate.from_dict(
            {
                **{
                    k: v
                    for k, v in template.to_dict().items()
                    if k not in ("owner_sub", "created_at")
                },
                "created_at": "2030-01-01T00:00:00",
            }
        ).edited(name="編集後", questions=[Question.create_free_text("Q2")], images=[])

        spy = Mock(wraps=moto_survey_service.dynamodb_client.put_item)
        moto_survey_service.dynamodb_client.put_item = spy
        assert moto_survey_service.update_survey_template_content(edited) is True
        assert spy.call_count == 0

        stored = moto_survey_service.get_survey_template(template.id, consistent=True)
        assert stored.name == "編集後"
        assert stored.owner_sub == "user-a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == template.created_at

    def test_update_content_of_a_deleted_template_creates_nothing(
        self, moto_survey_service
    ):
        template = _owned_template()
        assert moto_survey_service.update_survey_template_content(template) is False
        assert (
            _raw(
                moto_survey_service,
                moto_survey_service.survey_templates_table,
                template.id,
            )
            is None
        )

    def test_update_content_removes_emptied_images(self, moto_survey_service):
        from src.models.survey_template import TemplateImage

        image = TemplateImage.create_new("img", "s3://b/k.png", "image/png", "k.png")
        template = SurveyTemplate.create_new(
            name="t", questions=[Question.create_free_text("Q")], images=[image]
        ).owned_by("user-a")
        moto_survey_service.put_new_survey_template(template)
        moto_survey_service.update_survey_template_content(
            template.edited(name="t", questions=template.questions, images=[])
        )
        item = _raw(
            moto_survey_service, moto_survey_service.survey_templates_table, template.id
        )
        assert "images" not in item

    def test_batch_read_is_strong_and_rereads_unprocessed(self, moto_survey_service):
        first, second = _owned_template(), _owned_template()
        for template in (first, second):
            moto_survey_service.put_new_survey_template(template)
        client = moto_survey_service.dynamodb_client
        real = client.batch_get_item
        calls: list[dict] = []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}
            return real(**kwargs)

        client.batch_get_item = flaky
        found = moto_survey_service.get_survey_templates_by_ids(
            [first.id, second.id, "missing"]
        )
        assert set(found) == {first.id, second.id}
        table = moto_survey_service.survey_templates_table
        assert calls[0]["RequestItems"][table]["ConsistentRead"] is True

    def test_all_templates_scan_reads_every_page(self, moto_survey_service):
        made = [_owned_template() for _ in range(5)]
        for template in made:
            moto_survey_service.put_new_survey_template(template)
        client = moto_survey_service.dynamodb_client
        real = client.scan
        client.scan = lambda **kw: real(**{**kw, "Limit": 2})
        found = moto_survey_service.get_all_survey_templates()
        assert {t.id for t in found} == {t.id for t in made}


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
