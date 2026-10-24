"""KnowledgeBaseStore（ナレッジベースの登録の読み書き）の結合テスト。moto を使う。

U7 BR1.5（新規の登録は「まだない ID」の条件付き）、BR5.1（利用範囲は登録では書かず、
admin だけで始まる）、BR5.2（利用範囲は ADD / DELETE。全部外すと列が消える）、
NFR9.37（一覧は scan の最後のページまで）。
"""

from typing import Any

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402

from src.models.errors import ErrorCode  # noqa: E402
from src.models.knowledge_base import KnowledgeBase  # noqa: E402
from src.services.knowledge_base_store import (  # noqa: E402
    KnowledgeBaseStore,
    KnowledgeBaseStoreError,
)

REGION = "ap-northeast-1"
TABLE = "KnowledgeBasesTest"


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
def store(client) -> KnowledgeBaseStore:
    return KnowledgeBaseStore(region=REGION, table_name=TABLE, client=client)


def _kb(name: str = "社内資料") -> KnowledgeBase:
    return KnowledgeBase.create_new("KB123", name, "説明")


def _raw(client: Any, kb_id: str) -> dict[str, Any] | None:
    return client.get_item(TableName=TABLE, Key={"id": {"S": kb_id}}).get("Item")


class TestPutAndGet:
    def test_registered_kb_reads_back(self, store):
        kb = _kb()
        store.put_new(kb)

        read = store.get(kb.id, consistent=True)
        assert read is not None
        assert (read.id, read.knowledge_base_id, read.name) == (
            kb.id,
            "KB123",
            "社内資料",
        )
        assert read.shared_group_ids == frozenset()

    def test_registration_never_writes_the_scope(self, store, client):
        """BR5.1: 登録は利用範囲を書かない（admin だけで始まる）。"""
        kb = KnowledgeBase.from_dict({**_kb().to_dict(), "shared_group_ids": ["sales"]})
        store.put_new(kb)

        assert "shared_group_ids" not in _raw(client, kb.id)

    def test_existing_id_is_refused(self, store):
        kb = _kb()
        store.put_new(kb)

        with pytest.raises(KnowledgeBaseStoreError) as exc_info:
            store.put_new(kb)
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED

    def test_missing_kb_is_none(self, store):
        assert store.get("missing") is None


class TestListAndDelete:
    def test_list_reads_every_scan_page(self, store, client):
        kbs = [_kb(f"資料{i}") for i in range(5)]
        for kb in kbs:
            store.put_new(kb)
        original = client.scan
        calls: list[dict[str, Any]] = []

        def paged(**kwargs: Any) -> Any:
            calls.append(kwargs)
            return original(**{**kwargs, "Limit": 2})

        client.scan = paged

        assert sorted(kb.id for kb in store.list_all()) == sorted(k.id for k in kbs)
        assert len(calls) == 3

    def test_delete(self, store):
        kb = _kb()
        store.put_new(kb)

        store.delete(kb.id)

        assert store.get(kb.id, consistent=True) is None


class TestUpdateScope:
    def test_add_then_remove(self, store):
        kb = _kb()
        store.put_new(kb)

        assert store.update_scope(
            kb.id, add=frozenset({"sales", "plan"}), remove=frozenset()
        )
        assert store.get(kb.id, consistent=True).shared_group_ids == {"sales", "plan"}

        assert store.update_scope(kb.id, add=frozenset(), remove=frozenset({"plan"}))
        assert store.get(kb.id, consistent=True).shared_group_ids == {"sales"}

    def test_removing_every_department_drops_the_column(self, store, client):
        kb = _kb()
        store.put_new(kb)
        store.update_scope(kb.id, add=frozenset({"sales"}), remove=frozenset())

        store.update_scope(kb.id, add=frozenset(), remove=frozenset({"sales"}))

        assert "shared_group_ids" not in _raw(client, kb.id)

    def test_missing_kb_is_false_and_creates_nothing(self, store, client):
        assert not store.update_scope(
            "missing", add=frozenset({"sales"}), remove=frozenset()
        )
        assert _raw(client, "missing") is None


class TestFailures:
    def test_unreachable_table_raises_the_store_error(self, client):
        store = KnowledgeBaseStore(
            region=REGION, table_name="NoSuchTable", client=client
        )

        with pytest.raises(KnowledgeBaseStoreError) as exc_info:
            store.list_all()
        assert exc_info.value.code is ErrorCode.DATABASE_OPERATION_FAILED
