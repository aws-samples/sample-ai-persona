"""SurveyStore（アンケートの本体の読み書き）の結合テスト。moto を使う。

U7 BR1.1（新規は「まだない ID」の条件付きで、共有は書かない。OwnerIndex に載る）、
BR1.5・NFR1.87（更新は渡した列だけ。所有者・共有・キー・テンプレートの写しは書かない）、
NFR2.21（まとめ読みは強い整合性で、未処理のキーは読み直す）、NFR2.25（本体の大きさ）。
"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.models.survey import InsightReport, Survey, TemplateSnapshot  # noqa: E402
from src.models.survey_template import Question, SurveyTemplate  # noqa: E402
from src.services.stores.survey_store import SurveyStore, SurveyStoreError  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "SurveysTest"


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        c.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "id", "AttributeType": "S"},
                {"AttributeName": "owner_sub", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "run_queue", "AttributeType": "S"},
                {"AttributeName": "queued_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "OwnerIndex",
                    "KeySchema": [
                        {"AttributeName": "owner_sub", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                # Same shape as the CDK Data stack's RunQueueIndex (sparse)
                {
                    "IndexName": "RunQueueIndex",
                    "KeySchema": [
                        {"AttributeName": "run_queue", "KeyType": "HASH"},
                        {"AttributeName": "queued_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        yield c


@pytest.fixture
def store(client) -> SurveyStore:
    return SurveyStore(region=REGION, table_name=TABLE, client=client)


def _survey(owner: str = "user-a", **kwargs: Any) -> Survey:
    template = SurveyTemplate.create_new(
        name="t", questions=[Question.create_free_text("Q")]
    )
    return Survey.create_new(
        name="2025年1月調査",
        description="テスト調査",
        template_id=template.id,
        persona_count=100,
        datasource="nemotron",
        owner_sub=owner,
        template_snapshot=TemplateSnapshot.from_template(template),
        **kwargs,
    )


def _raw(client: Any, survey_id: str) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": survey_id}}).get("Item")


def _share(client: Any, survey_id: str) -> None:
    client.update_item(
        TableName=TABLE,
        Key={"id": {"S": survey_id}},
        UpdateExpression="SET shared_group_ids = :s",
        ExpressionAttributeValues={":s": {"SS": ["sales"]}},
    )


class TestPutAndGet:
    def test_registered_survey_reads_back(self, store):
        survey = _survey(filters={"gender": "女性", "age": {"min": 20.5}})
        store.put_new(survey)

        read = store.get(survey.id, consistent=True)
        assert read is not None
        assert (read.name, read.owner_sub, read.status) == (
            "2025年1月調査",
            "user-a",
            survey.status,
        )
        assert read.persona_count == 100
        assert isinstance(read.persona_count, int)
        assert read.filters["gender"] == "女性"
        assert read.template_snapshot == survey.template_snapshot
        assert read.created_at == survey.created_at

    def test_optional_fields_left_out_read_back_as_none(self, store, client):
        survey = _survey()
        store.put_new(survey)

        read = store.get(survey.id, consistent=True)
        assert read.s3_result_path is None
        assert read.insight_report is None
        assert read.error_code is None
        assert "s3_result_path" not in _raw(client, survey.id)

    def test_new_survey_has_no_shares_and_lands_in_owner_index(self, store, client):
        """BR1.1: 共有は ResourceSharing だけが書く（作成は共有なしで始まる）。"""
        survey = Survey.from_dict({**_survey().to_dict(), "shared_group_ids": ["s"]})
        store.put_new(survey)

        assert "shared_group_ids" not in _raw(client, survey.id)
        owned = client.query(
            TableName=TABLE,
            IndexName="OwnerIndex",
            KeyConditionExpression="owner_sub = :o",
            ExpressionAttributeValues={":o": {"S": "user-a"}},
        )["Items"]
        assert [i["id"]["S"] for i in owned] == [survey.id]

    def test_existing_id_is_refused(self, store):
        survey = _survey()
        store.put_new(survey)

        with pytest.raises(SurveyStoreError) as exc_info:
            store.put_new(survey)
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED

    def test_missing_survey_is_none(self, store):
        assert store.get("missing") is None


class TestUpdateFields:
    def test_writes_only_those_columns(self, store, client):
        survey = _survey()
        store.put_new(survey)
        _share(client, survey.id)
        put = Mock(wraps=client.put_item)
        client.put_item = put

        assert store.update_fields(
            survey.id,
            {
                "status": "completed",
                "s3_result_path": "s3://b/survey-results/x/results.csv",
                "persona_count": 120,
                "updated_at": "2030-01-01T00:00:00",
            },
            remove=("error_code",),
        )

        assert put.call_count == 0
        stored = store.get(survey.id, consistent=True)
        assert stored.status == "completed"
        assert stored.persona_count == 120
        assert stored.owner_sub == "user-a"
        assert stored.shared_group_ids == frozenset({"sales"})
        assert stored.created_at == survey.created_at
        assert stored.template_snapshot == survey.template_snapshot

    def test_removes_columns(self, store, client):
        survey = _survey()
        store.put_new(survey)
        store.update_fields(survey.id, {"error_context": {"min_count": 100}})
        assert "error_context" in _raw(client, survey.id)

        assert store.update_fields(survey.id, {}, remove=("error_context",))

        assert "error_context" not in _raw(client, survey.id)

    def test_float_values_are_written(self, store):
        survey = _survey()
        store.put_new(survey)

        assert store.update_fields(survey.id, {"error_context": {"ratio": 0.5}})

    def test_insight_report_is_written_as_a_map(self, store):
        survey = _survey()
        store.put_new(survey)
        report = InsightReport.create_new(survey.id, "内容")

        store.update_fields(survey.id, {"insight_report": report.to_dict()})

        assert store.get(survey.id, consistent=True).insight_report.content == "内容"

    def test_deleted_survey_is_false_and_creates_nothing(self, store, client):
        assert store.update_fields("gone", {"status": "error"}) is False
        assert _raw(client, "gone") is None

    def test_nothing_to_write_sends_nothing(self, store, client):
        update = Mock(wraps=client.update_item)
        client.update_item = update

        assert store.update_fields("any", {}) is True
        assert update.call_count == 0

    @pytest.mark.parametrize(
        "column",
        ["owner_sub", "shared_group_ids", "created_at", "id", "template_snapshot"],
    )
    def test_refuses_owner_shares_keys_and_snapshot(self, store, column):
        with pytest.raises(ValueError):
            store.update_fields("x", {column: "y"})
        with pytest.raises(ValueError):
            store.update_fields("x", {}, remove=(column,))


class TestGetMany:
    def test_reads_strongly_and_rereads_unprocessed_keys(self, store, client):
        """NFR2.21: まとめ読みは強い整合性で、未処理のキーは読み直す。"""
        surveys = [_survey() for _ in range(3)]
        for survey in surveys:
            store.put_new(survey)
        real = client.batch_get_item
        calls: list[dict[str, Any]] = []

        def flaky(**kwargs: Any) -> Any:
            calls.append(kwargs)
            if len(calls) == 1:
                return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}
            return real(**kwargs)

        client.batch_get_item = flaky

        found = store.get_many([s.id for s in surveys] + ["missing", ""])

        assert set(found) == {s.id for s in surveys}
        assert found[surveys[0].id].persona_count == 100
        assert len(calls) == 2
        assert calls[0]["RequestItems"][TABLE]["ConsistentRead"] is True

    def test_reads_in_chunks_of_100(self, store, client):
        batch = Mock(wraps=client.batch_get_item)
        client.batch_get_item = batch

        assert store.get_many([f"id-{i}" for i in range(150)]) == {}

        sizes = [len(c.kwargs["RequestItems"][TABLE]["Keys"]) for c in batch.mock_calls]
        assert sizes == [100, 50]

    def test_keys_left_unprocessed_raise(self, store, client):
        client.batch_get_item = lambda **kwargs: {
            "Responses": {},
            "UnprocessedKeys": kwargs["RequestItems"],
        }

        with pytest.raises(SurveyStoreError):
            store.get_many(["a"])


class TestItemSize:
    def test_grows_with_the_body(self):
        """NFR2.25: 本体の大きさは属性名と値の合計で見積もる。"""
        small = SurveyStore.item_size({"id": "a"})
        large = SurveyStore.item_size({"id": "a", "text": "あ" * 1000})

        assert small < large
        assert large >= 3000

    def test_none_values_are_not_counted(self):
        assert SurveyStore.item_size({"id": "a", "x": None}) == SurveyStore.item_size(
            {"id": "a"}
        )


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = SurveyStore(region=REGION, table_name="NoSuchTable", client=client)

        with pytest.raises(SurveyStoreError) as exc_info:
            store.get("x")
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED


# =============================================================================
# Step Functions への移行: 待ちの索引と条件付きの遷移（NFR3.10、NFR4.4）
# =============================================================================


def _queued(store: SurveyStore, at: str, **kwargs: Any) -> Survey:
    survey = _survey(**kwargs)
    survey.queued_at = datetime.fromisoformat(at)
    store.put_new(survey)
    return survey


class TestRunQueue:
    """Only queued and running surveys are in RunQueueIndex (NFR3.10)."""

    def test_lists_queued_and_running_but_not_finished(self, store):
        a = _queued(store, "2026-10-30T01:00:00+00:00")
        b = _queued(store, "2026-10-30T01:00:01+00:00")
        done = _queued(store, "2026-10-30T01:00:02+00:00")
        assert store.transition(b.id, expected="queued", fields={"status": "running"})
        assert store.transition(
            done.id,
            expected="queued",
            fields={"status": "error"},
            remove=("run_queue",),
        )

        listed = {s.id: s.status for s in store.list_run_queue()}

        assert listed == {a.id: "queued", b.id: "running"}

    def test_old_surveys_without_run_queue_are_not_listed(self, store):
        survey = _survey()
        survey.run_queue = None
        survey.queued_at = None
        store.put_new(survey)

        assert store.list_run_queue() == []

    def test_reads_every_page(self, store, client):
        for i in range(3):
            _queued(store, f"2026-10-30T01:00:0{i}+00:00")
        pages = Mock(wraps=client.query)
        store.client = Mock(wraps=client)
        store.client.query = pages
        store.page_size = 1

        assert len(store.list_run_queue()) == 3
        assert pages.call_count >= 3


class TestTransition:
    """Every transition holds only when the status is the expected one (NFR4.4)."""

    def test_queued_to_running_writes_the_execution(self, store, client):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")

        ok = store.transition(
            survey.id,
            expected="queued",
            fields={
                "status": "running",
                "execution_arn": "arn:aws:states:r:1:execution:sm:x",
                "started_at": "2026-10-30T01:00:05+00:00",
                "model_id": "m",
            },
            remove=("start_blocked_since",),
        )

        assert ok
        got = store.get(survey.id, consistent=True)
        assert got.status == "running"
        assert got.execution_arn.endswith(":x")
        assert got.run_queue == "active"

    def test_running_to_completed_removes_run_queue(self, store):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")
        store.transition(survey.id, expected="queued", fields={"status": "running"})

        ok = store.transition(
            survey.id,
            expected="running",
            fields={"status": "completed", "failed_count": 0, "persona_count": 10},
            remove=("run_queue",),
        )

        assert ok
        got = store.get(survey.id, consistent=True)
        assert got.status == "completed"
        assert got.run_queue is None
        assert got.failed_count == 0
        assert store.list_run_queue() == []

    @pytest.mark.parametrize("expected", ["running", "completed", "error"])
    def test_unexpected_status_does_not_transition(self, store, expected):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")

        assert not store.transition(
            survey.id, expected=expected, fields={"status": "error"}
        )
        assert store.get(survey.id, consistent=True).status == "queued"

    def test_deleted_survey_does_not_transition(self, store):
        assert not store.transition(
            "gone", expected="queued", fields={"status": "running"}
        )
        assert store.get("gone") is None

    def test_protected_columns_are_refused(self, store):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")
        with pytest.raises(ValueError):
            store.transition(
                survey.id, expected="queued", fields={"owner_sub": "someone"}
            )

    def test_start_blocked_since_is_written_and_cleared(self, store):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")

        store.transition(
            survey.id,
            expected="queued",
            fields={"start_blocked_since": "2026-10-30T01:01:00+00:00"},
        )
        assert store.get(survey.id, consistent=True).start_blocked_since is not None
        store.transition(
            survey.id, expected="queued", fields={}, remove=("start_blocked_since",)
        )

        assert store.get(survey.id, consistent=True).start_blocked_since is None


class TestAttempts:
    """start_attempts / finish_attempts count up under the status condition."""

    def test_adds_one_and_returns_the_new_count(self, store):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")

        assert store.add_attempt(survey.id, "start_attempts", expected="queued") == 1
        assert store.add_attempt(survey.id, "start_attempts", expected="queued") == 2
        assert store.get(survey.id, consistent=True).start_attempts == 2

    def test_wrong_status_counts_nothing(self, store):
        survey = _queued(store, "2026-10-30T01:00:00+00:00")

        assert (
            store.add_attempt(survey.id, "finish_attempts", expected="running") is None
        )
        assert store.get(survey.id, consistent=True).finish_attempts == 0

    def test_only_the_two_counters(self, store):
        with pytest.raises(ValueError):
            store.add_attempt("x", "persona_count", expected="queued")
