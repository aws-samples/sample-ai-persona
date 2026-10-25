"""ResourceShareStore（本体と ResourceShares の読み書き）の結合テスト。moto を使う。

BR1.6〜BR1.7（1 つのトランザクション、前の値を条件、空なら列を消す）、BR3.2〜BR3.5・BR3.9
（索引の読み取り、件数、カーソルの位置）、BR4.1（削除の条件に共有先と後始末の列）、
NFR1.12・NFR1.17・NFR1.19（失敗の分類）、NFR2.5（まとめ読みと読み残し）。

moto で起こせない TransactionConflict とスロットリングは、CancellationReasons を
組み立てたスタブの client で確かめる（reliability-design.md §1）。

NFR9.23（2 つの書き込みを交互に起こしても索引と本体が一致）は、本物の ResourceSharing と
この Store を moto でつなぎ、1 人目の読みと書きの間に 2 人目の書き込みを差し込んで確かめる
（TestInterleavedShareWrites。Build and Test の Loop-back 1）。
"""

from typing import Any
from unittest.mock import Mock

import pytest

moto = pytest.importorskip("moto", reason="moto is required for DynamoDB tests")

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from src.managers.components.resource_sharing import ResourceSharing  # noqa: E402
from src.models.authz import (  # noqa: E402
    AccessTarget,
    Department,
    Principal,
    ResourceType,
    Role,
)
from src.models.errors import ErrorCode  # noqa: E402
from src.models.sharing import (  # noqa: E402
    ResourceShareEntry,
    ShareChange,
    SharingTable,
)
from src.services.stores.resource_share_store import (  # noqa: E402
    ResourceShareStore,
    ResourceShareStoreError,
    ShareConditionFailed,
    ShareStartKeyInvalid,
    ShareThrottled,
    ShareTransactionConflict,
    decode_position,
    encode_position,
)

REGION = "ap-northeast-1"
PERSONAS = "PersonasTest"
SHARES = "ResourceSharesTest"
PERSONA = ResourceType.PERSONA
ATTRS = ["id", "owner_sub", "shared_group_ids", "created_at", "name", "avatar_path"]


