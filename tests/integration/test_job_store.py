"""JobStore（ジョブの記録の書き込み）の結合テスト。moto を使う。

約束 C14（投入者と種類を記録する）、U6 BR8.3（失敗は例外文ではなくエラーコードで
保存する）。読み出しは ``AuthzLookupService.get_job`` が担うため、書いたものがそのまま
読めることも確かめる。
"""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.models.job import JobStatus  # noqa: E402
from src.services.authz_lookup_service import AuthzLookupService  # noqa: E402
from src.services.stores.job_store import JobStore, JobStoreError  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "JobsTest"


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
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield c


@pytest.fixture
def store(client) -> JobStore:
    return JobStore(region=REGION, table_name=TABLE, client=client)


@pytest.fixture
def lookup(client) -> AuthzLookupService:
    return AuthzLookupService(region=REGION, jobs_table_name=TABLE, client=client)


def _item(client: Any, job_id: str) -> dict[str, Any]:
    return client.get_item(TableName=TABLE, Key={"id": {"S": job_id}})["Item"]


class TestCreate:
    def test_records_the_submitter_type_and_expiry(self, store, client):
        store.create(
            "job-1",
            status="pending",
            expires_at=9999999999,
            owner_sub="user-a",
            job_type="discussion",
        )

        item = _item(client, "job-1")
        assert item["status"] == {"S": "pending"}
        assert item["expires_at"] == {"N": "9999999999"}
        assert item["owner_sub"] == {"S": "user-a"}
        assert item["job_type"] == {"S": "discussion"}
        assert item["created_at"] == item["updated_at"]

    def test_omits_missing_submitter_and_type(self, store, client):
        store.create("job-1", status="pending", expires_at=1)

        item = _item(client, "job-1")
        assert "owner_sub" not in item and "job_type" not in item


class TestLifecycle:
    def test_completed_job_reads_back_with_its_result(self, store, lookup):
        store.create("job-1", status="pending", expires_at=1, owner_sub="user-a")
        store.update_status("job-1", "running")
        assert lookup.get_job("job-1").status is JobStatus.RUNNING

        store.update_completed("job-1", '{"key": "value"}')

        job = lookup.get_job("job-1")
        assert job.status is JobStatus.COMPLETED
        assert job.result == {"key": "value"}
        assert job.owner_sub == "user-a"

    def test_failed_job_keeps_only_the_code(self, store, client, lookup):
        """U6 BR8.3: 失敗はエラーコードの列だけを書く（例外文の列 error は書かない）。"""
        store.create("job-1", status="pending", expires_at=1, owner_sub="user-a")

        store.update_failed("job-1", ErrorCode.UNKNOWN.value)

        item = _item(client, "job-1")
        assert item["error_code"] == {"S": ErrorCode.UNKNOWN.value}
        assert "error" not in item
        job = lookup.get_job("job-1")
        assert job.status is JobStatus.FAILED
        assert job.error_code == ErrorCode.UNKNOWN.value


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = JobStore(region=REGION, table_name="NoSuchTable", client=client)

        with pytest.raises(JobStoreError) as exc_info:
            store.create("job-1", status="pending", expires_at=1)

        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
