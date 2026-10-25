"""PersonaBindingStore（紐付けの読み書き）の結合テスト。moto を使う。

BR2.2・BR2.5（ペルソナ ID と紐付け先の組での削除）、BR2.9（KB は固定キーの上書き 1 回）、
BR3.1（persona_id での全件削除、未処理分の再試行）、NFR2.9（Query をページの最後まで、
Scan を呼ばない）、NFR1.35、TargetIndex の逆引き。
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.persona_binding import KB_TARGET_KEY, PersonaBinding  # noqa: E402
from src.services.stores.persona_binding_store import (  # noqa: E402
    PersonaBindingStore,
    PersonaBindingStoreError,
)

REGION = "ap-northeast-1"
TABLE = "PersonaBindingsTest"
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)


def _create(client: Any) -> None:
    client.create_table(
        TableName=TABLE,
        KeySchema=[
            {"AttributeName": "persona_id", "KeyType": "HASH"},
            {"AttributeName": "target_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "persona_id", "AttributeType": "S"},
            {"AttributeName": "target_key", "AttributeType": "S"},
            {"AttributeName": "target_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=[
            {
                "IndexName": "TargetIndex",
                "KeySchema": [{"AttributeName": "target_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            }
        ],
    )


@pytest.fixture
def client(monkeypatch) -> Any:
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        c = boto3.client("dynamodb", region_name=REGION)
        _create(c)
        yield c


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def store(client, sleeps) -> PersonaBindingStore:
    return PersonaBindingStore(
        region=REGION, table_name=TABLE, client=client, sleep=sleeps.append
    )


def _dataset(persona: str, dataset: str, **kwargs: Any) -> PersonaBinding:
    return PersonaBinding.for_dataset(persona, dataset, now=NOW, **kwargs)


class TestListAndPut:
    def test_list_reads_every_page_with_query(self, store, client):
        for i in range(5):
            store.put(_dataset("p1", f"d{i}", binding_keys={"user_id": f"U{i}"}))
        store.put(_dataset("p2", "other"))

        calls: list[dict[str, Any]] = []
        original = client.query

        def paged(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**{**kwargs, "Limit": 2})

        client.query = paged
        client.scan = Mock(side_effect=AssertionError("Scan must not be used"))

        bindings = store.list_for_persona("p1")

        assert sorted(b.target_id for b in bindings) == [f"d{i}" for i in range(5)]
        assert len(calls) == 3  # 2 + 2 + 1
        assert all(c["ConsistentRead"] for c in calls)
        assert bindings[0].binding_keys["user_id"].startswith("U")

    def test_kb_binding_is_one_overwritten_item(self, store, client):
        store.put(PersonaBinding.for_kb("p1", "kb-1", now=NOW))
        store.put(
            PersonaBinding.for_kb("p1", "kb-2", metadata_filters={"a": "b"}, now=NOW)
        )

        kbs = [b for b in store.list_for_persona("p1") if b.target_key == KB_TARGET_KEY]
        assert len(kbs) == 1
        assert kbs[0].target_id == "kb-2"
        assert kbs[0].metadata_filters == {"a": "b"}

    def test_empty_persona_has_no_bindings(self, store):
        assert store.list_for_persona("nobody") == []


class TestDelete:
    def test_delete_by_persona_and_target(self, store):
        store.put(_dataset("p1", "d1"))
        store.put(_dataset("p2", "d1"))

        assert store.delete("p1", "DATASET#d1")
        assert store.list_for_persona("p1") == []
        assert [b.persona_id for b in store.list_for_persona("p2")] == ["p2"]

    def test_delete_of_a_missing_binding_is_false(self, store):
        store.put(_dataset("p2", "d1"))
        assert not store.delete("p1", "DATASET#d1")
        assert not store.delete("p1", KB_TARGET_KEY)

    def test_delete_all_removes_every_binding(self, store):
        for i in range(30):  # more than one batch of 25
            store.put(_dataset("p1", f"d{i}"))
        store.put(PersonaBinding.for_kb("p1", "kb", now=NOW))

        assert store.delete_all("p1") == 31
        assert store.list_for_persona("p1") == []

    def test_unprocessed_items_are_retried_three_times(self, store, client, sleeps):
        store.put(_dataset("p1", "d1"))
        original = client.batch_write_item
        state = {"calls": 0}

        def flaky(**kwargs: Any) -> Any:
            state["calls"] += 1
            if state["calls"] <= 2:
                return {"UnprocessedItems": kwargs["RequestItems"]}
            return original(**kwargs)

        client.batch_write_item = flaky
        assert store.delete_all("p1") == 1
        assert sleeps == [0.2, 0.4]

    def test_unprocessed_items_after_three_retries_fail(self, store, client, sleeps):
        store.put(_dataset("p1", "d1"))
        client.batch_write_item = lambda **kwargs: {
            "UnprocessedItems": kwargs["RequestItems"]
        }
        with pytest.raises(PersonaBindingStoreError):
            store.delete_all("p1")
        assert sleeps == [0.2, 0.4, 0.8]


class TestTargetIndex:
    def test_persona_ids_for_a_target(self, store):
        store.put(_dataset("p1", "d1"))
        store.put(_dataset("p2", "d1"))
        store.put(PersonaBinding.for_kb("p3", "d1", now=NOW))
        store.put(_dataset("p4", "other"))

        assert sorted(store.persona_ids_for_target("d1")) == ["p1", "p2", "p3"]


class TestFailures:
    def test_dynamodb_failures_are_store_errors(self, client):
        store = PersonaBindingStore(region=REGION, table_name="Missing", client=client)
        with pytest.raises(PersonaBindingStoreError):
            store.list_for_persona("p1")
        with pytest.raises(PersonaBindingStoreError):
            store.put(_dataset("p1", "d1"))
