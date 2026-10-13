"""AuthzLookupService（判定用の DynamoDB の読み取り）の結合テスト。moto を使う。

BR2.10（約束 C8 の列だけを射影で読む・書き込みをしない・失敗は許可に倒さない）、
NFR9.12（100 件を 1 回の BatchGetItem で読む）、NFR9.18（接続 0.5 秒・読み取り 0.5 秒・
試行合計 2 回、UnprocessedKeys の読み直しは 1 回、締め切りの確認）、NFR9.22（Jobs の
読み取り）、NFR Design R-01（締め切りの時刻を受け取り、残りが 1 回分未満なら読まない）。
"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError, EndpointConnectionError  # noqa: E402

from src.models.authz import AccessTarget, ResourceType  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.models.job import JobStatus  # noqa: E402
from src.services.authz_lookup_service import (  # noqa: E402
    CLIENT_CONFIG,
    MIN_CALL_BUDGET_SECONDS,
    AuthzLookupError,
    AuthzLookupService,
    LookupTable,
)
from tests.error_helpers import raises_code  # noqa: E402

REGION = "ap-northeast-1"
PERSONAS = "PersonasTest"
JOBS = "JobsTest"
PERSONA_TABLE = LookupTable(
    ResourceType.PERSONA, PERSONAS, display_name_attribute="name"
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _create_table(name: str) -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        _create_table(PERSONAS)
        _create_table(JOBS)
        yield boto3.client("dynamodb", region_name=REGION)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(aws, clock) -> AuthzLookupService:
    return AuthzLookupService(region=REGION, jobs_table_name=JOBS, clock=clock)


def _put_persona(client: Any, persona_id: str, **extra: Any) -> None:
    item: dict[str, Any] = {
        "id": {"S": persona_id},
        "name": {"S": f"ペルソナ {persona_id}"},
        "background": {"S": "本体の大きな項目"},
        "owner_sub": {"S": "user-a"},
    }
    item.update(extra)
    client.put_item(TableName=PERSONAS, Item=item)


def _later(clock: FakeClock, seconds: float = 3.0) -> float:
    return clock.now + seconds


class TestProjection:
    def test_single_id_reads_only_the_decision_columns(self, aws, service, clock):
        _put_persona(aws, "p-1", shared_group_ids={"SS": ["sales", "dev"]})
        spy = Mock(wraps=service.client)
        service.client = spy

        result = service.read_targets(PERSONA_TABLE, ["p-1"], _later(clock))

        assert result == {
            "p-1": AccessTarget(
                resource_type=ResourceType.PERSONA,
                resource_id="p-1",
                owner_sub="user-a",
                shared_group_ids=frozenset({"sales", "dev"}),
                display_name="ペルソナ p-1",
            )
        }
        kwargs = spy.get_item.call_args.kwargs
        assert kwargs["ConsistentRead"] is True
        projected = set(kwargs["ExpressionAttributeNames"].values())
        assert projected == {"id", "owner_sub", "shared_group_ids", "name"}
        assert "background" not in str(kwargs)

    def test_missing_id_is_left_out(self, service, clock):
        assert service.read_targets(PERSONA_TABLE, ["nope"], _later(clock)) == {}

    def test_item_without_owner_or_shares(self, aws, service, clock):
        aws.put_item(TableName=PERSONAS, Item={"id": {"S": "p-2"}})
        target = service.read_targets(PERSONA_TABLE, ["p-2"], _later(clock))["p-2"]
        assert target.owner_sub is None
        assert target.shared_group_ids == frozenset()
        assert target.display_name is None

    def test_reader_adapter_reads_the_same(self, aws, service, clock):
        _put_persona(aws, "p-1")
        reader = service.reader(PERSONA_TABLE)
        assert reader.read(["p-1"], _later(clock))["p-1"].owner_sub == "user-a"


class TestBatch:
    def test_hundred_ids_are_one_batch_call(self, aws, service, clock):
        ids = [f"p-{i}" for i in range(100)]
        for persona_id in ids[:10]:
            _put_persona(aws, persona_id)
        spy = Mock(wraps=service.client)
        service.client = spy

        result = service.read_targets(PERSONA_TABLE, ids, _later(clock))

        assert spy.batch_get_item.call_count == 1
        assert set(result) == set(ids[:10])
        request = spy.batch_get_item.call_args.kwargs["RequestItems"][PERSONAS]
        assert request["ConsistentRead"] is True
        assert "background" not in str(request)

    def _stub_batch(self, service: AuthzLookupService, responses: list[dict]) -> Mock:
        stub = Mock()
        stub.batch_get_item.side_effect = responses
        service.client = stub
        return stub

    def test_unprocessed_keys_are_read_again_once(self, service, clock):
        found = {"id": {"S": "p-1"}, "owner_sub": {"S": "user-a"}}
        later = {"id": {"S": "p-2"}, "owner_sub": {"S": "user-b"}}
        stub = self._stub_batch(
            service,
            [
                {
                    "Responses": {PERSONAS: [found]},
                    "UnprocessedKeys": {PERSONAS: {"Keys": [{"id": {"S": "p-2"}}]}},
                },
                {"Responses": {PERSONAS: [later]}, "UnprocessedKeys": {}},
            ],
        )

        result = service.read_targets(PERSONA_TABLE, ["p-1", "p-2"], _later(clock))

        assert stub.batch_get_item.call_count == 2
        assert result["p-2"].owner_sub == "user-b"
        again = stub.batch_get_item.call_args_list[1].kwargs["RequestItems"][PERSONAS]
        assert again["Keys"] == [{"id": {"S": "p-2"}}]

    def test_keys_left_after_the_second_read_fail(self, service, clock):
        pending = {
            "Responses": {PERSONAS: []},
            "UnprocessedKeys": {PERSONAS: {"Keys": [{"id": {"S": "p-2"}}]}},
        }
        stub = self._stub_batch(service, [pending, pending, pending])

        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.read_targets(PERSONA_TABLE, ["p-1", "p-2"], _later(clock))

        assert info.value.reason == "unprocessed"
        assert stub.batch_get_item.call_count == 2

    def test_no_second_read_without_time_for_it(self, service, clock):
        def slow_first_read(**_: Any) -> dict:
            clock.now += 2.5  # 残りが 0.5 秒になる
            return {
                "Responses": {PERSONAS: []},
                "UnprocessedKeys": {PERSONAS: {"Keys": [{"id": {"S": "p-2"}}]}},
            }

        stub = Mock()
        stub.batch_get_item.side_effect = slow_first_read
        service.client = stub

        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.read_targets(PERSONA_TABLE, ["p-1", "p-2"], _later(clock))

        assert info.value.reason == "deadline"
        assert stub.batch_get_item.call_count == 1


class TestDeadline:
    def test_past_deadline_reads_nothing(self, service, clock):
        stub = Mock()
        service.client = stub
        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.read_targets(PERSONA_TABLE, ["p-1"], clock.now - 0.1)
        assert info.value.reason == "deadline"
        stub.get_item.assert_not_called()

    def test_less_than_one_call_left_reads_nothing(self, service, clock):
        stub = Mock()
        service.client = stub
        deadline = clock.now + MIN_CALL_BUDGET_SECONDS - 0.01
        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.read_targets(PERSONA_TABLE, ["p-1", "p-2"], deadline)
        assert info.value.reason == "deadline"
        stub.batch_get_item.assert_not_called()


class TestUnreachable:
    @pytest.mark.parametrize(
        "error",
        [
            EndpointConnectionError(endpoint_url="https://dynamodb.example"),
            ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                "BatchGetItem",
            ),
        ],
        ids=["connection", "throttled"],
    )
    def test_failures_raise_lookup_errors(self, service, clock, error):
        stub = Mock()
        stub.get_item.side_effect = error
        service.client = stub
        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.read_targets(PERSONA_TABLE, ["p-1"], _later(clock))
        assert info.value.reason == "unreachable"
        assert "p-1" not in str(info.value)

    def test_client_config_has_the_short_limits(self, service):
        assert CLIENT_CONFIG.connect_timeout == 0.5
        assert CLIENT_CONFIG.read_timeout == 0.5
        assert CLIENT_CONFIG.retries == {"total_max_attempts": 2, "mode": "standard"}
        config = service.client.meta.config
        assert config.connect_timeout == 0.5
        assert config.read_timeout == 0.5


class TestJobs:
    def _put_job(self, client: Any, **extra: Any) -> None:
        item: dict[str, Any] = {
            "id": {"S": "job-1"},
            "status": {"S": "completed"},
            "created_at": {"S": datetime(2026, 10, 12, 10, 0).isoformat()},
            "updated_at": {"S": datetime(2026, 10, 12, 10, 1).isoformat()},
            "expires_at": {"N": "1999999999"},
            "result": {"S": '{"answer": 42}'},
            "owner_sub": {"S": "user-a"},
            "job_type": {"S": "discussion"},
        }
        item.update(extra)
        client.put_item(TableName=JOBS, Item=item)

    def test_reads_the_job_with_its_owner(self, aws, service):
        self._put_job(aws)
        spy = Mock(wraps=service.client)
        service.client = spy

        job = service.get_job("job-1")

        assert job is not None
        assert job.owner_sub == "user-a"
        assert job.status is JobStatus.COMPLETED
        assert job.result == {"answer": 42}
        assert spy.get_item.call_args.kwargs["ConsistentRead"] is True

    def test_missing_job_is_none(self, service):
        assert service.get_job("nope") is None

    def test_unreachable_jobs_table_raises(self, service):
        stub = Mock()
        stub.get_item.side_effect = EndpointConnectionError(endpoint_url="https://x")
        service.client = stub
        with raises_code(AuthzLookupError, ErrorCode.LOOKUP_UNAVAILABLE) as info:
            service.get_job("job-1")
        assert info.value.reason == "unreachable"
