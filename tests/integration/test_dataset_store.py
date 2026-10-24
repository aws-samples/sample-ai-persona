"""DatasetStore（データセットの登録の読み書き）の結合テスト。moto を使う。

U5 BR6.1・BR6.2（ペルソナ専用のデータセットは owner_persona_id を持ち、一覧に出ない）、
U7 BR1.5（新規の登録は上書きしない、編集は渡した列だけ）、BR5.2・BR5.6（利用範囲は
ADD / DELETE で、組織資産だけ。全部外すと列が消える）、NFR1.77。
"""

from datetime import datetime
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.dataset import Dataset, DatasetColumn  # noqa: E402
from src.models.errors import ErrorCode  # noqa: E402
from src.services.dataset_store import DatasetStore, DatasetStoreError  # noqa: E402

REGION = "ap-northeast-1"
TABLE = "DatasetsTest"


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
def store(client) -> DatasetStore:
    return DatasetStore(region=REGION, table_name=TABLE, client=client)


def _dataset(**kwargs: Any) -> Dataset:
    return Dataset.create_new(
        name="売上",
        description="説明",
        s3_path="s3://b/datasets/x.csv",
        columns=[DatasetColumn(name="a", data_type="string", description="列A")],
        row_count=3,
        **kwargs,
    )


def _raw(client: Any, dataset_id: str) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": dataset_id}}).get("Item")


def _scope(**groups: frozenset[str]) -> dict[str, frozenset[str]]:
    return {"add": frozenset(), "remove": frozenset(), **groups}


class TestPutAndGet:
    def test_registered_dataset_reads_back(self, store):
        dataset = _dataset()
        store.put_new(dataset)

        read = store.get(dataset.id, consistent=True)
        assert read is not None
        assert (read.name, read.description, read.row_count) == ("売上", "説明", 3)
        assert isinstance(read.row_count, int)
        assert read.columns[0].description == "列A"
        assert read.created_at == dataset.created_at

    def test_org_asset_has_no_owner_column(self, store, client):
        dataset = _dataset()
        store.put_new(dataset)

        assert "owner_persona_id" not in _raw(client, dataset.id)

    def test_dedicated_dataset_keeps_its_persona(self, store):
        """U5 BR6.1: ペルソナ専用のデータセットは owner_persona_id を持つ。"""
        dataset = _dataset(owner_persona_id="p1")
        store.put_new(dataset)

        assert store.get(dataset.id).owner_persona_id == "p1"

    def test_existing_id_is_refused_and_the_scope_stays(self, store):
        """U7 BR1.5: 新規の登録は上書きしない（利用範囲が消えない）。"""
        dataset = _dataset()
        store.put_new(dataset)
        store.update_scope(dataset.id, **_scope(add=frozenset({"sales"})))

        with pytest.raises(DatasetStoreError) as exc_info:
            store.put_new(dataset)

        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
        assert store.get(dataset.id).shared_group_ids == {"sales"}

    def test_missing_dataset_is_none(self, store):
        assert store.get("missing") is None


class TestUpdateFields:
    def test_writes_only_the_given_columns_and_keeps_the_scope(self, store, client):
        dataset = _dataset()
        store.put_new(dataset)
        store.update_scope(dataset.id, **_scope(add=frozenset({"sales"})))
        put = Mock(wraps=client.put_item)
        client.put_item = put

        assert store.update_fields(
            dataset.id,
            updated_at=datetime(2030, 1, 1),
            name="新しい名前",
            notes="n2",
            columns=[DatasetColumn(name="b", data_type="integer")],
        )

        assert put.call_count == 0
        stored = store.get(dataset.id, consistent=True)
        assert (stored.name, stored.description, stored.notes) == (
            "新しい名前",
            "説明",
            "n2",
        )
        assert stored.columns[0].name == "b"
        assert stored.shared_group_ids == {"sales"}
        assert stored.created_at == dataset.created_at
        assert stored.updated_at == datetime(2030, 1, 1)

    def test_missing_dataset_is_false_and_creates_nothing(self, store, client):
        assert not store.update_fields(
            "missing", updated_at=datetime(2030, 1, 1), name="x"
        )
        assert _raw(client, "missing") is None


class TestListAndDelete:
    def test_listing_leaves_out_dedicated_in_the_scan(self, store, client):
        """U5 BR6.2: 一覧はペルソナ専用を scan の条件で外す。"""
        org = _dataset()
        store.put_new(org)
        store.put_new(_dataset(owner_persona_id="p1"))
        store.update_scope(org.id, **_scope(add=frozenset({"plan"})))
        scan = Mock(wraps=client.scan)
        client.scan = scan

        listed = store.list_org_assets()

        assert [d.id for d in listed] == [org.id]
        assert listed[0].shared_group_ids == frozenset({"plan"})
        assert isinstance(listed[0].shared_group_ids, frozenset)
        assert (
            "attribute_not_exists(owner_persona_id)"
            in (scan.call_args.kwargs["FilterExpression"])
        )

    def test_listing_reads_every_scan_page(self, store, client):
        made = [_dataset() for _ in range(5)]
        for dataset in made:
            store.put_new(dataset)
        original = client.scan
        calls: list[dict[str, Any]] = []

        def paged(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**{**kwargs, "Limit": 2})

        client.scan = paged

        assert {d.id for d in store.list_org_assets()} == {d.id for d in made}
        assert len(calls) == 3

    def test_delete(self, store):
        dataset = _dataset(owner_persona_id="p1")
        store.put_new(dataset)

        store.delete(dataset.id)

        assert store.get(dataset.id, consistent=True) is None


class TestUpdateScope:
    def test_add_then_remove_as_two_set_updates(self, store, client):
        dataset = _dataset()
        store.put_new(dataset)
        update = Mock(wraps=client.update_item)
        client.update_item = update

        assert store.update_scope(
            dataset.id, **_scope(add=frozenset({"sales", "plan"}))
        )
        assert store.update_scope(
            dataset.id, add=frozenset({"dev"}), remove=frozenset({"plan"})
        )

        expressions = [c.kwargs["UpdateExpression"] for c in update.call_args_list]
        assert len(expressions) == 3
        assert expressions[1].startswith("ADD") and expressions[2].startswith("DELETE")
        assert store.get(dataset.id).shared_group_ids == {"sales", "dev"}

    def test_removing_every_department_drops_the_column(self, store, client):
        dataset = _dataset()
        store.put_new(dataset)
        store.update_scope(dataset.id, **_scope(add=frozenset({"sales"})))

        store.update_scope(dataset.id, **_scope(remove=frozenset({"sales"})))

        assert "shared_group_ids" not in _raw(client, dataset.id)

    def test_empty_change_sends_nothing(self, store, client):
        update = Mock(wraps=client.update_item)
        client.update_item = update

        assert store.update_scope("any", **_scope())
        assert update.call_count == 0

    def test_missing_dataset_is_false_and_creates_nothing(self, store, client):
        assert not store.update_scope("gone", **_scope(add=frozenset({"sales"})))
        assert _raw(client, "gone") is None

    def test_dedicated_dataset_is_refused(self, store, client):
        """BR5.6: 利用範囲は組織資産だけ（ペルソナ専用には付けない）。"""
        dedicated = _dataset(owner_persona_id="p1")
        store.put_new(dedicated)

        assert not store.update_scope(dedicated.id, **_scope(add=frozenset({"sales"})))
        assert "shared_group_ids" not in _raw(client, dedicated.id)


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = DatasetStore(region=REGION, table_name="NoSuchTable", client=client)

        with pytest.raises(DatasetStoreError) as exc_info:
            store.list_org_assets()
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