def _create_tables(client: Any) -> None:
    client.create_table(
        TableName=PERSONAS,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "owner_sub", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
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
            }
        ],
    )
    client.create_table(
        TableName=SHARES,
        KeySchema=[
            {"AttributeName": "group_id", "KeyType": "HASH"},
            {"AttributeName": "share_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "group_id", "AttributeType": "S"},
            {"AttributeName": "share_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with moto.mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        _create_tables(client)
        yield client


@pytest.fixture
def store(aws) -> ResourceShareStore:
    return ResourceShareStore(
        region=REGION, shares_table=SHARES, client=aws, sleep=lambda _s: None
    )


def _put_persona(
    client: Any,
    pid: str,
    *,
    owner: str | None = "sub-a",
    created_at: str = "2026-10-01T09:00:00",
    shared: list[str] | None = None,
    avatar: str | None = None,
) -> None:
    item: dict[str, Any] = {
        "id": {"S": pid},
        "name": {"S": f"name-{pid}"},
        "created_at": {"S": created_at},
        "background": {"S": "本文"},
    }
    if owner:
        item["owner_sub"] = {"S": owner}
    if shared:
        item["shared_group_ids"] = {"SS": shared}
    if avatar:
        item["avatar_path"] = {"S": avatar}
    client.put_item(TableName=PERSONAS, Item=item)


def _share_items(client: Any) -> list[tuple[str, str]]:
    items = client.scan(TableName=SHARES)["Items"]
    return sorted((i["group_id"]["S"], i["share_key"]["S"]) for i in items)


def _write(store: ResourceShareStore, pid: str, expected: set[str], new: set[str]):
    store.write_shares(
        PERSONAS,
        PERSONA,
        pid,
        expected=frozenset(expected),
        new=frozenset(new),
        created_at="2026-10-01T09:00:00",
        owner_sub="sub-a",
    )


class TestWriteShares:
    def test_adds_groups_in_one_transaction(self, aws, store):
        _put_persona(aws, "p1")

        _write(store, "p1", set(), {"sales", "plan"})

        body = store.get_item(PERSONAS, "p1", ATTRS)
        assert body["shared_group_ids"] == {"sales", "plan"}
        assert _share_items(aws) == [
            ("plan", "persona#2026-10-01T09:00:00#p1"),
            ("sales", "persona#2026-10-01T09:00:00#p1"),
        ]
        item = aws.get_item(
            TableName=SHARES,
            Key={
                "group_id": {"S": "sales"},
                "share_key": {"S": "persona#2026-10-01T09:00:00#p1"},
            },
        )["Item"]
        assert item["owner_sub"] == {"S": "sub-a"}
        assert item["resource_id"] == {"S": "p1"}
        assert item["resource_type"] == {"S": "persona"}
        assert item["created_at"] == {"S": "2026-10-01T09:00:00"}

    def test_replaces_and_removes(self, aws, store):
        _put_persona(aws, "p1", shared=["sales"])
        aws.put_item(
            TableName=SHARES,
            Item={
                "group_id": {"S": "sales"},
                "share_key": {"S": "persona#2026-10-01T09:00:00#p1"},
            },
        )

        _write(store, "p1", {"sales"}, {"plan"})

        assert store.get_item(PERSONAS, "p1", ATTRS)["shared_group_ids"] == {"plan"}
        assert _share_items(aws) == [("plan", "persona#2026-10-01T09:00:00#p1")]

    def test_empty_set_removes_the_column(self, aws, store):
        _put_persona(aws, "p1", shared=["sales"])

        _write(store, "p1", {"sales"}, set())

        assert "shared_group_ids" not in store.get_item(PERSONAS, "p1", ATTRS)
        assert _share_items(aws) == []

    def test_other_columns_are_untouched(self, aws, store):
        _put_persona(aws, "p1", avatar="s3://a.webp")

        _write(store, "p1", set(), {"sales"})

        item = aws.get_item(TableName=PERSONAS, Key={"id": {"S": "p1"}})["Item"]
        assert item["background"] == {"S": "本文"}
        assert item["avatar_path"] == {"S": "s3://a.webp"}
        assert item["owner_sub"] == {"S": "sub-a"}

    def test_condition_fails_when_shares_changed(self, aws, store):
        _put_persona(aws, "p1", shared=["hr"])

        with pytest.raises(ShareConditionFailed):
            _write(store, "p1", set(), {"sales"})
        assert _share_items(aws) == []
        assert store.get_item(PERSONAS, "p1", ATTRS)["shared_group_ids"] == {"hr"}

    def test_condition_fails_when_the_body_is_gone(self, aws, store):
        with pytest.raises(ShareConditionFailed):
            _write(store, "missing", set(), {"sales"})
        assert _share_items(aws) == []
        assert store.get_item(PERSONAS, "missing", ATTRS) is None


class TestReads:
    def test_get_item_projects_and_is_consistent(self):
        client = Mock()
        client.get_item.return_value = {"Item": {"id": {"S": "p1"}}}
        store = ResourceShareStore(region=REGION, shares_table=SHARES, client=client)

        assert store.get_item(PERSONAS, "p1", ["id", "name"]) == {"id": "p1"}

        kwargs = client.get_item.call_args.kwargs
        assert kwargs["ConsistentRead"] is True
        assert set(kwargs["ExpressionAttributeNames"].values()) == {"id", "name"}


class TestOwnerIndex:
    def test_newest_first_page_and_count(self, aws, store):
        for i in range(5):
            _put_persona(aws, f"p{i}", created_at=f"2026-10-0{i + 1}T00:00:00")
        _put_persona(aws, "other", owner="sub-b")
        _put_persona(aws, "legacy", owner=None)

        ids, last = store.query_owner(PERSONAS, "sub-a", limit=2)
        assert ids == ["p4", "p3"]
        assert last is not None
        rest, _ = store.query_owner(PERSONAS, "sub-a", limit=10, start_key=last)
        assert rest == ["p2", "p1", "p0"]
        assert store.count_owner(PERSONAS, "sub-a") == 5

    def test_tampered_start_key_is_reported(self):
        client = Mock()
        client.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad key"}}, "Query"
        )
        store = ResourceShareStore(region=REGION, shares_table=SHARES, client=client)

        with pytest.raises(ShareStartKeyInvalid):
            store.query_owner(PERSONAS, "sub-a", limit=2, start_key={"id": {"S": "x"}})


class TestGroupIndex:
    def test_reads_every_entry_of_a_type_consistently(self, aws, store):
        for pid, ts in [("p1", "2026-10-01T00:00:00"), ("p2", "2026-10-02T00:00:00")]:
            _put_persona(aws, pid, created_at=ts)
            store.write_shares(
                PERSONAS,
                PERSONA,
                pid,
                expected=frozenset(),
                new=frozenset({"sales"}),
                created_at=ts,
                owner_sub="sub-a",
            )
        aws.put_item(
            TableName=SHARES,
            Item={
                "group_id": {"S": "sales"},
                "share_key": {"S": "discussion#2026-10-03T00:00:00#d1"},
                "resource_type": {"S": "discussion"},
                "resource_id": {"S": "d1"},
                "created_at": {"S": "2026-10-03T00:00:00"},
                "owner_sub": {"S": "sub-a"},
            },
        )

        entries = store.query_group("sales", PERSONA)

        assert [e.resource_id for e in entries] == ["p2", "p1"]
        assert entries[0] == ResourceShareEntry(
            "sales", PERSONA, "p2", "2026-10-02T00:00:00", "sub-a"
        )

    def test_query_is_strongly_consistent(self):
        client = Mock()
        client.query.return_value = {"Items": []}
        store = ResourceShareStore(region=REGION, shares_table=SHARES, client=client)

        store.query_group("sales", PERSONA)

        assert client.query.call_args.kwargs["ConsistentRead"] is True
        assert client.query.call_args.kwargs["ScanIndexForward"] is False


class TestScan:
    def test_page_and_count(self, aws, store):
        for i in range(3):
            _put_persona(aws, f"p{i}")

        items, last = store.scan_page(PERSONAS, limit=2, attributes=["id", "owner_sub"])
        assert len(items) == 2 and last is not None
        more, last2 = store.scan_page(
            PERSONAS, limit=2, start_key=last, attributes=["id"]
        )
        assert len(more) == 1
        assert {i["id"] for i in items + more} == {"p0", "p1", "p2"}
        assert store.scan_count(PERSONAS) == 3


class TestDelete:
    def test_deletes_body_and_shares_together(self, aws, store):
        _put_persona(aws, "p1", shared=["sales", "plan"], avatar="s3://a.webp")
        for group in ("sales", "plan"):
            aws.put_item(
                TableName=SHARES,
                Item={
                    "group_id": {"S": group},
                    "share_key": {"S": "persona#2026-10-01T09:00:00#p1"},
                },
            )

        store.delete_with_shares(
            PERSONAS,
            PERSONA,
            "p1",
            expected=frozenset({"sales", "plan"}),
            created_at="2026-10-01T09:00:00",
            cleanup={"avatar_path": "s3://a.webp"},
        )

        assert store.get_item(PERSONAS, "p1", ATTRS) is None
        assert _share_items(aws) == []

    def test_changed_avatar_fails_the_condition(self, aws, store):
        _put_persona(aws, "p1", avatar="s3://new.webp")

        with pytest.raises(ShareConditionFailed):
            store.delete_with_shares(
                PERSONAS,
                PERSONA,
                "p1",
                expected=frozenset(),
                created_at="2026-10-01T09:00:00",
                cleanup={"avatar_path": "s3://old.webp"},
            )
        assert store.get_item(PERSONAS, "p1", ATTRS) is not None

    def test_absent_cleanup_column_is_a_condition_too(self, aws, store):
        _put_persona(aws, "p1", avatar="s3://appeared.webp")

        with pytest.raises(ShareConditionFailed):
            store.delete_with_shares(
                PERSONAS,
                PERSONA,
                "p1",
                expected=frozenset(),
                created_at="2026-10-01T09:00:00",
                cleanup={"avatar_path": None},
            )


def _cancelled(*codes: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": c} for c in codes],
        },
        "TransactWriteItems",
    )


class TestFailureClassification:
    def _store(
        self, client: Any, sleeps: list[float] | None = None
    ) -> ResourceShareStore:
        record = sleeps if sleeps is not None else []
        return ResourceShareStore(
            region=REGION, shares_table=SHARES, client=client, sleep=record.append
        )

    def test_body_condition_failure(self):
        client = Mock()
        client.transact_write_items.side_effect = _cancelled(
            "ConditionalCheckFailed", "None"
        )
        with pytest.raises(ShareConditionFailed):
            _write(self._store(client), "p1", set(), {"sales"})

    def test_transaction_conflict(self):
        client = Mock()
        client.transact_write_items.side_effect = _cancelled(
            "None", "TransactionConflict"
        )
        with pytest.raises(ShareTransactionConflict):
            _write(self._store(client), "p1", set(), {"sales"})

    def test_throttling_is_retried_twice_then_raised(self):
        client = Mock()
        client.transact_write_items.side_effect = _cancelled("ThrottlingError", "None")
        sleeps: list[float] = []
        with pytest.raises(ShareThrottled):
            _write(self._store(client, sleeps), "p1", set(), {"sales"})
        assert client.transact_write_items.call_count == 3
        assert sleeps == [0.1, 0.2]

    def test_throttling_then_success(self):
        client = Mock()
        client.transact_write_items.side_effect = [
            ClientError(
                {"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems"
            ),
            {},
        ]
        _write(self._store(client), "p1", set(), {"sales"})
        assert client.transact_write_items.call_count == 2

    def test_other_failures_are_not_retried(self):
        client = Mock()
        client.transact_write_items.side_effect = _cancelled("ValidationError", "None")
        with pytest.raises(ResourceShareStoreError) as exc:
            _write(self._store(client), "p1", set(), {"sales"})
        assert not isinstance(
            exc.value, (ShareConditionFailed, ShareTransactionConflict)
        )
        assert exc.value.reason == "failed"
        assert client.transact_write_items.call_count == 1


class _InterleavingStore:
    """Wraps the real store and runs another writer before the first write.

    The wrapped writer reads the body, then — just before its first
    ``write_shares`` — ``interleave`` runs the second writer to completion. The
    first write therefore meets a changed body, as two users saving at the same
    moment would (NFR9.23). No threads and no sleep: the order is fixed.
    """

    def __init__(self, inner: ResourceShareStore, interleave: Any) -> None:
        self._inner = inner
        self._interleave = interleave
        self.write_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def write_shares(self, *args: Any, **kwargs: Any) -> None:
        self.write_calls += 1
        if self.write_calls == 1:
            self._interleave()
        self._inner.write_shares(*args, **kwargs)


def _indexed_groups(client: Any, pid: str) -> list[str]:
    """Department of every ResourceShares row of ``pid`` (duplicates kept)."""
    items = client.scan(TableName=SHARES)["Items"]
    return sorted(i["group_id"]["S"] for i in items if i["resource_id"]["S"] == pid)


class TestInterleavedShareWrites:
    """NFR9.23: after two interleaved writes the index equals the body."""

    OWNER = Principal("sub-b", frozenset({Role.MEMBER}), frozenset({"sales", "plan"}))

    def _sharing(self, store: Any) -> ResourceSharing:
        departments = Mock()
        departments.list_all.return_value = [
            Department("sales", "営業部"),
            Department("plan", "企画部"),
            Department("hr", "人事部"),
        ]
        sharing = ResourceSharing(store, departments, sleep=lambda _s: None)
        sharing.register(
            SharingTable(
                resource_type=PERSONA,
                table=PERSONAS,
                name_attribute="name",
                not_found=ErrorCode.PERSONA_NOT_FOUND,
                delete_failed=ErrorCode.PERSONA_OPERATION_FAILED,
                cleanup_attributes=("avatar_path",),
            )
        )
        return sharing

    def _run(
        self,
        store: ResourceShareStore,
        first: ShareChange,
        second: ShareChange,
    ) -> tuple[frozenset[str], _InterleavingStore]:
        target = AccessTarget(PERSONA, "p1")
        other = self._sharing(store)
        wrapped = _InterleavingStore(
            store, lambda: other.update_shares(self.OWNER, target, second)
        )
        result = self._sharing(wrapped).update_shares(self.OWNER, target, first)
        return result, wrapped

    def _body_groups(self, store: ResourceShareStore) -> list[str]:
        body = store.get_item(PERSONAS, "p1", ATTRS)
        return sorted(body.get("shared_group_ids", set()))

    def test_add_and_add(self, aws, store):
        _put_persona(aws, "p1", owner="sub-b")

        result, wrapped = self._run(
            store,
            ShareChange(add=frozenset({"sales"})),
            ShareChange(add=frozenset({"plan"})),
        )

        # 索引の部署ごとの行が本体の shared_group_ids と一致する（NFR9.23）
        assert _indexed_groups(aws, "p1") == self._body_groups(store)
        assert self._body_groups(store) == ["plan", "sales"]
        assert result == {"sales", "plan"}
        # 1 回目は条件の失敗、読み直して判定し直した 2 回目で書いた（BR1.9）
        assert wrapped.write_calls == 2

    def test_remove_and_add(self, aws, store):
        _put_persona(aws, "p1", owner="sub-b", shared=["sales"])
        _write_index(aws, "p1", "sales")

        result, wrapped = self._run(
            store,
            ShareChange(remove=frozenset({"sales"})),
            ShareChange(add=frozenset({"plan"})),
        )

        assert _indexed_groups(aws, "p1") == self._body_groups(store)
        assert self._body_groups(store) == ["plan"]
        assert result == {"plan"}
        assert wrapped.write_calls == 2

    def test_add_and_remove(self, aws, store):
        _put_persona(aws, "p1", owner="sub-b", shared=["sales"])
        _write_index(aws, "p1", "sales")

        result, wrapped = self._run(
            store,
            ShareChange(add=frozenset({"plan"})),
            ShareChange(remove=frozenset({"sales"})),
        )

        assert _indexed_groups(aws, "p1") == self._body_groups(store)
        assert self._body_groups(store) == ["plan"]
        assert result == {"plan"}
        assert wrapped.write_calls == 2


def _write_index(client: Any, pid: str, group: str) -> None:
    """Seed the index row that matches a body already shared with ``group``."""
    client.put_item(
        TableName=SHARES,
        Item={
            "group_id": {"S": group},
            "share_key": {"S": f"persona#2026-10-01T09:00:00#{pid}"},
            "resource_id": {"S": pid},
            "resource_type": {"S": "persona"},
            "created_at": {"S": "2026-10-01T09:00:00"},
            "owner_sub": {"S": "sub-b"},
        },
    )


class TestPosition:
    def test_round_trip(self):
        key = {"id": {"S": "p1"}, "owner_sub": {"S": "s"}, "created_at": {"S": "t"}}
        assert decode_position(encode_position(key)) == key

    @pytest.mark.parametrize("bad", ["", "%%%", "bm90IGpzb24", "WzFd"])
    def test_unreadable_values_are_none(self, bad):
        assert decode_position(bad) is None
