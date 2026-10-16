"""UploadedFileStore（アップロードファイルのメタデータ）の結合テスト。moto を使う。

BR5.1〜BR5.3、NFR1.27（本人・期限内・未紐付けだけを紐付ける条件）、NFR1.30、
NFR1.34・NFR1.37（全部か無しのトランザクション）、NFR1.38（戻し）、
NFR9.29（強い整合性の読み）。

moto で起こせない TransactionConflict は、CancellationReasons を組み立てた
スタブの client で確かめる（unit-test-instructions.md）。
"""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.models.uploaded_file import UploadedFile  # noqa: E402
from src.services.uploaded_file_store import (  # noqa: E402
    MAX_TRANSACTION_ITEMS,
    AttachConditionFailed,
    AttachConflict,
    UploadedFileStore,
    UploadedFileStoreError,
)

REGION = "ap-northeast-1"
TABLE = "UploadedFilesTest"
NOW = datetime(2026, 10, 14, 3, 0, 0, tzinfo=timezone.utc)
NOW_EPOCH = int(NOW.timestamp())


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
def store(client) -> UploadedFileStore:
    return UploadedFileStore(region=REGION, table_name=TABLE, client=client)


def _file(owner: str = "sub-a", **kwargs: Any) -> UploadedFile:
    return UploadedFile.create_new(
        owner_sub=owner,
        file_type="discussion_document",
        original_filename="資料.pdf",
        file_size=10,
        file_hash="h",
        mime_type="application/pdf",
        now=NOW,
        **kwargs,
    ).with_path("s3://bucket/key")


class TestReadWrite:
    def test_put_writes_owner_and_expiry(self, store, client):
        f = _file()
        store.put(f)
        item = client.get_item(TableName=TABLE, Key={"id": {"S": f.id}})["Item"]

        assert item["owner_sub"] == {"S": "sub-a"}
        assert item["expires_at"] == {"N": str(f.expires_at)}
        assert store.get(f.id) == f

    def test_get_is_strongly_consistent(self, store, client):
        f = _file()
        store.put(f)
        spy = Mock(wraps=client.get_item)
        client.get_item = spy
        store.get(f.id)
        assert spy.call_args.kwargs["ConsistentRead"] is True
        assert store.get("missing") is None

    def test_get_many_rereads_unprocessed_keys(self, store, client):
        files = [_file() for _ in range(3)]
        for f in files:
            store.put(f)
        original = client.batch_get_item
        state = {"calls": 0}

        def partial(**kwargs: Any) -> Any:
            state["calls"] += 1
            request = kwargs["RequestItems"][TABLE]
            assert request["ConsistentRead"] is True
            if state["calls"] == 1:
                keys = request["Keys"]
                first = original(RequestItems={TABLE: {**request, "Keys": keys[:1]}})
                first["UnprocessedKeys"] = {TABLE: {**request, "Keys": keys[1:]}}
                return first
            return original(**kwargs)

        client.batch_get_item = partial
        found = store.get_many([f.id for f in files] + ["missing"])

        assert set(found) == {f.id for f in files}
        assert state["calls"] == 2

    def test_get_many_of_nothing_reads_nothing(self, store, client):
        client.batch_get_item = Mock(side_effect=AssertionError("no read"))
        assert store.get_many([]) == {}


class TestTransactAttach:
    def test_attach_sets_the_parent_and_removes_the_expiry(self, store):
        files = [_file(), _file()]
        for f in files:
            store.put(f)

        store.transact_attach(
            "sub-a", [f.id for f in files], "discussion", "d1", NOW_EPOCH
        )

        for f in files:
            stored = store.get(f.id)
            assert stored.parent_type == "discussion"
            assert stored.parent_id == "d1"
            assert stored.expires_at is None
            # 後始末のスクリプトが保存の途中を見分けるための紐付けの時刻
            assert stored.attached_at == NOW_EPOCH

    @pytest.mark.parametrize(
        "case", ["other_owner", "expired", "already_attached", "missing"]
    )
    def test_any_failed_condition_changes_nothing(self, store, case):
        good = _file()
        store.put(good)
        if case == "other_owner":
            bad = _file(owner="sub-b")
            store.put(bad)
        elif case == "expired":
            bad = _file()
            store.put(bad)
        elif case == "already_attached":
            bad = _file().attached_to("discussion", "d0")
            store.put(bad)
        else:
            bad = _file()
        now = (bad.expires_at or NOW_EPOCH) if case == "expired" else NOW_EPOCH

        with pytest.raises(AttachConditionFailed):
            store.transact_attach("sub-a", [good.id, bad.id], "discussion", "d1", now)
        assert store.get(good.id).parent_id is None
        assert store.get(good.id).expires_at == good.expires_at

    def test_more_than_100_files_is_a_caller_error(self, store):
        assert MAX_TRANSACTION_ITEMS == 100
        with pytest.raises(ValueError):
            store.transact_attach(
                "sub-a", [f"f{i}" for i in range(101)], "discussion", "d1", NOW_EPOCH
            )


class TestTransactDetach:
    def test_detach_restores_the_original_expiry(self, store):
        f = _file()
        store.put(f)
        store.transact_attach("sub-a", [f.id], "discussion", "d1", NOW_EPOCH)

        store.transact_detach({f.id: f.expires_at}, "discussion", "d1")

        restored = store.get(f.id)
        assert restored.parent_id is None
        assert restored.expires_at == f.expires_at
        assert restored.attached_at is None

    def test_detach_from_another_parent_fails(self, store):
        f = _file()
        store.put(f)
        store.transact_attach("sub-a", [f.id], "discussion", "d1", NOW_EPOCH)

        with pytest.raises(AttachConditionFailed):
            store.transact_detach({f.id: f.expires_at}, "discussion", "d2")
        assert store.get(f.id).parent_id == "d1"


class TestFailureClassification:
    def _error(self, code: str, reasons: list[str] | None = None) -> ClientError:
        response: dict[str, Any] = {"Error": {"Code": code, "Message": "x"}}
        if reasons is not None:
            response["CancellationReasons"] = [{"Code": r} for r in reasons]
        return ClientError(response, "TransactWriteItems")

    def _store_raising(self, error: ClientError) -> UploadedFileStore:
        stub = Mock()
        stub.transact_write_items.side_effect = error
        return UploadedFileStore(region=REGION, table_name=TABLE, client=stub)

    def test_conflict_is_its_own_error(self):
        store = self._store_raising(
            self._error("TransactionCanceledException", ["None", "TransactionConflict"])
        )
        with pytest.raises(AttachConflict):
            store.transact_attach("s", ["a", "b"], "discussion", "d", NOW_EPOCH)

    def test_condition_failure_anywhere_is_a_condition_error(self):
        store = self._store_raising(
            self._error(
                "TransactionCanceledException", ["None", "ConditionalCheckFailed"]
            )
        )
        with pytest.raises(AttachConditionFailed):
            store.transact_attach("s", ["a", "b"], "discussion", "d", NOW_EPOCH)

    def test_other_failures_are_plain_store_errors(self):
        store = self._store_raising(self._error("InternalServerError"))
        with pytest.raises(UploadedFileStoreError) as exc_info:
            store.transact_attach("s", ["a"], "discussion", "d", NOW_EPOCH)
        assert not isinstance(exc_info.value, (AttachConflict, AttachConditionFailed))
