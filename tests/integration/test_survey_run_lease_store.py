"""SurveyRunLeaseStore（進行役の担当者の記録）の結合テスト。moto を使う。

BR2.3・NFR3.8: 空き・期限切れ・自分のものなら取れる。他人の有効な記録は取れない。
延長は自分のものだけ。消せるのも holder が自分のときだけ（NFR4.10）。
"""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.services.stores.survey_run_lease_store import (  # noqa: E402
    LEASE_ID,
    SurveyRunLeaseStore,
    SurveyRunLeaseStoreError,
)

REGION = "ap-northeast-1"
TABLE = "SurveyRunLeaseTest"
NOW = 1_800_000_000


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
def store(client) -> SurveyRunLeaseStore:
    return SurveyRunLeaseStore(region=REGION, table_name=TABLE, client=client)


def _row(client: Any) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": LEASE_ID}}).get("Item")


class TestAcquire:
    def test_free_lease_is_taken(self, store, client):
        assert store.acquire("task-a", now=NOW, ttl_seconds=60)

        row = _row(client)
        assert row["holder"]["S"] == "task-a"
        assert int(row["expires_at"]["N"]) == NOW + 60

    def test_own_lease_is_taken_again_and_extended(self, store, client):
        store.acquire("task-a", now=NOW, ttl_seconds=60)

        assert store.acquire("task-a", now=NOW + 15, ttl_seconds=60)
        assert int(_row(client)["expires_at"]["N"]) == NOW + 75

    def test_someone_elses_valid_lease_is_not_taken(self, store, client):
        """NFR3.8: 2 つの進行役が取り合うと、片方だけが仕事をする。"""
        assert store.acquire("task-a", now=NOW, ttl_seconds=60)

        assert not store.acquire("task-b", now=NOW + 30, ttl_seconds=60)
        assert _row(client)["holder"]["S"] == "task-a"

    def test_expired_lease_is_taken_over(self, store, client):
        """NFR4.2: 担当が消えても期限が切れればほかのタスクが取る。"""
        store.acquire("task-a", now=NOW, ttl_seconds=60)

        assert store.acquire("task-b", now=NOW + 61, ttl_seconds=60)
        assert _row(client)["holder"]["S"] == "task-b"


class TestRenew:
    def test_own_lease_is_extended(self, store, client):
        store.acquire("task-a", now=NOW, ttl_seconds=60)

        assert store.renew("task-a", now=NOW + 20, ttl_seconds=60)
        assert int(_row(client)["expires_at"]["N"]) == NOW + 80

    def test_lease_taken_over_is_not_extended(self, store, client):
        store.acquire("task-a", now=NOW, ttl_seconds=60)
        store.acquire("task-b", now=NOW + 61, ttl_seconds=60)

        assert not store.renew("task-a", now=NOW + 62, ttl_seconds=60)
        assert _row(client)["holder"]["S"] == "task-b"

    def test_missing_lease_is_not_created(self, store, client):
        assert not store.renew("task-a", now=NOW, ttl_seconds=60)
        assert _row(client) is None


class TestRelease:
    def test_own_lease_is_removed(self, store, client):
        store.acquire("task-a", now=NOW, ttl_seconds=60)

        assert store.release("task-a")
        assert _row(client) is None

    def test_someone_elses_lease_is_kept(self, store, client):
        store.acquire("task-b", now=NOW, ttl_seconds=60)

        assert not store.release("task-a")
        assert _row(client)["holder"]["S"] == "task-b"


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = SurveyRunLeaseStore(region=REGION, table_name="NoSuch", client=client)

        with pytest.raises(SurveyRunLeaseStoreError) as info:
            store.acquire("task-a", now=NOW, ttl_seconds=60)
        assert info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
